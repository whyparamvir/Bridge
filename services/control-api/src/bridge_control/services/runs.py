"""Runs: durable orchestration records driven by the agent runtime.

- Creation reserves the run's whole budget atomically and enqueues `run.start` in the
  SAME transaction (transactional outbox).
- A runner holds a lease on the run (runner_id + runner_lease_until). If the process
  dies, the lease lapses and another runner resumes from the persisted checkpoint.
- Cancellation is recorded first, then propagated to tasks, delegations and jobs; the
  runner observes it between steps and before every provider call.
- Runs started in a PRIVATE conversation keep their progress events private to the
  requester and create private tasks unless `publish_tasks` is set.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.common import Money
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import RunProgress
from ..contracts.work import Run, RunStatus, TaskState
from ..core import Principal, cas, new_id, not_found, require_role
from ..db import tables as t
from ..domain.state_machines import RUN, TASK
from ..reliability import emit, enqueue
from . import budget
from .work import readable_conversation, system_transition
from .workspace import owned_agent

RUNNER_LEASE = timedelta(seconds=60)
MAX_RUN_ATTEMPTS = 3


def contract(conn: Connection, r) -> Run:  # noqa: ANN001
    task_ids = conn.execute(sa.select(t.tasks.c.id).where(t.tasks.c.run_id == r.id)
                            .order_by(t.tasks.c.created_at)).scalars().all()
    return Run(id=r.id, project_id=r.project_id, conversation_id=r.conversation_id,
               requested_by_user_id=r.requested_by_user_id, orchestrator_agent_id=r.orchestrator_agent_id,
               status=RunStatus(r.status), goal=r.goal, task_ids=list(task_ids),
               budget_limit=Money(amount_micros=r.budget_limit_micros, currency=r.currency),
               idempotency_key=r.idempotency_key, revision=r.revision,
               created_at=r.created_at, finished_at=r.finished_at)


def _row(conn: Connection, run_id: UUID):  # noqa: ANN201
    row = conn.execute(sa.select(t.runs).where(t.runs.c.id == run_id)).first()
    if row is None:
        raise not_found("run")
    return row


def private_audience(conn: Connection, row) -> UUID | None:  # noqa: ANN001
    conv = conn.execute(sa.select(t.conversations).where(t.conversations.c.id == row.conversation_id)).one()
    return conv.owner_user_id if conv.scope == "private" else None


def progress(conn: Connection, now: datetime, row, summary: str, *, task_id: UUID | None = None,  # noqa: ANN001
             model_endpoint_id: UUID | None = None) -> None:
    emit(conn, now, row.project_id, row.id, row.revision, RunProgress(
        status=RunStatus(row.status), summary=summary[:200], task_id=task_id, model_endpoint_id=model_endpoint_id),
        audience_user_id=private_audience(conn, row))


def readable(conn: Connection, p: Principal, run_id: UUID):  # noqa: ANN201
    row = _row(conn, run_id)
    require_role(conn, p, row.project_id, "viewer")
    readable_conversation(conn, p, row.conversation_id)  # private runs stay private
    return row


def create(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreateRun) -> Run:
    conv = readable_conversation(conn, p, body.conversation_id)
    if conv.project_id != project_id:
        raise not_found("conversation")
    require_role(conn, p, project_id, "editor")
    agent_id = p.agent_id if p.kind == "agent" else body.orchestrator_agent_id
    if agent_id is None:
        raise DomainError(ErrorCode.validation_error, "orchestrator_agent_id required")
    owned_agent(conn, p, agent_id)
    rid = new_id()
    conn.execute(sa.insert(t.runs).values(
        id=rid, project_id=project_id, conversation_id=body.conversation_id, requested_by_user_id=p.user_id,
        orchestrator_agent_id=agent_id, status="queued", goal=body.goal, currency=body.budget_limit.currency,
        budget_limit_micros=body.budget_limit.amount_micros,
        idempotency_key=body.idempotency_key,
        checkpoint={"publish_tasks": body.publish_tasks,
                    "model_endpoint_id": str(body.model_endpoint_id) if body.model_endpoint_id else None,
                    "max_tasks": body.max_tasks},
        created_at=now))
    brid = budget.reserve(conn, now, project_id, body.budget_limit, subject_kind="run", subject_id=rid,
                          payer_user_id=p.user_id)
    conn.execute(sa.update(t.runs).where(t.runs.c.id == rid).values(budget_reservation_id=brid))
    enqueue(conn, now, "run.start", rid)
    row = _row(conn, rid)
    progress(conn, now, row, "Queued")
    return contract(conn, row)


def get(conn: Connection, p: Principal, run_id: UUID) -> Run:
    return contract(conn, readable(conn, p, run_id))


def list_for_project(conn: Connection, p: Principal, project_id: UUID, limit: int) -> list[Run]:
    require_role(conn, p, project_id, "viewer")
    r, c = t.runs.c, t.conversations.c
    rows = conn.execute(sa.select(t.runs).join(t.conversations, c.id == r.conversation_id).where(
        r.project_id == project_id, sa.or_(c.scope == "project", c.owner_user_id == p.user_id)
    ).order_by(r.created_at).limit(limit)).all()
    return [contract(conn, x) for x in rows]


def set_status(conn: Connection, now: datetime, run_id: UUID, dst: RunStatus, summary: str,
               *, blocked_reason: str | None = None, runner_id: str | None = None) -> sa.Row:
    row = _row(conn, run_id)
    if RunStatus(row.status) is dst:
        return row
    from ..contracts.common import ActorKind

    RUN.check(RunStatus(row.status), dst, ActorKind.system)
    rc = t.runs.c
    cond = [rc.id == run_id, rc.revision == row.revision]
    if runner_id is not None:
        cond.append(rc.runner_id == runner_id)
    values = {"status": dst.value, "revision": rc.revision + 1, "blocked_reason": blocked_reason}
    if dst in (RunStatus.complete, RunStatus.failed, RunStatus.cancelled):
        values.update(finished_at=now, runner_id=None, runner_lease_until=None)
    cas(conn, sa.update(t.runs).where(*cond).values(**values), "run")
    row = _row(conn, run_id)
    progress(conn, now, row, summary)
    if dst in (RunStatus.complete, RunStatus.failed, RunStatus.cancelled) and row.budget_reservation_id:
        budget.release(conn, now, row.budget_reservation_id)
    return row


def cancel(conn: Connection, now: datetime, p: Principal | None, run_id: UUID) -> Run:
    """Records the request, cancels queued/blocked work immediately and propagates to
    tasks, delegations and jobs. A runner mid-provider-call observes the flag after the
    call returns (in-flight HTTP calls are not interruptible; their usage is still
    charged and recorded)."""
    from . import delegation, scheduling

    row = readable(conn, p, run_id) if p is not None else _row(conn, run_id)
    if p is not None:
        require_role(conn, p, row.project_id, "editor")
    if RunStatus(row.status) in RUN.terminal:
        return contract(conn, row)
    conn.execute(sa.update(t.runs).where(t.runs.c.id == run_id).values(cancel_requested=True))
    tc = t.tasks.c
    for task in conn.execute(sa.select(t.tasks).where(tc.run_id == run_id)).all():
        if TaskState.cancelled in TASK.targets(TaskState(task.state)):
            system_transition(conn, now, task.id, TaskState.cancelled)
        for d in conn.execute(sa.select(t.delegations.c.id).where(t.delegations.c.task_id == task.id)).scalars():
            delegation.cancel(conn, now, None, d)
        for j in conn.execute(sa.select(t.jobs.c.id).where(t.jobs.c.task_id == task.id)).scalars():
            scheduling.cancel_job(conn, now, j)
    set_status(conn, now, run_id, RunStatus.cancelled, "Cancelled")
    return contract(conn, _row(conn, run_id))


def accept(conn: Connection, now: datetime, p: Principal, run_id: UUID) -> Run:
    """Human acceptance of a run in needs_review: its review tasks become done."""
    from ..contracts.api import TransitionTask
    from ..core import require_human
    from .work import task_contract, transition_task

    require_human(p, "accepting run results")
    row = readable(conn, p, run_id)
    require_role(conn, p, row.project_id, "editor")
    if row.status != "needs_review":
        raise DomainError(ErrorCode.invalid_transition, f"run is {row.status}")
    for task in conn.execute(sa.select(t.tasks).where(t.tasks.c.run_id == run_id, t.tasks.c.state == "review")).all():
        tc = task_contract(conn, task)
        transition_task(conn, now, p, task.id, TransitionTask(to_state=TaskState.done, expected_revision=tc.revision))
    from ..contracts.common import ActorKind

    RUN.check(RunStatus.needs_review, RunStatus.complete, ActorKind.human)
    rc = t.runs.c
    cas(conn, sa.update(t.runs).where(rc.id == run_id, rc.status == "needs_review")
        .values(status="complete", finished_at=now, revision=rc.revision + 1), "run")
    row = _row(conn, run_id)
    progress(conn, now, row, "Complete: accepted by requester")
    if row.budget_reservation_id:
        budget.release(conn, now, row.budget_reservation_id)
    return contract(conn, row)


# --------------------------------------------------------------------- runner API


def claim_for_runner(conn: Connection, now: datetime, run_id: UUID, runner_id: str) -> sa.Row | None:
    """Acquire (or take over an expired) runner lease. Bounded: a run whose runners keep
    dying fails visibly after MAX_RUN_ATTEMPTS."""
    rc = t.runs.c
    row = _row(conn, run_id)
    if row.status not in ("queued", "working") or row.cancel_requested:
        return None
    if row.attempt_count >= MAX_RUN_ATTEMPTS:
        set_status(conn, now, run_id, RunStatus.failed, "Failed: runner restarted too many times")
        return None
    n = conn.execute(sa.update(t.runs).where(
        rc.id == run_id, rc.status.in_(["queued", "working"]),
        sa.or_(rc.runner_lease_until.is_(None), rc.runner_lease_until < now, rc.runner_id == runner_id),
    ).values(runner_id=runner_id, runner_lease_until=now + RUNNER_LEASE, attempt_count=rc.attempt_count + 1)).rowcount
    if n != 1:
        return None
    row = _row(conn, run_id)
    conn.execute(sa.insert(t.run_attempts).values(
        id=new_id(), run_id=run_id, attempt_number=row.attempt_count, runner_id=runner_id,
        outcome="in_progress", started_at=now))
    if row.status == "queued":
        row = set_status(conn, now, run_id, RunStatus.working, "Planning")
    else:
        progress(conn, now, row, f"Resumed by runner (attempt {row.attempt_count})")
    return row


def checkpoint(conn: Connection, now: datetime, run_id: UUID, runner_id: str, state: dict) -> sa.Row:
    """Persist progress; fails with stale_attempt if this runner lost its lease."""
    rc = t.runs.c
    row = _row(conn, run_id)
    merged = {**row.checkpoint, **state}
    cas(conn, sa.update(t.runs).where(rc.id == run_id, rc.runner_id == runner_id, rc.runner_lease_until > now)
        .values(checkpoint=merged, runner_lease_until=now + RUNNER_LEASE), "runner lease", ErrorCode.stale_attempt)
    return _row(conn, run_id)


def end_attempt(conn: Connection, now: datetime, run_id: UUID, runner_id: str, outcome: str,
                error_code: str | None = None) -> None:
    ra = t.run_attempts.c
    conn.execute(sa.update(t.run_attempts).where(ra.run_id == run_id, ra.runner_id == runner_id,
                                                 ra.outcome == "in_progress")
                 .values(outcome=outcome, error_code=error_code, ended_at=now))


def runnable(conn: Connection, now: datetime) -> list[UUID]:
    """Runs a runner may pick up: queued, or working with a lapsed runner lease."""
    rc = t.runs.c
    return list(conn.execute(sa.select(rc.id).where(
        rc.status.in_(["queued", "working"]), rc.cancel_requested.is_(False),
        sa.or_(rc.runner_lease_until.is_(None), rc.runner_lease_until < now)).order_by(rc.created_at)).scalars())
