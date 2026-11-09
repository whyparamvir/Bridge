"""Section 20 delegation protocol: discover -> request -> reserve/assign -> recipient-only
assignment delivery -> accept/decline -> heartbeat -> fenced completion -> requester
verification.

Permission is the intersection of: requester membership + role, task visibility,
non-widening bounds, a payer the requester may spend, the recipient agent's persistent
per-project consent, the recipient owner's live membership, and (for device execution)
the device owner's sharing policy + eligibility + an atomic capacity reservation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts import api as A
from ..contracts.common import ActorKind, Money, ResourceVector, Visibility
from ..contracts.compute import (
    AssignmentOperation, AssignmentProof, DelegationAccept, DelegationAssignment, DelegationBounds,
    DelegationDecline, DelegationHeartbeat, DelegationHeartbeatResponse, DelegationMode, DelegationRequest,
    DelegationResult, DelegationState as S, DelegationVerification, PayerRef, ReservationKind,
)
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import DelegationUpdated
from ..core import Principal, new_id, new_token, not_found, require_role, token_hash
from ..db import tables as t
from ..domain import policy as P
from ..domain.state_machines import DELEGATION
from ..reliability import emit, enqueue
from . import budget, scheduling
from .workspace import owned_agent

ASSIGN_TTL = timedelta(seconds=60)  # time for the recipient to fetch + accept
RUN_LEASE = timedelta(seconds=30)
PRESENCE_STALE = P.STALE_AFTER
_SYS = ActorKind.system


def contract(r) -> DelegationRequest:  # noqa: ANN001
    b = r.body
    return DelegationRequest(
        id=r.id, project_id=r.project_id, task_id=r.task_id, parent_delegation_id=r.parent_delegation_id,
        mode=DelegationMode(r.mode), requester_agent_id=r.requester_agent_id,
        requester_user_id=r.requester_user_id, recipient_agent_id=r.recipient_agent_id,
        input_artifact_ids=b["input_artifact_ids"], acceptance_criteria=b["acceptance_criteria"],
        resource_requirements=ResourceVector.model_validate(b["resource_requirements"]),
        payer=PayerRef.model_validate(b["payer"]), bounds=DelegationBounds.model_validate(b["bounds"]),
        state=S(r.state), reservation_id=r.reservation_id, worker_id=r.worker_id,
        attempt_id=r.attempt_id, fencing_token=r.fence or None, lease_expires_at=r.lease_expires_at,
        blocked_reason=r.blocked_reason, declined_agent_ids=[UUID(a) for a in r.declined_agent_ids],
        decline_count=r.decline_count, max_alternates=r.max_alternates, idempotency_key=r.idempotency_key,
        revision=r.revision, created_at=r.created_at)


def _row(conn: Connection, did: UUID):  # noqa: ANN201
    row = conn.execute(sa.select(t.delegations).where(t.delegations.c.id == did)).first()
    if row is None:
        raise not_found("delegation")
    return row


def _event(conn: Connection, now: datetime, row) -> None:  # noqa: ANN001
    # Never includes the assignment token.
    emit(conn, now, row.project_id, row.id, row.revision, DelegationUpdated(
        delegation_id=row.id, state=S(row.state), worker_id=row.worker_id,
        recipient_agent_id=row.recipient_agent_id))


def _move(conn: Connection, now: datetime, row, dst: S, actor: ActorKind, *, lease_ok: bool = False,  # noqa: ANN001
          **values) -> sa.Row:  # noqa: ANN003
    DELEGATION.check(S(row.state), dst, actor, lease_ok=lease_ok)
    dc = t.delegations.c
    n = conn.execute(sa.update(t.delegations).where(dc.id == row.id, dc.revision == row.revision)
                     .values(state=dst.value, revision=dc.revision + 1, **values)).rowcount
    if n != 1:
        raise DomainError(ErrorCode.revision_conflict, "delegation changed concurrently")
    new = _row(conn, row.id)
    _event(conn, now, new)
    return new


# ------------------------------------------------------------------------ create


def _root_bounds(conn: Connection, task, agent, body: A.CreateDelegation) -> DelegationBounds:  # noqa: ANN001
    """The ceiling for a top-level delegation, derived from the task and agent."""
    return DelegationBounds(
        budget=Money(amount_micros=task.budget_limit_micros, currency=task.currency),
        deadline=body.bounds.deadline, remaining_depth=agent.max_delegation_depth,
        allowed_tools=body.bounds.allowed_tools, allowed_model_endpoint_ids=body.bounds.allowed_model_endpoint_ids,
        data_visibility=Visibility.project)


def create(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreateDelegation) -> DelegationRequest:
    from .work import readable_task

    require_role(conn, p, project_id, "editor")
    requester_agent = p.agent_id if p.kind == "agent" else body.requester_agent_id
    if requester_agent is None:
        raise DomainError(ErrorCode.validation_error, "requester_agent_id required for human callers")
    agent = owned_agent(conn, p, requester_agent)
    task = readable_task(conn, p, body.task_id, "editor")
    if task.project_id != project_id:
        raise not_found("task")
    if task.visibility == "private":
        raise DomainError(ErrorCode.policy_denied, "private tasks cannot be delegated; publish a brief first")
    if body.bounds.deadline <= now:
        raise DomainError(ErrorCode.validation_error, "deadline already passed")
    if body.mode is DelegationMode.deterministic_job:
        raise DomainError(ErrorCode.validation_error,
                          "deterministic remote jobs use POST /v1/projects/{id}/jobs; delegation is for agent tasks")
    dc = t.delegations.c
    if body.parent_delegation_id is not None:
        parent = _row(conn, body.parent_delegation_id)
        if parent.project_id != project_id or parent.recipient_agent_id != requester_agent:
            raise DomainError(ErrorCode.permission_denied, "only the parent's recipient may sub-delegate")
        if parent.state not in ("accepted", "running"):
            raise DomainError(ErrorCode.invalid_transition, "parent delegation is not active")
        ceiling = DelegationBounds.model_validate(parent.body["bounds"])
        P.narrow_bounds(ceiling, body.bounds)  # strictly narrower, depth - 1
    else:
        ceiling = _root_bounds(conn, task, agent, body)
        if ceiling.remaining_depth < 1:
            raise DomainError(ErrorCode.delegation_bounds_exceeded, "agent may not delegate")
        _check_root(ceiling, body.bounds)
    for aid in body.input_artifact_ids:
        art = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == aid)).first()
        if art is None or art.project_id != project_id or art.status != "ready" or \
                (art.visibility == "private" and art.created_by_user_id != p.user_id):
            raise not_found("input artifact")
        if art.visibility == "private":
            raise DomainError(ErrorCode.delegation_bounds_exceeded, "private artifacts cannot be delegated")
    payer_user, grant_id = budget.resolve_payer(conn, now, project_id, p.user_id, body.payer)
    did = new_id()
    doc = {"input_artifact_ids": [str(a) for a in body.input_artifact_ids],
           "acceptance_criteria": body.acceptance_criteria,
           "resource_requirements": body.resource_requirements.model_dump(mode="json"),
           "payer": body.payer.model_dump(mode="json"), "bounds": body.bounds.model_dump(mode="json")}
    try:
        with conn.begin_nested():
            conn.execute(sa.insert(t.delegations).values(
                id=did, project_id=project_id, task_id=body.task_id, parent_delegation_id=body.parent_delegation_id,
                mode=body.mode.value, requester_agent_id=requester_agent, requester_user_id=p.user_id,
                recipient_agent_id=body.recipient_agent_id, requested_recipient=body.recipient_agent_id is not None,
                body=doc, state="requested", deadline=body.bounds.deadline, max_alternates=body.max_alternates,
                declined_agent_ids=[], idempotency_key=body.idempotency_key, created_at=now))
    except IntegrityError:
        prior = conn.execute(sa.select(t.delegations).where(
            dc.requester_user_id == p.user_id, dc.idempotency_key == body.idempotency_key)).one()
        if prior.body != doc or prior.task_id != body.task_id:
            raise DomainError(ErrorCode.idempotency_conflict, "idempotency key reused") from None
        return contract(prior)
    brid = budget.reserve(conn, now, project_id, body.bounds.budget, subject_kind="delegation", subject_id=did,
                          payer_user_id=payer_user, payer_grant_id=grant_id)
    conn.execute(sa.update(t.delegations).where(dc.id == did).values(budget_reservation_id=brid))
    _event(conn, now, _row(conn, did))
    schedule(conn, now, did)
    return contract(_row(conn, did))


def _check_root(ceiling: DelegationBounds, child: DelegationBounds) -> None:
    problems = []
    if child.budget.currency != ceiling.budget.currency:
        problems.append("currency mismatch")
    if child.budget.amount_micros > ceiling.budget.amount_micros:
        problems.append("budget exceeds task budget")
    if child.remaining_depth > ceiling.remaining_depth - 1:
        problems.append("depth exceeds agent limit")
    if child.data_visibility is Visibility.private:
        problems.append("private-scope data cannot be delegated")
    if problems:
        raise DomainError(ErrorCode.delegation_bounds_exceeded, "; ".join(problems))


# ---------------------------------------------------------------------- schedule


def _candidates(conn: Connection, now: datetime, row) -> list[sa.Row]:  # noqa: ANN001
    ap, ac, m = t.agent_profiles.c, t.agent_consents.c, t.memberships.c
    pr = t.agent_presence.c
    q = (sa.select(t.agent_profiles, t.agent_consents.c.allowed_skills)
         .join(t.agent_consents, sa.and_(ac.agent_id == ap.id, ac.project_id == row.project_id))
         .join(t.memberships, sa.and_(m.user_id == ap.owner_user_id, m.project_id == row.project_id))
         .join(t.agent_presence, sa.and_(pr.agent_id == ap.id, pr.project_id == row.project_id))
         .where(ac.accept_delegations.is_(True), m.revoked_at.is_(None), ap.id != row.requester_agent_id,
                pr.last_seen_at > now - PRESENCE_STALE, pr.last_seen_at <= now))
    if row.requested_recipient:
        q = q.where(ap.id == row.recipient_agent_id)
    declined = set(row.declined_agent_ids)
    return [c for c in conn.execute(q.order_by(ap.created_at)).all() if str(c.id) not in declined]


def schedule(conn: Connection, now: datetime, did: UUID) -> bool:
    """Assign to the first consenting, present recipient that can run it. Device work
    also needs that device's own opt-in, eligibility and an atomic reservation.
    Returns False (with a visible blocked_reason) when nobody qualifies right now."""
    row = _row(conn, did)
    if row.state != "requested" or row.cancel_requested:
        return False
    if now >= row.deadline:
        _finish(conn, now, _move(conn, now, row, S.expired, _SYS, blocked_reason="deadline passed"))
        return False
    resources = ResourceVector.model_validate(row.body["resource_requirements"])
    needs_device = resources != ResourceVector()
    reason = "no consenting agent is online"
    for cand in _candidates(conn, now, row):
        fence = row.fence + 1
        worker_id = rid = None
        if needs_device:
            pres = conn.execute(sa.select(t.agent_presence.c.worker_ids).where(
                t.agent_presence.c.agent_id == cand.id, t.agent_presence.c.project_id == row.project_id)).scalar() or []
            for wid in [UUID(w) for w in pres]:
                w = conn.execute(sa.select(t.workers).where(t.workers.c.id == wid)).first()
                if w is None or w.owner_user_id != cand.owner_user_id:
                    continue
                ok, why = scheduling.eligible(conn, now, wid, row.project_id, ReservationKind.delegation, None)
                if not ok:
                    reason = f"recipient device unavailable: {why}"
                    continue
                rid = scheduling.reserve_capacity(
                    conn, now, worker_id=wid, project_id=row.project_id, kind=ReservationKind.delegation,
                    subject_id=did, requester_user_id=row.requester_user_id, resources=resources, fence=fence,
                    ttl=ASSIGN_TTL)
                if rid is None:
                    reason = "recipient device has no free capacity"
                    continue
                worker_id = wid
                break
            if worker_id is None:
                continue
        _move(conn, now, row, S.assigned, _SYS, recipient_agent_id=cand.id, worker_id=worker_id,
              reservation_id=rid, fence=fence, attempt_id=new_id(), token_hash=None, token_expires_at=None,
              lease_expires_at=now + ASSIGN_TTL, blocked_reason=None)
        return True
    dc = t.delegations.c
    conn.execute(sa.update(t.delegations).where(dc.id == did).values(blocked_reason=reason[:200]))
    enqueue(conn, now, "delegation.schedule", did, delay=timedelta(seconds=10))
    return False


def requeue_delegation(conn: Connection, now: datetime, did: UUID, reason: str) -> None:
    """Revoke the current assignment (token, fence, capacity) and reschedule within the
    alternates bound. Used for lease expiry, revocation and stop-now."""
    row = _row(conn, did)
    if row.state not in ("assigned", "accepted", "running"):
        return
    scheduling.release_capacity(conn, now, row.reservation_id)
    if row.decline_count + 1 > row.max_alternates:
        new = _move(conn, now, row, S.failed if row.state == "running" else
                    (S.cancelled if row.state == "accepted" else S.declined), _SYS,
                    token_hash=None, reservation_id=None, blocked_reason=reason[:200])
        _finish(conn, now, new)
        return
    _move(conn, now, row, S.requested, _SYS, token_hash=None, token_expires_at=None, reservation_id=None,
          worker_id=None, attempt_id=None, lease_expires_at=None, decline_count=row.decline_count + 1,
          recipient_agent_id=row.recipient_agent_id if row.requested_recipient else None,
          blocked_reason=reason[:200])
    schedule(conn, now, did)


def _finish(conn: Connection, now: datetime, row) -> None:  # noqa: ANN001
    scheduling.release_capacity(conn, now, row.reservation_id)
    if row.budget_reservation_id:
        budget.release(conn, now, row.budget_reservation_id)


# --------------------------------------------------------------- recipient side


def _recipient(conn: Connection, p: Principal, row) -> None:  # noqa: ANN001
    if p.kind != "agent" or row.recipient_agent_id != p.agent_id:
        raise not_found("delegation")
    require_role(conn, p, row.project_id, "viewer")  # membership is rechecked every call


def _proof(conn: Connection, now: datetime, p: Principal, row, proof: AssignmentProof) -> None:  # noqa: ANN001
    _recipient(conn, p, row)
    if (row.token_hash is None or token_hash(proof.assignment_token) != row.token_hash
            or row.token_expires_at is None or row.token_expires_at <= now):
        raise DomainError(ErrorCode.assignment_token_invalid, "assignment token invalid or expired")
    if row.attempt_id != proof.attempt_id or row.fence != proof.fencing_token:
        raise DomainError(ErrorCode.stale_attempt, "attempt superseded")
    if row.lease_expires_at is None or row.lease_expires_at <= now:
        raise DomainError(ErrorCode.stale_attempt, "assignment lease expired")
    if row.cancel_requested:
        raise DomainError(ErrorCode.invalid_transition, "delegation cancelled")


def inbox(conn: Connection, now: datetime, p: Principal) -> list[DelegationRequest]:
    if p.kind != "agent":
        raise DomainError(ErrorCode.permission_denied, "agents only")
    dc = t.delegations.c
    rows = conn.execute(sa.select(t.delegations).where(
        dc.recipient_agent_id == p.agent_id, dc.state.in_(["assigned", "accepted", "running"])
    ).order_by(dc.created_at)).all()
    out = []
    for r in rows:
        try:
            require_role(conn, p, r.project_id, "viewer")
            out.append(contract(r))
        except DomainError:
            continue
    return out


def fetch_assignment(conn: Connection, now: datetime, p: Principal, did: UUID) -> DelegationAssignment:
    """Recipient-only. Mints a fresh token for the CURRENT attempt and revokes any
    earlier token for this delegation."""
    row = _row(conn, did)
    _recipient(conn, p, row)
    if row.state not in ("assigned", "accepted", "running") or row.attempt_id is None:
        raise DomainError(ErrorCode.invalid_transition, f"delegation is {row.state}")
    token = new_token("hd")
    expires = min(row.deadline, now + timedelta(hours=1))
    dc = t.delegations.c
    n = conn.execute(sa.update(t.delegations).where(dc.id == did, dc.attempt_id == row.attempt_id)
                     .values(token_hash=token_hash(token), token_expires_at=expires)).rowcount
    if n != 1:
        raise DomainError(ErrorCode.stale_attempt, "attempt changed")
    row = _row(conn, did)
    res = None
    if row.reservation_id:
        res = scheduling._reservation(conn.execute(sa.select(t.resource_reservations).where(
            t.resource_reservations.c.id == row.reservation_id)).one())
    ops = {"assigned": [AssignmentOperation.accept, AssignmentOperation.decline],
           "accepted": [AssignmentOperation.heartbeat, AssignmentOperation.decline],
           "running": [AssignmentOperation.heartbeat, AssignmentOperation.complete]}[row.state]
    return DelegationAssignment(
        delegation=contract(row), attempt_id=row.attempt_id, fencing_token=row.fence, assignment_token=token,
        token_expires_at=expires, lease_expires_at=row.lease_expires_at, reservation=res,
        input_grants={a: f"/v1/artifacts/{a}/content" for a in row.body["input_artifact_ids"][:50]},
        allowed_operations=ops)


def accept(conn: Connection, now: datetime, p: Principal, did: UUID, body: DelegationAccept) -> DelegationRequest:
    row = _row(conn, did)
    _proof(conn, now, p, row, body)
    if row.revision != body.expected_revision:
        raise DomainError(ErrorCode.revision_conflict, "delegation changed", current_revision=row.revision)
    lease = now + RUN_LEASE
    scheduling.extend_reservation(conn, row.reservation_id, lease)
    return contract(_move(conn, now, row, S.accepted, ActorKind.agent, lease_ok=True, lease_expires_at=lease))


def decline(conn: Connection, now: datetime, p: Principal, did: UUID, body: DelegationDecline) -> DelegationRequest:
    row = _row(conn, did)
    _proof(conn, now, p, row, body)
    if row.revision != body.expected_revision:
        raise DomainError(ErrorCode.revision_conflict, "delegation changed", current_revision=row.revision)
    declined = [*row.declined_agent_ids, str(p.agent_id)]
    scheduling.release_capacity(conn, now, row.reservation_id)
    nxt = P.state_after_decline(contract(row))
    common = {"token_hash": None, "reservation_id": None, "decline_count": row.decline_count + 1,
              "declined_agent_ids": declined, "blocked_reason": f"declined: {body.reason}"}
    if nxt is S.requested and not row.requested_recipient:
        _move(conn, now, row, S.requested, _SYS, worker_id=None, attempt_id=None, lease_expires_at=None,
              recipient_agent_id=None, token_expires_at=None, **common)
        schedule(conn, now, did)
    else:
        _finish(conn, now, _move(conn, now, row, S.declined, _SYS, **common))
    return contract(_row(conn, did))


def heartbeat(conn: Connection, now: datetime, p: Principal, did: UUID,
              body: DelegationHeartbeat) -> DelegationHeartbeatResponse:
    row = _row(conn, did)
    if row.cancel_requested and row.recipient_agent_id == p.agent_id:
        return DelegationHeartbeatResponse(lease_expires_at=now, cancel_requested=True)
    _proof(conn, now, p, row, body)
    lease = now + RUN_LEASE
    scheduling.extend_reservation(conn, row.reservation_id, lease)
    if row.state == "accepted":
        _move(conn, now, row, S.running, ActorKind.agent, lease_ok=True, lease_expires_at=lease)
    elif row.state == "running":
        dc = t.delegations.c
        conn.execute(sa.update(t.delegations).where(dc.id == did, dc.attempt_id == body.attempt_id)
                     .values(lease_expires_at=lease))
    else:
        raise DomainError(ErrorCode.invalid_transition, "accept before heartbeating")
    return DelegationHeartbeatResponse(lease_expires_at=lease)


def complete(conn: Connection, now: datetime, p: Principal, did: UUID, body: DelegationResult) -> DelegationRequest:
    """Fenced and token-bound. The result is UNTRUSTED until the requester verifies it.
    Artifacts must be finalized project artifacts with matching hashes; actual usage is
    charged to the delegation's budget reservation."""
    row = _row(conn, did)
    _proof(conn, now, p, row, body)
    if row.state != "running":
        raise DomainError(ErrorCode.invalid_transition, "only running delegations complete")
    for ref in body.artifacts:
        a = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == ref.artifact_id)).first()
        if a is None or a.project_id != row.project_id or a.sha256 != ref.sha256 or a.status != "ready":
            raise DomainError(ErrorCode.validation_error, f"artifact {ref.artifact_id} is not a finalized project artifact")
    bounds = DelegationBounds.model_validate(row.body["bounds"])
    if body.environment.model_endpoint_id is not None and bounds.allowed_model_endpoint_ids and \
            body.environment.model_endpoint_id not in bounds.allowed_model_endpoint_ids:
        raise DomainError(ErrorCode.delegation_bounds_exceeded, "result used a model outside the bounds")
    if body.actual_usage.amount_micros and row.budget_reservation_id:
        budget.consume(conn, now, row.budget_reservation_id, body.actual_usage.amount_micros,
                       kind="model_tokens", operation_key=f"delegation:{did}:attempt:{row.attempt_id}",
                       provider_reported=False)
    scheduling.release_capacity(conn, now, row.reservation_id)
    result = body.model_dump(mode="json", exclude={"assignment_token", "fencing_token"})
    target = S.completed if body.outcome == "succeeded" else S.failed
    new = _move(conn, now, row, target, ActorKind.agent, lease_ok=True, result=result, token_hash=None,
                lease_expires_at=None)
    if target is S.failed:
        _finish(conn, now, new)
    return contract(new)


# --------------------------------------------------------------- requester side


def get(conn: Connection, p: Principal, did: UUID) -> A.DelegationView:
    row = _row(conn, did)
    require_role(conn, p, row.project_id, "viewer")
    return A.DelegationView(delegation=contract(row), result=row.result)


def list_for_project(conn: Connection, p: Principal, project_id: UUID, limit: int) -> list[DelegationRequest]:
    require_role(conn, p, project_id, "viewer")
    dc = t.delegations.c
    return [contract(r) for r in conn.execute(sa.select(t.delegations).where(dc.project_id == project_id)
                                              .order_by(dc.created_at).limit(limit)).all()]


def verify(conn: Connection, now: datetime, p: Principal, did: UUID, body: DelegationVerification) -> DelegationRequest:
    """Only the requester side (its human or its agent) judges the result."""
    row = _row(conn, did)
    require_role(conn, p, row.project_id, "editor")
    is_requester = (p.kind == "agent" and p.agent_id == row.requester_agent_id) or \
                   (p.kind == "user" and p.user_id == row.requester_user_id)
    if not is_requester:
        raise DomainError(ErrorCode.permission_denied, "only the requester verifies results")
    if row.revision != body.expected_revision:
        raise DomainError(ErrorCode.revision_conflict, "delegation changed", current_revision=row.revision)
    new = _move(conn, now, row, S.verified if body.accepted else S.rejected_result, ActorKind.agent,
                blocked_reason=body.notes)
    _finish(conn, now, new)
    return contract(new)


def cancel(conn: Connection, now: datetime, p: Principal | None, did: UUID) -> DelegationRequest:
    row = _row(conn, did)
    if p is not None:
        require_role(conn, p, row.project_id, "editor")
        if not ((p.kind == "agent" and p.agent_id == row.requester_agent_id) or p.user_id == row.requester_user_id):
            raise DomainError(ErrorCode.permission_denied, "only the requester cancels")
    if row.state in [s.value for s in DELEGATION.terminal] or row.state == "completed":
        return contract(row)
    dc = t.delegations.c
    conn.execute(sa.update(t.delegations).where(dc.id == did).values(cancel_requested=True))
    new = _move(conn, now, _row(conn, did), S.cancelled, _SYS, token_hash=None, lease_expires_at=None)
    _finish(conn, now, new)
    return contract(new)


def expire(conn: Connection, now: datetime) -> int:
    dc = t.delegations.c
    rows = conn.execute(sa.select(t.delegations.c.id, t.delegations.c.state).where(
        dc.state.in_(["assigned", "accepted", "running"]), dc.lease_expires_at < now)).all()
    for r in rows:
        requeue_delegation(conn, now, r.id, "assignment lease expired")
    for r in conn.execute(sa.select(t.delegations).where(dc.state == "requested", dc.deadline <= now)).all():
        _finish(conn, now, _move(conn, now, r, S.expired, _SYS, blocked_reason="deadline passed"))
    return len(rows)


# --------------------------------------------------------------------- presence


def report_presence(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                    body: A.ReportPresence) -> None:
    if p.kind != "agent":
        raise DomainError(ErrorCode.permission_denied, "agents only")
    require_role(conn, p, project_id, "viewer")
    for wid in body.worker_ids:
        w = conn.execute(sa.select(t.workers.c.owner_user_id).where(t.workers.c.id == wid)).first()
        if w is None or w.owner_user_id != p.user_id:
            raise not_found("worker")
    pr = t.agent_presence.c
    conn.execute(sa.delete(t.agent_presence).where(pr.agent_id == p.agent_id, pr.project_id == project_id))
    conn.execute(sa.insert(t.agent_presence).values(agent_id=p.agent_id, project_id=project_id,
                                                    worker_ids=[str(w) for w in body.worker_ids], last_seen_at=now))
