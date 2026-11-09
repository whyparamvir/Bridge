"""Budget ledger: reserve before concurrent work, consume actual usage, release the rest.

Race safety: a reservation is ONE conditional UPDATE
  reserved = reserved + x  WHERE reserved + spent + x <= limit
so concurrent reservers can never jointly exceed the limit. Payer grants get the same
treatment against their own cap. Limits are a safety margin, not a billing guarantee:
provider usage is reported after the fact and is always recorded, even past the cap.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts.common import Money
from ..contracts.compute import PayerRef
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import BudgetUpdated
from ..core import new_id
from ..db import tables as t
from ..reliability import emit


def _emit_budget(conn: Connection, now: datetime, project_id: UUID) -> None:
    r = conn.execute(sa.select(t.projects).where(t.projects.c.id == project_id)).one()
    c = r.currency
    emit(conn, now, project_id, project_id, r.revision, BudgetUpdated(
        reserved=Money(amount_micros=r.budget_reserved_micros, currency=c),
        spent=Money(amount_micros=r.budget_spent_micros, currency=c),
        limit=Money(amount_micros=r.budget_limit_micros, currency=c)))


def resolve_payer(conn: Connection, now: datetime, project_id: UUID, requester_user_id: UUID,
                  payer: PayerRef) -> tuple[UUID | None, UUID | None]:
    """Return (payer_user_id, payer_grant_id). Spending someone else's allowance needs a
    live grant naming the requester for this project."""
    if payer.kind == "workspace":
        return None, None
    if payer.kind == "requester":
        if payer.user_id not in (None, requester_user_id):
            raise DomainError(ErrorCode.policy_denied, "requester payer must be the requester")
        return requester_user_id, None
    g = t.payer_grants.c
    grant = conn.execute(sa.select(t.payer_grants).where(g.id == payer.payer_grant_id)).first()
    if (grant is None or grant.project_id != project_id or grant.grantee_user_id != requester_user_id
            or grant.revoked_at is not None or grant.expires_at <= now):
        raise DomainError(ErrorCode.policy_denied, "no live payer grant for this requester and project")
    return grant.grantor_user_id, grant.id


def reserve(conn: Connection, now: datetime, project_id: UUID, amount: Money, *, subject_kind: str,
            subject_id: UUID, payer_user_id: UUID | None = None, payer_grant_id: UUID | None = None) -> UUID:
    pr = t.projects.c
    n = conn.execute(sa.update(t.projects).where(
        pr.id == project_id, pr.currency == amount.currency,
        pr.budget_reserved_micros + pr.budget_spent_micros + amount.amount_micros <= pr.budget_limit_micros,
    ).values(budget_reserved_micros=pr.budget_reserved_micros + amount.amount_micros)).rowcount
    if n != 1:
        raise DomainError(ErrorCode.budget_exhausted, "project budget would be exceeded")
    if payer_grant_id is not None:
        g = t.payer_grants.c
        n = conn.execute(sa.update(t.payer_grants).where(
            g.id == payer_grant_id, g.revoked_at.is_(None), g.expires_at > now,
            g.reserved_micros + g.spent_micros + amount.amount_micros <= g.cap_micros,
        ).values(reserved_micros=g.reserved_micros + amount.amount_micros)).rowcount
        if n != 1:
            raise DomainError(ErrorCode.budget_exhausted, "payer grant cap would be exceeded")
    rid = new_id()
    conn.execute(sa.insert(t.budget_reservations).values(
        id=rid, project_id=project_id, payer_user_id=payer_user_id, payer_grant_id=payer_grant_id,
        subject_kind=subject_kind, subject_id=subject_id, currency=amount.currency,
        estimate_micros=amount.amount_micros, consumed_micros=0, state="held", created_at=now))
    _emit_budget(conn, now, project_id)
    return rid


def consume(conn: Connection, now: datetime, reservation_id: UUID, cost_micros: int, *, kind: str,
            operation_key: str | None = None, model_endpoint_id: UUID | None = None,
            provider_model_id: str | None = None, input_tokens: int | None = None,
            output_tokens: int | None = None, runtime_ms: int | None = None,
            provider_reported: bool = True) -> bool:
    """Record actual usage against a held reservation. Moves min(cost, remaining) out of
    `reserved` and the full cost into `spent`. Idempotent on operation_key. Returns False
    when the cost overran the reservation (callers must then stop new spending)."""
    b = t.budget_reservations.c
    res = conn.execute(sa.select(t.budget_reservations).where(b.id == reservation_id)).one()
    try:
        conn.execute(sa.insert(t.usage_entries).values(
            id=new_id(), project_id=res.project_id, reservation_id=reservation_id, kind=kind,
            model_endpoint_id=model_endpoint_id, provider_model_id=provider_model_id,
            input_tokens=input_tokens, output_tokens=output_tokens, runtime_ms=runtime_ms,
            cost_micros=cost_micros, currency=res.currency, provider_reported=provider_reported,
            operation_key=operation_key, recorded_at=now))
    except IntegrityError:
        return True  # already recorded (replay after restart)
    remaining = max(0, res.estimate_micros - res.consumed_micros) if res.state == "held" else 0
    moved = min(cost_micros, remaining)
    conn.execute(sa.update(t.budget_reservations).where(b.id == reservation_id)
                 .values(consumed_micros=b.consumed_micros + cost_micros))
    pr = t.projects.c
    conn.execute(sa.update(t.projects).where(pr.id == res.project_id).values(
        budget_reserved_micros=pr.budget_reserved_micros - moved,
        budget_spent_micros=pr.budget_spent_micros + cost_micros))
    if res.payer_grant_id is not None:
        g = t.payer_grants.c
        conn.execute(sa.update(t.payer_grants).where(g.id == res.payer_grant_id).values(
            reserved_micros=g.reserved_micros - moved, spent_micros=g.spent_micros + cost_micros))
    _emit_budget(conn, now, res.project_id)
    return cost_micros <= remaining


def remaining(conn: Connection, reservation_id: UUID) -> int:
    r = conn.execute(sa.select(t.budget_reservations).where(t.budget_reservations.c.id == reservation_id)).one()
    return 0 if r.state != "held" else max(0, r.estimate_micros - r.consumed_micros)


def release(conn: Connection, now: datetime, reservation_id: UUID) -> None:
    """Return the unconsumed remainder. Idempotent (CAS on state)."""
    b = t.budget_reservations.c
    res = conn.execute(sa.select(t.budget_reservations).where(b.id == reservation_id)).first()
    if res is None:
        return
    n = conn.execute(sa.update(t.budget_reservations).where(b.id == reservation_id, b.state == "held")
                     .values(state="released", revision=b.revision + 1)).rowcount
    if n != 1:
        return
    left = max(0, res.estimate_micros - res.consumed_micros)
    pr = t.projects.c
    conn.execute(sa.update(t.projects).where(pr.id == res.project_id)
                 .values(budget_reserved_micros=pr.budget_reserved_micros - left))
    if res.payer_grant_id is not None:
        g = t.payer_grants.c
        conn.execute(sa.update(t.payer_grants).where(g.id == res.payer_grant_id)
                     .values(reserved_micros=g.reserved_micros - left))
    _emit_budget(conn, now, res.project_id)
