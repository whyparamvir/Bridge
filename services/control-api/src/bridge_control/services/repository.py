"""Repository bindings, binding-scoped executor credentials and per-task repository
operations with fenced leases, reconciliation after lost responses, cancellation and
evidence. The executor (Codex's adapter) does all Git/GitHub I/O with its own App key.

Lost-response rule: an operation whose executor lease lapses may already have acted
(for example the draft PR exists but the response was lost). It is never blindly re-run:
it returns to `requested` with `reconcile_required=true`, and the next assignment tells
the executor to look up the operation marker before any side effect. After
`max_attempts` it fails visibly for a human.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts.artifacts import RepositoryBinding
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import RepositoryOperationUpdated
from ..contracts.repository import (
    CompleteRepositoryOperation, CreateRepositoryBinding, CreateRepositoryOperation, ExecutorCredential,
    RepositoryEvidence, RepositoryOperation, RepositoryOperationAssignment, RepositoryOperationHeartbeatResponse,
    RepositoryOperationKind, RepositoryOperationParams, RepositoryOperationProof, RepositoryOperationState,
)
from ..core import Principal, cas, new_id, new_token, not_found, require_human, require_role, token_hash
from ..db import tables as t
from ..reliability import emit

LEASE = timedelta(minutes=10)

# ------------------------------------------------------------------ bindings


def binding_contract(r) -> RepositoryBinding:  # noqa: ANN001
    return RepositoryBinding(id=r.id, project_id=r.project_id, provider=r.provider, owner=r.owner, repo=r.repo,
                             repository_id=r.repository_id, installation_id=r.installation_id,
                             default_branch=r.default_branch, created_by_user_id=r.created_by_user_id,
                             revoked_at=r.revoked_at, revision=r.revision, created_at=r.created_at)


def _binding(conn: Connection, bid: UUID):  # noqa: ANN202
    row = conn.execute(sa.select(t.repository_bindings).where(t.repository_bindings.c.id == bid)).first()
    if row is None:
        raise not_found("repository binding")
    return row


def create_binding(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                   body: CreateRepositoryBinding) -> RepositoryBinding:
    require_human(p, "binding a repository")
    require_role(conn, p, project_id, "owner")
    rb = t.repository_bindings.c
    dup = conn.execute(sa.select(rb.id).where(rb.project_id == project_id, rb.revoked_at.is_(None), sa.or_(
        sa.and_(rb.owner == body.owner, rb.repo == body.repo), rb.repository_id == body.repository_id))).first()
    if dup is not None:
        raise DomainError(ErrorCode.validation_error, "repository already bound to this project")
    bid = new_id()
    conn.execute(sa.insert(t.repository_bindings).values(
        id=bid, project_id=project_id, provider=body.provider, owner=body.owner, repo=body.repo,
        repository_id=body.repository_id, installation_id=body.installation_id, default_branch=body.default_branch,
        created_by_user_id=p.user_id, created_at=now))
    return binding_contract(_binding(conn, bid))


def list_bindings(conn: Connection, p: Principal, project_id: UUID) -> list[RepositoryBinding]:
    require_role(conn, p, project_id, "viewer")
    rb = t.repository_bindings.c
    return [binding_contract(r) for r in conn.execute(
        sa.select(t.repository_bindings).where(rb.project_id == project_id).order_by(rb.created_at)).all()]


def revoke_binding(conn: Connection, now: datetime, p: Principal, bid: UUID, expected_revision: int) -> RepositoryBinding:
    """Stops everything immediately: executor credentials die, open operations are
    cancelled (a claimed one learns via heartbeat/complete returning cancelled/stale)."""
    require_human(p, "revoking a repository binding")
    row = _binding(conn, bid)
    require_role(conn, p, row.project_id, "owner")
    rb = t.repository_bindings.c
    cas(conn, sa.update(t.repository_bindings).where(rb.id == bid, rb.revision == expected_revision,
                                                     rb.revoked_at.is_(None))
        .values(revoked_at=now, revision=rb.revision + 1), "repository binding")
    conn.execute(sa.update(t.credentials).where(t.credentials.c.binding_id == bid, t.credentials.c.revoked_at.is_(None))
                 .values(revoked_at=now))
    ro = t.repository_operations.c
    for op in conn.execute(sa.select(t.repository_operations.c.id).where(
            ro.binding_id == bid, ro.state.in_(["requested", "claimed"]))).scalars().all():
        _cancel(conn, now, op, "repository binding revoked")
    return binding_contract(_binding(conn, bid))


def issue_executor_credential(conn: Connection, now: datetime, p: Principal, bid: UUID,
                              ttl: timedelta) -> ExecutorCredential:
    require_human(p, "issuing repository executor credentials")
    row = _binding(conn, bid)
    require_role(conn, p, row.project_id, "owner")
    if row.revoked_at is not None:
        raise DomainError(ErrorCode.invalid_transition, "binding is revoked")
    token = new_token("hx")
    conn.execute(sa.insert(t.credentials).values(
        id=new_id(), kind="executor", token_hash=token_hash(token), user_id=p.user_id, binding_id=bid,
        created_at=now, expires_at=now + ttl))
    return ExecutorCredential(binding_id=bid, token=token, expires_at=now + ttl)


def _executor_binding(conn: Connection, p: Principal, bid: UUID):  # noqa: ANN202
    """Executor credentials act only on their own binding, only while it is live and only
    while their issuer is still an owner of the project."""
    if p.kind != "executor" or p.binding_id != bid:
        raise not_found("repository binding")
    row = _binding(conn, bid)
    owner = conn.execute(sa.select(t.memberships.c.role).where(
        t.memberships.c.project_id == row.project_id, t.memberships.c.user_id == p.user_id,
        t.memberships.c.revoked_at.is_(None))).scalar()
    if row.revoked_at is not None or owner != "owner":
        raise not_found("repository binding")
    return row


# ---------------------------------------------------------------- operations


def op_contract(r) -> RepositoryOperation:  # noqa: ANN001
    claimed = r.state == "claimed"
    return RepositoryOperation(
        id=r.id, project_id=r.project_id, task_id=r.task_id, binding_id=r.binding_id,
        kind=RepositoryOperationKind(r.kind), operation_key=r.operation_key,
        params=RepositoryOperationParams.model_validate(r.params), state=RepositoryOperationState(r.state),
        attempt_id=r.attempt_id if claimed else None, fencing_token=r.fence if claimed else None,
        lease_expires_at=r.lease_expires_at if claimed else None, attempts=r.attempts, max_attempts=r.max_attempts,
        reconcile_required=r.reconcile_required,
        evidence=RepositoryEvidence.model_validate(r.evidence) if r.evidence else None, error=r.error,
        requested_by_user_id=r.requested_by_user_id, revision=r.revision, created_at=r.created_at,
        updated_at=r.updated_at)


def _op(conn: Connection, oid: UUID):  # noqa: ANN202
    row = conn.execute(sa.select(t.repository_operations).where(t.repository_operations.c.id == oid)).first()
    if row is None:
        raise not_found("repository operation")
    return row


def _event(conn: Connection, now: datetime, row) -> None:  # noqa: ANN001
    ev = row.evidence or {}
    emit(conn, now, row.project_id, row.id, row.revision, RepositoryOperationUpdated(
        operation_id=row.id, task_id=row.task_id, binding_id=row.binding_id, kind=row.kind, state=row.state,
        pr_url=ev.get("pr_url")))


def create_operation(conn: Connection, now: datetime, p: Principal, task_id: UUID,
                     body: CreateRepositoryOperation) -> tuple[RepositoryOperation, bool]:
    """Returns (operation, created). Repeating the same operation_key with the same body
    returns the original (lost-response safe); a different body is a conflict."""
    from .work import readable_task

    task = readable_task(conn, p, task_id, "editor")
    if task.state in ("done", "cancelled", "failed"):
        raise DomainError(ErrorCode.invalid_transition, f"task is {task.state}")
    b = _binding(conn, body.binding_id)
    if b.project_id != task.project_id:
        raise not_found("repository binding")
    if b.revoked_at is not None:
        raise DomainError(ErrorCode.policy_denied, "repository binding is revoked")
    if body.params.head_branch == b.default_branch:
        raise DomainError(ErrorCode.policy_denied, "agents never write the default branch; humans merge")
    ro = t.repository_operations.c
    prior = conn.execute(sa.select(t.repository_operations).where(
        ro.binding_id == b.id, ro.operation_key == body.operation_key)).first()
    params = body.params.model_dump(mode="json")
    if prior is not None:
        if prior.task_id != task_id or prior.kind != body.kind.value or prior.params != params:
            raise DomainError(ErrorCode.idempotency_conflict, "operation_key reused with a different request")
        return op_contract(prior), False
    oid = new_id()
    try:
        with conn.begin_nested():
            conn.execute(sa.insert(t.repository_operations).values(
                id=oid, project_id=task.project_id, task_id=task_id, binding_id=b.id, requested_by_user_id=p.user_id,
                kind=body.kind.value, operation_key=body.operation_key, params=params, state="requested",
                created_at=now, updated_at=now))
    except IntegrityError:  # concurrent duplicate
        return op_contract(conn.execute(sa.select(t.repository_operations).where(
            ro.binding_id == b.id, ro.operation_key == body.operation_key)).one()), False
    row = _op(conn, oid)
    _event(conn, now, row)
    return op_contract(row), True


def get_operation(conn: Connection, p: Principal, oid: UUID) -> RepositoryOperation:
    from .work import readable_task

    row = _op(conn, oid)
    readable_task(conn, p, row.task_id)
    return op_contract(row)


def list_for_task(conn: Connection, p: Principal, task_id: UUID) -> list[RepositoryOperation]:
    from .work import readable_task

    readable_task(conn, p, task_id)
    ro = t.repository_operations.c
    return [op_contract(r) for r in conn.execute(
        sa.select(t.repository_operations).where(ro.task_id == task_id).order_by(ro.created_at)).all()]


def _cancel(conn: Connection, now: datetime, oid: UUID, reason: str) -> None:
    ro = t.repository_operations.c
    n = conn.execute(sa.update(t.repository_operations).where(ro.id == oid, ro.state.in_(["requested", "claimed"]))
                     .values(state="cancelled", cancel_requested=True, error=reason[:200], lease_expires_at=None,
                             revision=ro.revision + 1, updated_at=now)).rowcount
    if n:
        _event(conn, now, _op(conn, oid))


def cancel_operation(conn: Connection, now: datetime, p: Principal, oid: UUID) -> RepositoryOperation:
    from .work import readable_task

    row = _op(conn, oid)
    readable_task(conn, p, row.task_id, "editor")
    _cancel(conn, now, oid, "cancelled by requester")
    return op_contract(_op(conn, oid))


def cancel_for_task(conn: Connection, now: datetime, task_id: UUID) -> None:
    ro = t.repository_operations.c
    for oid in conn.execute(sa.select(ro.id).where(ro.task_id == task_id, ro.state.in_(["requested", "claimed"]))).scalars():
        _cancel(conn, now, oid, "task cancelled")


# ------------------------------------------------------------- executor side


def claim(conn: Connection, now: datetime, p: Principal, bid: UUID) -> RepositoryOperationAssignment | None:
    b = _executor_binding(conn, p, bid)
    ro = t.repository_operations.c
    tk = t.tasks.c
    rows = conn.execute(sa.select(t.repository_operations).join(t.tasks, tk.id == ro.task_id).where(
        ro.binding_id == bid, ro.state == "requested", ro.cancel_requested.is_(False),
        tk.state.not_in(["done", "cancelled", "failed"])).order_by(ro.created_at).limit(10)).all()
    for r in rows:
        if r.kind == "open_draft_pr":  # a PR needs its branch prepared first (same task + head branch)
            ready = conn.execute(sa.select(ro.id).where(
                ro.task_id == r.task_id, ro.binding_id == bid, ro.kind == "prepare_branch", ro.state == "succeeded")).all()
            if not any(_op(conn, x.id).params["head_branch"] == r.params["head_branch"] for x in ready):
                continue
        attempt = new_id()
        n = conn.execute(sa.update(t.repository_operations).where(ro.id == r.id, ro.state == "requested",
                                                                  ro.fence == r.fence).values(
            state="claimed", fence=ro.fence + 1, attempt_id=attempt, lease_expires_at=now + LEASE,
            attempts=ro.attempts + 1, revision=ro.revision + 1, updated_at=now)).rowcount
        if n != 1:
            continue
        row = _op(conn, r.id)
        _event(conn, now, row)
        return RepositoryOperationAssignment(
            operation=op_contract(row), attempt_id=attempt, fencing_token=row.fence, lease_expires_at=now + LEASE,
            reconcile=row.reconcile_required, owner=b.owner, repo=b.repo, repository_id=b.repository_id,
            installation_id=b.installation_id, default_branch=b.default_branch)
    return None


def _fenced(conn: Connection, now: datetime, p: Principal, oid: UUID, proof: RepositoryOperationProof):  # noqa: ANN202
    row = _op(conn, oid)
    _executor_binding(conn, p, row.binding_id)
    if row.state == "cancelled":
        raise DomainError(ErrorCode.invalid_transition, "operation cancelled")
    if (row.state != "claimed" or row.attempt_id != proof.attempt_id or row.fence != proof.fencing_token
            or row.lease_expires_at is None or row.lease_expires_at <= now):
        raise DomainError(ErrorCode.stale_attempt, "attempt superseded or expired")
    return row


def heartbeat(conn: Connection, now: datetime, p: Principal, oid: UUID,
              proof: RepositoryOperationProof) -> RepositoryOperationHeartbeatResponse:
    row = _op(conn, oid)
    _executor_binding(conn, p, row.binding_id)
    if row.state == "cancelled" and row.attempt_id == proof.attempt_id:
        return RepositoryOperationHeartbeatResponse(lease_expires_at=now, cancel_requested=True)
    _fenced(conn, now, p, oid, proof)
    ro = t.repository_operations.c
    conn.execute(sa.update(t.repository_operations).where(ro.id == oid, ro.attempt_id == proof.attempt_id)
                 .values(lease_expires_at=now + LEASE))
    return RepositoryOperationHeartbeatResponse(lease_expires_at=now + LEASE, cancel_requested=False)


_PR_URL = "https://github.com/{owner}/{repo}/pull/{n}"


def complete(conn: Connection, now: datetime, p: Principal, oid: UUID,
             body: CompleteRepositoryOperation) -> RepositoryOperation:
    row = _fenced(conn, now, p, oid, body)
    b = _binding(conn, row.binding_id)
    ev = body.evidence
    if body.outcome == "succeeded":
        if row.kind == "prepare_branch" and ev.head_commit is None:
            raise DomainError(ErrorCode.validation_error, "prepare_branch evidence needs head_commit")
        if row.kind == "open_draft_pr":
            if ev.pr_number is None or ev.pr_url is None:
                raise DomainError(ErrorCode.validation_error, "open_draft_pr evidence needs pr_number and pr_url")
            expected = _PR_URL.format(owner=b.owner, repo=b.repo, n=ev.pr_number)
            if ev.pr_url.lower() != expected.lower():
                raise DomainError(ErrorCode.validation_error, "pr_url must point at the bound repository")
    if ev.pr_url and not re.match(r"^https://github\.com/", ev.pr_url):
        raise DomainError(ErrorCode.validation_error, "unexpected evidence URL")
    ro = t.repository_operations.c
    cas(conn, sa.update(t.repository_operations).where(ro.id == oid, ro.attempt_id == body.attempt_id,
                                                       ro.fence == body.fencing_token, ro.state == "claimed")
        .values(state=body.outcome, evidence=ev.model_dump(mode="json"), error=body.error, lease_expires_at=None,
                reconcile_required=False, revision=ro.revision + 1, updated_at=now),
        "repository operation", ErrorCode.stale_attempt)
    new = _op(conn, oid)
    _event(conn, now, new)
    return op_contract(new)


def expire(conn: Connection, now: datetime) -> int:
    """Lapsed executor leases: the executor may have acted, so require reconciliation
    on the next attempt; fail visibly after max_attempts."""
    ro = t.repository_operations.c
    rows = conn.execute(sa.select(t.repository_operations).where(ro.state == "claimed", ro.lease_expires_at < now)).all()
    for r in rows:
        retry = r.attempts < r.max_attempts
        conn.execute(sa.update(t.repository_operations).where(ro.id == r.id, ro.attempt_id == r.attempt_id).values(
            state="requested" if retry else "failed", reconcile_required=True, attempt_id=None, lease_expires_at=None,
            error=None if retry else "executor lost repeatedly; reconcile manually",
            revision=ro.revision + 1, updated_at=now))
        _event(conn, now, _op(conn, r.id))
    return len(rows)
