"""Pure, deterministic policy checks. The database layer (O5/O8) wraps these in
atomic transactions; these functions decide *whether* an action is allowed."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..contracts.common import ResourceVector
from ..contracts.compute import (
    DelegationBounds,
    DelegationRequest,
    DelegationState,
    Lease,
    ReservationKind,
    ResourceReservation,
    ResourceReservationState,
    SharingPolicy,
    Worker,
)
from ..contracts.errors import DomainError, ErrorCode

HEARTBEAT_INTERVAL = timedelta(seconds=10)
STALE_AFTER = timedelta(seconds=30)

# ------------------------------------------------------------------------ fencing


def verify_fence(lease: Lease, attempt_id, fencing_token: int, now: datetime) -> None:  # noqa: ANN001
    """A completion/heartbeat is valid only for the current, unexpired, unreleased
    attempt. A late result from an older attempt can never overwrite a newer one."""
    if lease.attempt_id != attempt_id or lease.fencing_token != fencing_token:
        raise DomainError(ErrorCode.stale_attempt, "attempt superseded")
    if lease.released_at is not None or now >= lease.expires_at:
        raise DomainError(ErrorCode.stale_attempt, "lease expired or released")


def next_fencing_token(previous: int | None) -> int:
    return 1 if previous is None else previous + 1


# --------------------------------------------------------------------- eligibility


def device_fresh(worker: Worker, now: datetime) -> bool:
    """`last_heartbeat_at` is server receipt time. A future value (clock bug, tampering)
    is never fresh; age must be in [0, STALE_AFTER)."""
    if worker.last_heartbeat_at is None:
        return False
    age = now - worker.last_heartbeat_at
    return timedelta(0) <= age < STALE_AFTER


def eligible_for_new_work(
    worker: Worker,
    policy: SharingPolicy,
    *,
    project_id,  # noqa: ANN001
    kind: ReservationKind,
    job_type: str | None,
    on_ac: bool,
    interactive_use: bool,
    now: datetime,
) -> tuple[bool, str]:
    """Return (eligible, reason). Opted-out, paused, stale, revoked or on-battery
    devices never receive *new* work. Being powered on alone is insufficient."""
    if worker.revoked_at is not None:
        return False, "worker revoked"
    if policy.worker_id != worker.id or policy.owner_user_id != worker.owner_user_id:
        return False, "policy does not belong to worker owner"
    if not device_fresh(worker, now):
        return False, "stale heartbeat"
    if policy.paused:
        return False, "paused"
    flag = {
        ReservationKind.job: policy.share_compute,
        ReservationKind.inference: policy.share_local_inference,
        ReservationKind.delegation: policy.accept_delegated_tasks,
    }[kind]
    if not flag:
        return False, f"{kind.value} sharing not enabled"
    if project_id not in policy.allowed_project_ids:
        return False, "project not allowed"
    if kind is ReservationKind.job and (job_type is None or job_type not in policy.allowed_job_types):
        return False, "job type not allowed"
    if worker.isolation == "none" and kind is not ReservationKind.inference:
        return False, "no isolation boundary"
    if not on_ac and not policy.run_on_battery:
        return False, "on battery"
    if interactive_use and policy.pause_during_interactive_use:
        return False, "owner is using device"
    if policy.schedule and not in_schedule(policy, worker.timezone, now):
        return False, "outside schedule"
    return True, "ok"


def in_schedule(policy: SharingPolicy, tz: str, now: datetime) -> bool:
    """Convert the UTC instant to the device's IANA zone, then match local wall-clock
    windows (half-open). See TimeWindow for the DST rule."""
    local = now.astimezone(ZoneInfo(tz))
    minute = local.hour * 60 + local.minute
    return any(
        w.weekday == local.weekday() and w.start_minute <= minute < w.end_minute
        for w in policy.schedule
    )


# ------------------------------------------------------------------------ capacity

_LIVE = {ResourceReservationState.held, ResourceReservationState.active}


def committed(reservations: list[ResourceReservation], worker_id, now: datetime) -> ResourceVector:  # noqa: ANN001
    total = ResourceVector()
    for r in reservations:
        if r.worker_id == worker_id and r.state in _LIVE and r.expires_at > now:
            total = total + r.resources
    return total


COUNTED_KINDS = frozenset({ReservationKind.job, ReservationKind.delegation})


def check_capacity(
    worker: Worker,
    policy: SharingPolicy,
    live: list[ResourceReservation],
    request: ResourceVector,
    now: datetime,
    kind: ReservationKind,
) -> None:
    """Inference and execution share ONE resource pool: the request plus every live
    reservation of any kind must fit the shared limit (which must fit the device ceiling).
    `max_concurrent_jobs` counts jobs + delegations only, and applies only when the
    incoming request is itself a job or delegation."""
    if not policy.shared_limits.fits_within(worker.device_limits):
        raise DomainError(ErrorCode.policy_denied, "shared limits exceed device limits")
    if kind in COUNTED_KINDS:
        jobs_running = sum(
            1
            for r in live
            if r.worker_id == worker.id
            and r.state in _LIVE
            and r.expires_at > now
            and r.kind in COUNTED_KINDS
        )
        if jobs_running >= policy.max_concurrent_jobs:
            raise DomainError(ErrorCode.capacity_unavailable, "concurrency limit reached")
    after = committed(live, worker.id, now) + request
    if not after.fits_within(policy.shared_limits):
        raise DomainError(ErrorCode.capacity_unavailable, "would exceed shared limits")


# ---------------------------------------------------------------------- delegation


def narrow_bounds(parent: DelegationBounds, child: DelegationBounds) -> DelegationBounds:
    """Delegation may never widen permissions, budget, deadline or depth."""
    problems = []
    if parent.remaining_depth < 1:
        problems.append("delegation depth exhausted")
    if child.remaining_depth > parent.remaining_depth - 1:
        problems.append("depth not reduced")
    if child.budget.currency != parent.budget.currency:
        problems.append("currency mismatch")
    if child.budget.amount_micros > parent.budget.amount_micros:
        problems.append("budget widened")
    if child.deadline > parent.deadline:
        problems.append("deadline extended")
    if not set(child.allowed_tools) <= set(parent.allowed_tools):
        problems.append("tools widened")
    if not set(child.allowed_model_endpoint_ids) <= set(parent.allowed_model_endpoint_ids):
        problems.append("models widened")
    if "private" in (child.data_visibility.value, parent.data_visibility.value):
        problems.append("private-scope data cannot be delegated")
    else:
        order = ["run", "task", "project"]  # narrower first
        if order.index(child.data_visibility.value) > order.index(parent.data_visibility.value):
            problems.append("visibility widened")
    if problems:
        raise DomainError(ErrorCode.delegation_bounds_exceeded, "; ".join(problems))
    return child


def state_after_decline(d: DelegationRequest) -> DelegationState:
    """Decline -> bounded alternate scheduling, else a visible terminal outcome."""
    return (
        DelegationState.requested if d.decline_count + 1 <= d.max_alternates
        else DelegationState.declined
    )
