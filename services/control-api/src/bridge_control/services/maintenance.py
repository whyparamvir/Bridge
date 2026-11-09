"""Periodic deterministic maintenance: lease reapers, reservation expiry, delegation
rescheduling from the outbox, and event retention. Safe to run from several processes:
every step is a compare-and-set or a CAS-claimed outbox item."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..reliability import claim_outbox, complete_outbox, fail_outbox, prune_events
from . import Ctx, delegation, inference, repository, scheduling, work


@dataclass
class TickReport:
    task_leases: int = 0
    job_leases: int = 0
    delegations: int = 0
    inference: int = 0
    repository_operations: int = 0
    reservations: int = 0
    scheduled: int = 0
    pruned_events: int = 0


def tick(ctx: Ctx, worker_name: str = "maintenance") -> TickReport:
    rep = TickReport()
    now = ctx.now()
    with ctx.db.tx() as conn:
        rep.task_leases = work.expire_task_leases(conn, now)
    with ctx.db.tx() as conn:
        rep.job_leases = scheduling.expire_jobs(conn, now)
    with ctx.db.tx() as conn:
        rep.delegations = delegation.expire(conn, now)
    with ctx.db.tx() as conn:
        rep.inference = inference.expire(conn, now)
    with ctx.db.tx() as conn:
        rep.repository_operations = repository.expire(conn, now)
    with ctx.db.tx() as conn:
        rep.reservations = scheduling.expire_reservations(conn, now)
    with ctx.db.tx() as conn:
        items = claim_outbox(conn, now, worker_name, ["delegation.schedule"], limit=50)
    for item in items:
        try:
            with ctx.db.tx() as conn:
                delegation.schedule(conn, ctx.now(), item.subject_id)
                complete_outbox(conn, ctx.now(), item.seq)
            rep.scheduled += 1
        except Exception as e:  # noqa: BLE001 - recorded, retried with backoff
            with ctx.db.tx() as conn:
                fail_outbox(conn, ctx.now(), item.seq, repr(e), item.attempts + 1)
    with ctx.db.tx() as conn:
        rep.pruned_events = prune_events(conn, now, timedelta(hours=ctx.settings.event_retention_hours))
    return rep
