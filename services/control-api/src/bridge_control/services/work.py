"""Conversations, messages, tasks (claims, fenced transitions, lease expiry), decisions
and structured agent messages."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from pydantic import TypeAdapter
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts import api as A
from ..contracts.common import ActorKind, ActorRef, Money, Visibility
from ..contracts.compute import Lease, LeaseSubject
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import AgentMessagePosted, TaskUpdated
from ..contracts.messaging import AgentMessage, AgentMessagePayload, AgentMessageType
from ..contracts.work import (
    Conversation, ConversationScope, Decision, DecisionStatus, Message, MessageRole, Task, TaskState,
)
from ..core import Principal, cas, new_id, not_found, require_role
from ..db import tables as t
from ..domain.state_machines import TASK
from ..reliability import emit
from .workspace import actor_of, owned_agent

MAX_AGENT_MESSAGE_DEPTH = 4
MAX_MESSAGES_PER_CORRELATION = 50
DEFAULT_LEASE = timedelta(seconds=120)

# ----------------------------------------------------------------- conversations


def _conv(r) -> Conversation:  # noqa: ANN001
    return Conversation(id=r.id, project_id=r.project_id, scope=ConversationScope(r.scope),
                        owner_user_id=r.owner_user_id, created_at=r.created_at)


def create_conversation(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                        body: A.CreateConversation) -> Conversation:
    require_role(conn, p, project_id, "viewer" if body.scope is ConversationScope.private else "editor")
    cid = new_id()
    owner = p.user_id if body.scope is ConversationScope.private else None
    conn.execute(sa.insert(t.conversations).values(id=cid, project_id=project_id, scope=body.scope.value,
                                                   owner_user_id=owner, created_at=now))
    return _conv(conn.execute(sa.select(t.conversations).where(t.conversations.c.id == cid)).one())


def readable_conversation(conn: Connection, p: Principal, conversation_id: UUID):  # noqa: ANN201
    """Private conversations are visible only to their owner (and the owner's agents);
    for everyone else they do not exist."""
    row = conn.execute(sa.select(t.conversations).where(t.conversations.c.id == conversation_id)).first()
    if row is None:
        raise not_found("conversation")
    require_role(conn, p, row.project_id, "viewer")
    if row.scope == "private" and row.owner_user_id != p.user_id:
        raise not_found("conversation")
    return row


def list_conversations(conn: Connection, p: Principal, project_id: UUID) -> list[Conversation]:
    require_role(conn, p, project_id, "viewer")
    c = t.conversations.c
    rows = conn.execute(sa.select(t.conversations).where(
        c.project_id == project_id, sa.or_(c.scope == "project", c.owner_user_id == p.user_id)
    ).order_by(c.created_at)).all()
    return [_conv(r) for r in rows]


def _msg(r) -> Message:  # noqa: ANN001
    return Message(id=r.id, conversation_id=r.conversation_id, project_id=r.project_id,
                   role=MessageRole(r.role), author=ActorRef(kind=ActorKind(r.author_kind), id=r.author_id),
                   body=r.body, artifact_refs=r.artifact_refs, created_at=r.created_at)


def post_message(conn: Connection, now: datetime, p: Principal, conversation_id: UUID,
                 body: A.PostMessage, role: MessageRole = MessageRole.user) -> Message:
    conv = readable_conversation(conn, p, conversation_id)
    if conv.scope == "project":
        require_role(conn, p, conv.project_id, "editor")
    kind, aid = actor_of(p)
    if p.kind == "agent":
        role = MessageRole.agent
    mid = new_id()
    conn.execute(sa.insert(t.messages).values(
        id=mid, conversation_id=conversation_id, project_id=conv.project_id, role=role.value,
        author_kind=kind, author_id=aid, body=body.body, artifact_refs=[str(a) for a in body.artifact_refs],
        created_at=now))
    return _msg(conn.execute(sa.select(t.messages).where(t.messages.c.id == mid)).one())


def list_messages(conn: Connection, p: Principal, conversation_id: UUID, after: datetime | None,
                  limit: int) -> list[Message]:
    readable_conversation(conn, p, conversation_id)
    m = t.messages.c
    q = sa.select(t.messages).where(m.conversation_id == conversation_id)
    if after is not None:
        q = q.where(m.created_at > after)
    return [_msg(r) for r in conn.execute(q.order_by(m.created_at, m.id).limit(limit)).all()]


# ---------------------------------------------------------------------------- tasks


def task_contract(conn: Connection, r) -> Task:  # noqa: ANN001
    deps = conn.execute(sa.select(t.task_dependencies.c.depends_on_task_id)
                        .where(t.task_dependencies.c.task_id == r.id)).scalars().all()
    leased = r.state in ("leased", "running", "verifying") and r.attempt_id is not None
    return Task(
        id=r.id, project_id=r.project_id, title=r.title, description=r.description,
        acceptance_criteria=r.acceptance_criteria, dependencies=list(deps),
        owner_agent_id=r.owner_agent_id, state=TaskState(r.state), blocked_reason=r.blocked_reason,
        revision=r.revision, lease_id=r.attempt_id if leased else None,
        lease_token=r.fence if leased else None,
        budget_limit=Money(amount_micros=r.budget_limit_micros, currency=r.currency),
        payer_user_id=r.payer_user_id, visibility=Visibility(r.visibility),
        artifact_refs=[UUID(a) for a in r.artifact_refs], created_at=r.created_at, updated_at=r.updated_at)


def _visible(q, p: Principal):  # noqa: ANN001, ANN202
    c = t.tasks.c
    return q.where(sa.or_(c.visibility != "private", c.creator_user_id == p.user_id))


def readable_task(conn: Connection, p: Principal, task_id: UUID, minimum: str = "viewer"):  # noqa: ANN201
    row = conn.execute(sa.select(t.tasks).where(t.tasks.c.id == task_id)).first()
    if row is None:
        raise not_found("task")
    require_role(conn, p, row.project_id, minimum)
    if row.visibility == "private" and row.creator_user_id != p.user_id:
        raise not_found("task")
    return row


def _audience(row) -> UUID | None:  # noqa: ANN001
    return row.creator_user_id if row.visibility == "private" else None


def _task_event(conn: Connection, now: datetime, row, previous: str | None,  # noqa: ANN001
                created_by_agent_id: UUID | None = None) -> None:
    emit(conn, now, row.project_id, row.id, row.revision,
         TaskUpdated(state=TaskState(row.state), previous_state=TaskState(previous) if previous else None,
                     owner_agent_id=row.owner_agent_id, blocked_reason=row.blocked_reason,
                     created_by_agent_id=created_by_agent_id),
         audience_user_id=_audience(row))


def _reload(conn: Connection, task_id: UUID):  # noqa: ANN201
    return conn.execute(sa.select(t.tasks).where(t.tasks.c.id == task_id)).one()


def create_task(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreateTask,
                *, run_id: UUID | None = None, visibility: Visibility = Visibility.project) -> Task:
    require_role(conn, p, project_id, "editor")
    if visibility is Visibility.private and p.kind == "worker":
        raise DomainError(ErrorCode.permission_denied, "workers cannot create tasks")
    deps = list(dict.fromkeys(body.dependencies))
    for d in deps:
        dep = conn.execute(sa.select(t.tasks.c.project_id).where(t.tasks.c.id == d)).first()
        if dep is None or dep.project_id != project_id:
            raise DomainError(ErrorCode.validation_error, f"dependency {d} not in project")
    tid = new_id()
    conn.execute(sa.insert(t.tasks).values(
        id=tid, project_id=project_id, run_id=run_id, creator_user_id=p.user_id, title=body.title,
        description=body.description, acceptance_criteria=body.acceptance_criteria,
        state="backlog", currency=body.budget_limit.currency,
        budget_limit_micros=body.budget_limit.amount_micros, payer_user_id=p.user_id,
        visibility=visibility.value, artifact_refs=[], created_at=now, updated_at=now))
    for d in deps:
        conn.execute(sa.insert(t.task_dependencies).values(task_id=tid, depends_on_task_id=d))
    row = _reload(conn, tid)
    _task_event(conn, now, row, None, p.agent_id if p.kind == "agent" else None)
    promote_ready(conn, now, project_id)
    return task_contract(conn, _reload(conn, tid))


def promote_ready(conn: Connection, now: datetime, project_id: UUID, *, run_id: UUID | None = None) -> int:
    """SYSTEM transition backlog -> ready for tasks whose dependencies are all done.

    With `run_id`, only that run's tasks are considered and a dependency that is in
    `review` (verified by the runtime, awaiting human acceptance) also counts, so a run
    can proceed while humans still have the final say on every task."""
    c = t.tasks.c
    dep = t.task_dependencies.alias("d")
    blocker = t.tasks.alias("b")
    ok_states = ["done", "review"] if run_id is not None else ["done"]
    pending = sa.select(dep.c.task_id).join(blocker, blocker.c.id == dep.c.depends_on_task_id).where(
        dep.c.task_id == c.id, blocker.c.state.not_in(ok_states))
    q = sa.select(c.id).where(c.project_id == project_id, c.state == "backlog", ~sa.exists(pending))
    if run_id is not None:
        q = q.where(c.run_id == run_id)
    ids = conn.execute(q).scalars().all()
    for tid in ids:
        n = conn.execute(sa.update(t.tasks).where(c.id == tid, c.state == "backlog")
                         .values(state="ready", revision=c.revision + 1, updated_at=now)).rowcount
        if n:
            _task_event(conn, now, _reload(conn, tid), "backlog")
    return len(ids)


def list_tasks(conn: Connection, p: Principal, project_id: UUID, state: TaskState | None,
               after_id: UUID | None, limit: int) -> list[Task]:
    require_role(conn, p, project_id, "viewer")
    c = t.tasks.c
    q = _visible(sa.select(t.tasks).where(c.project_id == project_id), p)
    if state is not None:
        q = q.where(c.state == state.value)
    if after_id is not None:
        anchor = conn.execute(sa.select(c.created_at).where(c.id == after_id)).scalar()
        if anchor is not None:
            q = q.where(sa.or_(c.created_at > anchor, sa.and_(c.created_at == anchor, c.id > after_id)))
    return [task_contract(conn, r) for r in conn.execute(q.order_by(c.created_at, c.id).limit(limit)).all()]


def _agent_for(conn: Connection, p: Principal, agent_id: UUID | None) -> UUID:
    if p.kind == "agent":
        if agent_id not in (None, p.agent_id):
            raise DomainError(ErrorCode.permission_denied, "agents claim only for themselves")
        return p.agent_id
    if agent_id is None:
        raise DomainError(ErrorCode.validation_error, "agent_id required")
    owned_agent(conn, p, agent_id)
    return agent_id


def claim_task(conn: Connection, now: datetime, p: Principal, task_id: UUID, body: A.ClaimTask) -> A.ClaimResponse:
    """Atomic: exactly one concurrent claimer of a ready task succeeds. The fence is
    incremented, never reset, and a new attempt_id is issued."""
    row = readable_task(conn, p, task_id, "editor")
    agent_id = _agent_for(conn, p, body.agent_id)
    c = t.tasks.c
    attempt = new_id()
    expires = now + timedelta(seconds=body.lease_seconds)
    n = conn.execute(sa.update(t.tasks).where(c.id == task_id, c.state == "ready").values(
        state="leased", fence=c.fence + 1, attempt_id=attempt, owner_agent_id=agent_id,
        lease_expires_at=expires, revision=c.revision + 1, updated_at=now)).rowcount
    if n != 1:
        raise DomainError(ErrorCode.lease_conflict, f"task is {row.state}, not ready")
    row = _reload(conn, task_id)
    _task_event(conn, now, row, "ready")
    lease = Lease(id=attempt, subject=LeaseSubject.task, subject_id=task_id,
                  holder=ActorRef(kind=ActorKind.agent, id=agent_id), attempt_id=attempt,
                  fencing_token=row.fence, acquired_at=now, expires_at=expires)
    return A.ClaimResponse(task=task_contract(conn, row), lease=lease)


def _lease_ok(row, attempt_id: UUID | None, token: int | None, now: datetime) -> bool:  # noqa: ANN001
    return (attempt_id is not None and token is not None and row.attempt_id == attempt_id
            and row.fence == token and row.lease_expires_at is not None and row.lease_expires_at > now)


def renew_task_lease(conn: Connection, now: datetime, p: Principal, task_id: UUID, body: A.RenewLease) -> Lease:
    row = readable_task(conn, p, task_id, "editor")
    if not _lease_ok(row, body.attempt_id, body.lease_token, now):
        raise DomainError(ErrorCode.stale_attempt, "lease superseded or expired")
    c = t.tasks.c
    expires = now + timedelta(seconds=body.lease_seconds)
    cas(conn, sa.update(t.tasks).where(c.id == task_id, c.attempt_id == body.attempt_id,
                                       c.fence == body.lease_token).values(lease_expires_at=expires),
        "lease", ErrorCode.stale_attempt)
    return Lease(id=row.attempt_id, subject=LeaseSubject.task, subject_id=task_id,
                 holder=ActorRef(kind=ActorKind.agent, id=row.owner_agent_id), attempt_id=row.attempt_id,
                 fencing_token=row.fence, acquired_at=now, expires_at=expires)


def transition_task(conn: Connection, now: datetime, p: Principal, task_id: UUID, body: A.TransitionTask,
                    *, actor: ActorKind | None = None) -> Task:
    """Validated by the server state machine. Leased transitions require the CURRENT
    attempt_id + fence and an unexpired lease; stale attempts get stale_attempt."""
    row = readable_task(conn, p, task_id, "editor")
    kind = actor or ActorKind(actor_of(p)[0])
    src, dst = TaskState(row.state), body.to_state
    rule = TASK.check(src, dst, kind, lease_ok=_lease_ok(row, body.attempt_id, body.lease_token, now))
    if rule.requires_lease and p.kind == "agent" and row.owner_agent_id != p.agent_id:
        raise DomainError(ErrorCode.stale_attempt, "only the lease holder may do this")
    c = t.tasks.c
    values: dict = {"state": dst.value, "revision": c.revision + 1, "updated_at": now,
                    "blocked_reason": body.reason if dst is TaskState.blocked else None}
    if dst in (TaskState.ready, TaskState.backlog, TaskState.cancelled, TaskState.failed, TaskState.done):
        values.update(attempt_id=None, lease_expires_at=None)
    if body.result_summary is not None:
        values["result_summary"] = body.result_summary
    if body.artifact_refs:
        values["artifact_refs"] = [str(a) for a in body.artifact_refs]
    cond = [c.id == task_id, c.revision == body.expected_revision, c.state == src.value]
    if rule.requires_lease:
        cond += [c.attempt_id == body.attempt_id, c.fence == body.lease_token]
    if conn.execute(sa.update(t.tasks).where(*cond).values(**values)).rowcount != 1:
        current = conn.execute(sa.select(c.revision).where(c.id == task_id)).scalar()
        raise DomainError(ErrorCode.revision_conflict, "task changed", current_revision=current)
    new = _reload(conn, task_id)
    _task_event(conn, now, new, src.value)
    if dst is TaskState.cancelled:
        from .repository import cancel_for_task

        cancel_for_task(conn, now, task_id)
    if dst is TaskState.done:
        promote_ready(conn, now, row.project_id)
    return task_contract(conn, new)


def system_transition(conn: Connection, now: datetime, task_id: UUID, dst: TaskState,
                      reason: str | None = None, **extra) -> bool:  # noqa: ANN003
    """Deterministic server transition (lease expiry, cancellation, retries exhausted)."""
    row = _reload(conn, task_id)
    TASK.check(TaskState(row.state), dst, ActorKind.system)
    c = t.tasks.c
    values = {"state": dst.value, "revision": c.revision + 1, "updated_at": now,
              "blocked_reason": reason if dst is TaskState.blocked else None, **extra}
    if dst in (TaskState.ready, TaskState.cancelled, TaskState.failed):
        values.update(attempt_id=None, lease_expires_at=None)
    n = conn.execute(sa.update(t.tasks).where(c.id == task_id, c.revision == row.revision)
                     .values(**values)).rowcount
    if n:
        _task_event(conn, now, _reload(conn, task_id), row.state)
        if dst is TaskState.cancelled:
            from .repository import cancel_for_task

            cancel_for_task(conn, now, task_id)
    return bool(n)


def expire_task_leases(conn: Connection, now: datetime) -> int:
    """Leased/running tasks whose lease expired return to ready (bounded retries) or fail."""
    c = t.tasks.c
    rows = conn.execute(sa.select(t.tasks).where(c.state.in_(["leased", "running"]),
                                                 c.lease_expires_at < now)).all()
    for r in rows:
        if r.retries >= r.max_retries and r.state == "running":
            system_transition(conn, now, r.id, TaskState.failed)
        else:
            system_transition(conn, now, r.id, TaskState.ready, retries=c.retries + 1)
    return len(rows)


# ------------------------------------------------------------------------ decisions


def _decision(r) -> Decision:  # noqa: ANN001
    return Decision(id=r.id, project_id=r.project_id, key=r.key, body=r.body,
                    status=DecisionStatus(r.status),
                    author=ActorRef(kind=ActorKind(r.author_kind), id=r.author_id),
                    source_refs=[UUID(s) for s in r.source_refs], supersedes_id=r.supersedes_id,
                    version=r.version, revision=r.revision, created_at=r.created_at)


def propose_decision(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                     body: A.ProposeDecision) -> Decision:
    require_role(conn, p, project_id, "editor")
    d = t.decisions.c
    version = 1
    if body.supersedes_id is not None:
        prev = conn.execute(sa.select(t.decisions).where(d.id == body.supersedes_id,
                                                         d.project_id == project_id)).first()
        if prev is None:
            raise not_found("decision")
        if prev.key != body.key:
            raise DomainError(ErrorCode.validation_error, "superseding decision must keep its key")
        version = prev.version + 1
    kind, aid = actor_of(p)
    did = new_id()
    conn.execute(sa.insert(t.decisions).values(
        id=did, project_id=project_id, key=body.key, body=body.body, status="proposed",
        author_kind=kind, author_id=aid, source_refs=[str(s) for s in body.source_refs],
        supersedes_id=body.supersedes_id, version=version, created_at=now))
    return _decision(conn.execute(sa.select(t.decisions).where(d.id == did)).one())


def accept_decision(conn: Connection, now: datetime, p: Principal, decision_id: UUID,
                    body: A.AcceptDecision) -> Decision:
    """Accepting a superseding decision supersedes its predecessor only if that
    predecessor is still at the expected revision. Two accepted decisions on one key
    without a supersedes link become a visible conflict, never last-write-wins."""
    d = t.decisions.c
    row = conn.execute(sa.select(t.decisions).where(d.id == decision_id)).first()
    if row is None:
        raise not_found("decision")
    require_role(conn, p, row.project_id, "editor")
    cas(conn, sa.update(t.decisions).where(d.id == decision_id, d.revision == body.expected_revision,
                                           d.status == "proposed")
        .values(status="accepted", revision=d.revision + 1), "decision")
    if row.supersedes_id is not None:
        n = conn.execute(sa.update(t.decisions).where(
            d.id == row.supersedes_id, d.status.in_(["accepted", "conflicted"]),
            d.revision == body.supersedes_expected_revision,
        ).values(status="superseded", revision=d.revision + 1)).rowcount
        if n != 1:
            raise DomainError(ErrorCode.revision_conflict, "superseded decision changed concurrently")
    others = conn.execute(sa.select(t.decisions.c.id).where(
        d.project_id == row.project_id, d.key == row.key, d.id != decision_id,
        d.status.in_(["accepted", "conflicted"]))).scalars().all()
    if others:
        conn.execute(sa.update(t.decisions).where(d.id.in_([*others, decision_id]))
                     .values(status="conflicted", revision=d.revision + 1))
    return _decision(conn.execute(sa.select(t.decisions).where(d.id == decision_id)).one())


def list_decisions(conn: Connection, p: Principal, project_id: UUID, key: str | None, limit: int,
                   offset: int) -> list[Decision]:
    require_role(conn, p, project_id, "viewer")
    d = t.decisions.c
    q = sa.select(t.decisions).where(d.project_id == project_id)
    if key:
        q = q.where(d.key == key)
    return [_decision(r) for r in conn.execute(q.order_by(d.created_at, d.id).offset(offset).limit(limit)).all()]


# ------------------------------------------------------------------- agent messages

_payload = TypeAdapter(AgentMessagePayload)


def _amsg(r) -> AgentMessage:  # noqa: ANN001
    return AgentMessage(message_id=r.id, project_id=r.project_id, sender_agent_id=r.sender_agent_id,
                        recipient_agent_id=r.recipient_agent_id, task_id=r.task_id,
                        correlation_id=r.correlation_id, type=AgentMessageType(r.type),
                        payload=r.payload, created_at=r.created_at, expires_at=r.expires_at,
                        deduplication_key=r.deduplication_key)


def post_agent_message(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                       body: A.PostAgentMessage) -> AgentMessage:
    require_role(conn, p, project_id, "editor")
    if p.kind != "agent":
        raise DomainError(ErrorCode.permission_denied, "agent messages are sent by agents")
    if body.recipient_agent_id == p.agent_id:
        raise DomainError(ErrorCode.validation_error, "That's this agent.")
    m = t.agent_messages.c
    dup = conn.execute(sa.select(t.agent_messages).where(
        m.sender_agent_id == p.agent_id, m.deduplication_key == body.deduplication_key)).first()
    if dup is not None:
        return _amsg(dup)
    recipient = conn.execute(sa.select(t.agent_profiles).where(t.agent_profiles.c.id == body.recipient_agent_id)).first()
    member = recipient and conn.execute(sa.select(t.memberships.c.id).where(
        t.memberships.c.project_id == project_id, t.memberships.c.user_id == recipient.owner_user_id,
        t.memberships.c.revoked_at.is_(None))).first()
    if not member:
        raise not_found("recipient agent")
    if body.task_id is not None:
        readable_task(conn, p, body.task_id)
    prior = conn.execute(sa.select(sa.func.count(), sa.func.max(m.depth)).where(
        m.project_id == project_id, m.correlation_id == body.correlation_id)).one()
    payload = _payload.validate_python(body.payload)
    count, depth = prior[0], (prior[1] + 1 if prior[1] is not None else 0)
    if payload.type == "note":
        depth = 0  # a note is not a delegation step
    if count >= MAX_MESSAGES_PER_CORRELATION or depth > MAX_AGENT_MESSAGE_DEPTH:
        raise DomainError(ErrorCode.delegation_bounds_exceeded, "conversation bound reached")
    mid = new_id()
    msg = AgentMessage(message_id=mid, project_id=project_id, sender_agent_id=p.agent_id,
                       recipient_agent_id=body.recipient_agent_id, task_id=body.task_id,
                       correlation_id=body.correlation_id, type=AgentMessageType(payload.type),
                       payload=payload, created_at=now, expires_at=body.expires_at,
                       deduplication_key=body.deduplication_key)
    try:
        conn.execute(sa.insert(t.agent_messages).values(
            id=mid, project_id=project_id, sender_agent_id=p.agent_id,
            recipient_agent_id=body.recipient_agent_id, task_id=body.task_id,
            correlation_id=body.correlation_id, type=payload.type,
            payload=payload.model_dump(mode="json"), depth=depth, created_at=now,
            expires_at=body.expires_at, deduplication_key=body.deduplication_key))
    except IntegrityError:
        return _amsg(conn.execute(sa.select(t.agent_messages).where(
            m.sender_agent_id == p.agent_id, m.deduplication_key == body.deduplication_key)).one())
    emit(conn, now, project_id, mid, 0, AgentMessagePosted(
        message_id=mid, sender_agent_id=p.agent_id, recipient_agent_id=body.recipient_agent_id,
        message_type=payload.type))
    return msg


def agent_inbox(conn: Connection, now: datetime, p: Principal, project_id: UUID, limit: int) -> list[AgentMessage]:
    require_role(conn, p, project_id, "viewer")
    if p.kind != "agent":
        raise DomainError(ErrorCode.permission_denied, "inbox is per agent")
    m = t.agent_messages.c
    rows = conn.execute(sa.select(t.agent_messages).where(
        m.project_id == project_id, m.recipient_agent_id == p.agent_id, m.expires_at > now
    ).order_by(m.created_at).limit(limit)).all()
    return [_amsg(r) for r in rows]
