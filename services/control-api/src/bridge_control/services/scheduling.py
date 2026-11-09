"""Worker enrollment, heartbeats, sharing policy + revocation, the joint capacity pool,
and deterministic remote jobs with fenced attempts.

Capacity invariant (section 20): for each worker, the sum of live reservations of ALL
kinds (job, delegation, inference) never exceeds the owner's shared limits, and jobs +
delegations never exceed max_concurrent_jobs. It is enforced by a single conditional
UPDATE on the worker row whose WHERE clause reads the policy limits, so concurrent
reservers cannot jointly over-commit on PostgreSQL or SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.common import ActorKind, ActorRef, ResourceVector
from ..contracts.compute import (
    AttemptFence, DeviceAvailability, HeartbeatResponse, SharingPolicySettings, Job, JobAttempt, JobAttemptState, JobState,
    Lease, LeaseSubject, PowerState, ReservationKind, ResourceReservation, ResourceReservationState,
    RevocationMode, SharingPolicy, SharingPolicyUpdate, Worker,
)
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import ReservationUpdated, WorkerUpdated
from ..core import Principal, cas, new_id, new_token, not_found, require_human, require_role, token_hash
from ..db import tables as t
from ..domain import policy as P
from ..reliability import emit

JOB_LEASE = timedelta(seconds=30)
ENROLLMENT_TTL = timedelta(minutes=15)
COUNTED = (ReservationKind.job.value, ReservationKind.delegation.value)

# --------------------------------------------------------------------- converters


def worker_contract(r) -> Worker:  # noqa: ANN001
    return Worker(
        id=r.id, workspace_id=r.workspace_id, owner_user_id=r.owner_user_id,
        display_name=r.display_name, os=r.os, arch=r.arch, isolation=r.isolation,
        runtime_images=r.runtime_images, gpu_model=r.gpu_model, timezone=r.timezone,
        device_limits=ResourceVector(cpu_millicores=r.limit_cpu, ram_mib=r.limit_ram,
                                     vram_mib=r.limit_vram, disk_mib=r.limit_disk),
        enrolled_at=r.enrolled_at, last_heartbeat_at=r.last_heartbeat_at, revoked_at=r.revoked_at)


def policy_contract(r) -> SharingPolicy:  # noqa: ANN001
    return SharingPolicy.model_validate({**r.body, "revision": r.revision, "updated_at": r.updated_at})


def _reservation(r) -> ResourceReservation:  # noqa: ANN001
    return ResourceReservation(
        id=r.id, worker_id=r.worker_id, project_id=r.project_id, kind=ReservationKind(r.kind),
        subject_id=r.subject_id, requester_user_id=r.requester_user_id,
        resources=ResourceVector(cpu_millicores=r.cpu, ram_mib=r.ram, vram_mib=r.vram, disk_mib=r.disk),
        state=ResourceReservationState(r.state), fencing_token=r.fencing_token,
        created_at=r.created_at, expires_at=r.expires_at)


def _worker_row(conn: Connection, worker_id: UUID):  # noqa: ANN201
    row = conn.execute(sa.select(t.workers).where(t.workers.c.id == worker_id)).first()
    if row is None:
        raise not_found("worker")
    return row


def _policy_row(conn: Connection, worker_id: UUID):  # noqa: ANN201
    return conn.execute(sa.select(t.sharing_policies).where(t.sharing_policies.c.worker_id == worker_id)).one()


def availability(conn: Connection, w, pol, now: datetime) -> DeviceAvailability:  # noqa: ANN001
    if not P.device_fresh(worker_contract(w), now) or w.revoked_at is not None:
        return DeviceAvailability.offline
    if pol.paused or not (pol.share_compute or pol.accept_delegated_tasks or pol.share_local_inference):
        return DeviceAvailability.paused
    return DeviceAvailability.busy if w.used_slots > 0 else DeviceAvailability.available


# ---------------------------------------------------------------------- enrollment


def create_enrollment_grant(conn: Connection, now: datetime, p: Principal, workspace_id: UUID) -> A.EnrollmentGrant:
    from ..core import require_workspace_member

    require_human(p, "enrolling a device")
    require_workspace_member(conn, p, workspace_id)
    token = new_token("he")
    conn.execute(sa.insert(t.enrollment_grants).values(
        id=new_id(), workspace_id=workspace_id, user_id=p.user_id, grant_hash=token_hash(token),
        expires_at=now + ENROLLMENT_TTL, created_at=now))
    return A.EnrollmentGrant(enrollment_grant=token, expires_at=now + ENROLLMENT_TTL)


def register_worker(conn: Connection, now: datetime, body: A.RegisterWorker) -> A.RegisterWorkerResponse:
    """The enrollment grant (short-lived, single use, issued to a human) decides the
    workspace and owner; the device cannot self-declare project access. All sharing
    starts OFF."""
    g = t.enrollment_grants.c
    grant = conn.execute(sa.select(t.enrollment_grants).where(g.grant_hash == token_hash(body.enrollment_grant))).first()
    if grant is None or grant.used_at is not None or grant.expires_at <= now:
        raise DomainError(ErrorCode.unauthenticated, "invalid or expired enrollment grant")
    cas(conn, sa.update(t.enrollment_grants).where(g.id == grant.id, g.used_at.is_(None)).values(used_at=now),
        "enrollment grant", ErrorCode.unauthenticated)
    wid = new_id()
    lim = body.device_limits
    conn.execute(sa.insert(t.workers).values(
        id=wid, workspace_id=grant.workspace_id, owner_user_id=grant.user_id, display_name=body.display_name,
        os=body.os, arch=body.arch, isolation=body.isolation, timezone=body.timezone,
        runtime_images=body.runtime_images, gpu_model=body.gpu_model, limit_cpu=lim.cpu_millicores,
        limit_ram=lim.ram_mib, limit_vram=lim.vram_mib, limit_disk=lim.disk_mib, enrolled_at=now))
    default = SharingPolicy(worker_id=wid, owner_user_id=grant.user_id, updated_at=now)
    conn.execute(sa.insert(t.sharing_policies).values(
        worker_id=wid, owner_user_id=grant.user_id, body=default.model_dump(mode="json", exclude={"revision", "updated_at"}),
        updated_at=now))
    token = new_token("hw")
    conn.execute(sa.insert(t.credentials).values(
        id=new_id(), kind="worker", token_hash=token_hash(token), user_id=grant.user_id, worker_id=wid,
        created_at=now))
    return A.RegisterWorkerResponse(worker=worker_contract(_worker_row(conn, wid)), worker_credential=token)


def list_my_workers(conn: Connection, now: datetime, p: Principal) -> list[A.WorkerStatus]:
    require_human(p, "listing devices")
    rows = conn.execute(sa.select(t.workers).where(t.workers.c.owner_user_id == p.user_id)
                        .order_by(t.workers.c.enrolled_at)).all()
    return [worker_status(conn, now, r) for r in rows]


def worker_status(conn: Connection, now: datetime, w) -> A.WorkerStatus:  # noqa: ANN001
    pol = _policy_row(conn, w.id)
    live = conn.execute(sa.select(t.resource_reservations).where(
        t.resource_reservations.c.worker_id == w.id,
        t.resource_reservations.c.state.in_(["held", "active"]))).all()
    return A.WorkerStatus(
        worker=worker_contract(w), availability=availability(conn, w, pol, now),
        policy=policy_contract(pol),
        reserved=ResourceVector(cpu_millicores=w.used_cpu, ram_mib=w.used_ram, vram_mib=w.used_vram,
                                disk_mib=w.used_disk),
        reservations=[_reservation(r) for r in live],
        power=PowerState(on_ac=w.on_ac, battery_percent=w.battery_percent))


def revoke_worker(conn: Connection, now: datetime, p: Principal, worker_id: UUID) -> None:
    require_human(p, "revoking a device")
    w = _worker_row(conn, worker_id)
    if w.owner_user_id != p.user_id:
        raise not_found("worker")
    conn.execute(sa.update(t.workers).where(t.workers.c.id == worker_id).values(revoked_at=now))
    conn.execute(sa.update(t.credentials).where(t.credentials.c.worker_id == worker_id).values(revoked_at=now))
    _stop_worker_work(conn, now, worker_id, kinds=None, project_id=None)


# ----------------------------------------------------------------------- heartbeat


def heartbeat(conn: Connection, now: datetime, p: Principal, worker_id: UUID,
              body: A.WorkerHeartbeat) -> HeartbeatResponse:
    """Freshness is the SERVER receipt time. Reported attempts that are not the current
    fenced attempt (or were cancelled/revoked) come back in stop_attempt_ids."""
    if p.kind != "worker" or p.worker_id != worker_id:
        raise not_found("worker")
    w = _worker_row(conn, worker_id)
    wc = t.workers.c
    conn.execute(sa.update(t.workers).where(wc.id == worker_id, wc.heartbeat_seq <= body.seq).values(
        last_heartbeat_at=now, heartbeat_seq=body.seq, on_ac=body.power.on_ac,
        battery_percent=body.power.battery_percent, interactive_use=body.interactive_use))
    stop: list[UUID] = []
    jc = t.jobs.c
    for af in body.running_attempts:
        job = conn.execute(sa.select(t.jobs).where(jc.attempt_id == af.attempt_id)).first()
        if job is None:
            from . import inference

            if not inference.renew(conn, now, worker_id, af.attempt_id, af.fencing_token):
                stop.append(af.attempt_id)
            continue
        if job.fence != af.fencing_token or job.worker_id != worker_id or job.cancel_requested \
                or job.state not in ("reserved", "running"):
            stop.append(af.attempt_id)
            continue
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            stop.append(af.attempt_id)  # expired; the reaper fences it
            continue
        conn.execute(sa.update(t.jobs).where(jc.id == job.id, jc.attempt_id == af.attempt_id)
                     .values(lease_expires_at=now + JOB_LEASE))
        conn.execute(sa.update(t.job_attempts).where(t.job_attempts.c.id == af.attempt_id)
                     .values(last_heartbeat_at=now))
    # Advisory offers, only for projects the owner belongs to and the policy allows.
    pol = policy_contract(_policy_row(conn, worker_id))
    for offer in body.offers:
        if offer.project_id not in pol.allowed_project_ids:
            continue
        o = t.resource_offers.c
        doc = offer.model_dump(mode="json")
        conn.execute(sa.delete(t.resource_offers).where(o.worker_id == worker_id, o.project_id == offer.project_id))
        conn.execute(sa.insert(t.resource_offers).values(worker_id=worker_id, project_id=offer.project_id,
                                                         body=doc, reported_at=now))
    if w.last_heartbeat_at is None or not P.device_fresh(worker_contract(w), now):
        for pid in pol.allowed_project_ids:
            if _exists_project(conn, pid):
                emit(conn, now, pid, worker_id, body.seq, WorkerUpdated(
                    worker_id=worker_id, availability=availability(conn, _worker_row(conn, worker_id),
                                                                   _policy_row(conn, worker_id), now)))
    return HeartbeatResponse(server_received_at=now, stop_attempt_ids=stop, renewed_until=now + JOB_LEASE)


def _exists_project(conn: Connection, pid: UUID) -> bool:
    return conn.execute(sa.select(t.projects.c.id).where(t.projects.c.id == pid)).first() is not None


# ----------------------------------------------------------------- sharing policy


def get_policy(conn: Connection, p: Principal, worker_id: UUID) -> SharingPolicy:
    w = _worker_row(conn, worker_id)
    if w.owner_user_id != p.user_id:
        raise not_found("worker")
    return policy_contract(_policy_row(conn, worker_id))


def update_policy(conn: Connection, now: datetime, p: Principal, worker_id: UUID,
                  body: SharingPolicyUpdate) -> SharingPolicy:
    """Owner-only, human-only. Removing eligibility stops NEW assignments immediately; if
    running work is affected the caller must choose finish_current or stop_now."""
    require_human(p, "changing device sharing")
    w = _worker_row(conn, worker_id)
    if w.owner_user_id != p.user_id:
        raise not_found("worker")
    new = body.policy
    if new.worker_id != worker_id or new.owner_user_id != p.user_id:
        raise DomainError(ErrorCode.validation_error, "policy must name this worker and its owner")
    if not new.shared_limits.fits_within(worker_contract(w).device_limits):
        raise DomainError(ErrorCode.policy_denied, "shared limits exceed device limits")
    for pid in new.allowed_project_ids:
        require_role(conn, p, pid, "viewer")
    old = policy_contract(_policy_row(conn, worker_id))
    removed = _removed_kinds(old, new)
    removed_projects = set(old.allowed_project_ids) - set(new.allowed_project_ids)
    affected = _live_reservations(conn, worker_id, removed, removed_projects)
    if affected and body.on_revocation is None:
        raise DomainError(ErrorCode.validation_error,
                          "running work is affected; choose on_revocation=finish_current or stop_now")
    sp = t.sharing_policies.c
    doc = new.model_dump(mode="json", exclude={"revision", "updated_at"})
    lim = new.shared_limits
    cas(conn, sa.update(t.sharing_policies).where(sp.worker_id == worker_id, sp.revision == body.expected_revision)
        .values(body=doc, share_compute=new.share_compute, accept_delegated_tasks=new.accept_delegated_tasks,
                share_local_inference=new.share_local_inference, paused=new.paused,
                shared_cpu=lim.cpu_millicores, shared_ram=lim.ram_mib, shared_vram=lim.vram_mib,
                shared_disk=lim.disk_mib, max_concurrent_jobs=new.max_concurrent_jobs,
                revision=sp.revision + 1, updated_at=now), "sharing policy")
    if affected and body.on_revocation is RevocationMode.stop_now:
        for r in affected:
            _stop_reservation_subject(conn, now, r, reason="sharing revoked (stop now)")
    conn.execute(sa.insert(t.audit_events).values(
        id=new_id(), workspace_id=w.workspace_id, actor_kind="human", actor_id=p.user_id,
        action="sharing_policy.update", target_id=worker_id,
        detail={"on_revocation": body.on_revocation.value if body.on_revocation else "",
                "affected": str(len(affected))}, created_at=now))
    return policy_contract(_policy_row(conn, worker_id))


def _removed_kinds(old: SharingPolicy, new: SharingPolicySettings) -> set[str]:
    out = set()
    if new.paused and not old.paused:
        return {k.value for k in ReservationKind}
    if old.share_compute and not new.share_compute:
        out.add("job")
    if old.accept_delegated_tasks and not new.accept_delegated_tasks:
        out.add("delegation")
    if old.share_local_inference and not new.share_local_inference:
        out.add("inference")
    return out


def _live_reservations(conn: Connection, worker_id: UUID, kinds: set[str], projects: set[UUID]):  # noqa: ANN202
    r = t.resource_reservations.c
    conds = []
    if kinds:
        conds.append(r.kind.in_(kinds))
    if projects:
        conds.append(r.project_id.in_(projects))
    if not conds:
        return []
    return conn.execute(sa.select(t.resource_reservations).where(
        r.worker_id == worker_id, r.state.in_(["held", "active"]), sa.or_(*conds))).all()


# ---------------------------------------------------------------- capacity pool


def reserve_capacity(conn: Connection, now: datetime, *, worker_id: UUID, project_id: UUID,
                     kind: ReservationKind, subject_id: UUID, requester_user_id: UUID,
                     resources: ResourceVector, fence: int, ttl: timedelta) -> UUID | None:
    """Atomic joint reservation. Returns None when the pool or slot limit is full."""
    wc = t.workers.c
    sp = t.sharing_policies
    lim = lambda col: sa.select(col).where(sp.c.worker_id == worker_id).scalar_subquery()  # noqa: E731
    slots = 1 if kind.value in COUNTED else 0
    conds = [
        wc.id == worker_id, wc.revoked_at.is_(None),
        wc.used_cpu + resources.cpu_millicores <= lim(sp.c.shared_cpu),
        wc.used_ram + resources.ram_mib <= lim(sp.c.shared_ram),
        wc.used_vram + resources.vram_mib <= lim(sp.c.shared_vram),
        wc.used_disk + resources.disk_mib <= lim(sp.c.shared_disk),
    ]
    if slots:
        conds.append(wc.used_slots + 1 <= lim(sp.c.max_concurrent_jobs))
    n = conn.execute(sa.update(t.workers).where(*conds).values(
        used_cpu=wc.used_cpu + resources.cpu_millicores, used_ram=wc.used_ram + resources.ram_mib,
        used_vram=wc.used_vram + resources.vram_mib, used_disk=wc.used_disk + resources.disk_mib,
        used_slots=wc.used_slots + slots)).rowcount
    if n != 1:
        return None
    rid = new_id()
    conn.execute(sa.insert(t.resource_reservations).values(
        id=rid, worker_id=worker_id, project_id=project_id, kind=kind.value, subject_id=subject_id,
        requester_user_id=requester_user_id, cpu=resources.cpu_millicores, ram=resources.ram_mib,
        vram=resources.vram_mib, disk=resources.disk_mib, slots=slots, state="held",
        fencing_token=fence, created_at=now, expires_at=now + ttl))
    _emit_reservation(conn, now, rid)
    return rid


def release_capacity(conn: Connection, now: datetime, reservation_id: UUID | None,
                     state: str = "released") -> None:
    """Idempotent: only the first release of a live reservation decrements the pool."""
    if reservation_id is None:
        return
    r = t.resource_reservations.c
    row = conn.execute(sa.select(t.resource_reservations).where(r.id == reservation_id)).first()
    if row is None:
        return
    n = conn.execute(sa.update(t.resource_reservations).where(
        r.id == reservation_id, r.state.in_(["held", "active"])).values(state=state)).rowcount
    if n != 1:
        return
    wc = t.workers.c
    conn.execute(sa.update(t.workers).where(wc.id == row.worker_id).values(
        used_cpu=wc.used_cpu - row.cpu, used_ram=wc.used_ram - row.ram, used_vram=wc.used_vram - row.vram,
        used_disk=wc.used_disk - row.disk, used_slots=wc.used_slots - row.slots))
    _emit_reservation(conn, now, reservation_id)


def extend_reservation(conn: Connection, reservation_id: UUID | None, until: datetime) -> None:
    if reservation_id is not None:
        r = t.resource_reservations.c
        conn.execute(sa.update(t.resource_reservations).where(
            r.id == reservation_id, r.state.in_(["held", "active"])).values(state="active", expires_at=until))


def _emit_reservation(conn: Connection, now: datetime, rid: UUID) -> None:
    r = _reservation(conn.execute(sa.select(t.resource_reservations).where(t.resource_reservations.c.id == rid)).one())
    emit(conn, now, r.project_id, r.id, r.fencing_token,
         ReservationUpdated(reservation_id=r.id, worker_id=r.worker_id, state=r.state, resources=r.resources))


def eligible(conn: Connection, now: datetime, worker_id: UUID, project_id: UUID, kind: ReservationKind,
             job_type: str | None) -> tuple[bool, str]:
    w = _worker_row(conn, worker_id)
    owner_member = conn.execute(sa.select(t.memberships.c.id).where(
        t.memberships.c.project_id == project_id, t.memberships.c.user_id == w.owner_user_id,
        t.memberships.c.revoked_at.is_(None))).first()
    if owner_member is None:
        return False, "device owner is not a project member"
    return P.eligible_for_new_work(
        worker_contract(w), policy_contract(_policy_row(conn, worker_id)), project_id=project_id,
        kind=kind, job_type=job_type, on_ac=w.on_ac, interactive_use=w.interactive_use, now=now)


# --------------------------------------------------------------------------- jobs


def job_contract(r) -> Job:  # noqa: ANN001
    b = r.body
    return Job(id=r.id, project_id=r.project_id, task_id=r.task_id, job_type=r.job_type,
               input_hashes=b["input_hashes"], repo_commit=b.get("repo_commit"), image_digest=b["image_digest"],
               resource_request=ResourceVector.model_validate(b["resource_request"]),
               timeout_seconds=b["timeout_seconds"], allowed_network_policy=b["allowed_network_policy"],
               egress_allowlist=b.get("egress_allowlist", []), idempotent=r.idempotent,
               supports_checkpoint=b.get("supports_checkpoint", False), state=JobState(r.state),
               attempt_id=r.attempt_id, max_attempts=r.max_attempts, revision=r.revision, created_at=r.created_at)


def create_job(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreateJob) -> Job:
    from .work import readable_task

    require_role(conn, p, project_id, "editor")
    task = readable_task(conn, p, body.task_id, "editor")
    if task.project_id != project_id:
        raise not_found("task")
    jid = new_id()
    doc = body.model_dump(mode="json", exclude={"task_id", "idempotent", "max_attempts", "idempotency_key"})
    conn.execute(sa.insert(t.jobs).values(
        id=jid, project_id=project_id, task_id=body.task_id, requested_by_user_id=p.user_id,
        job_type=body.job_type, body=doc, state="queued", idempotent=body.idempotent,
        max_attempts=body.max_attempts, created_at=now))
    return job_contract(conn.execute(sa.select(t.jobs).where(t.jobs.c.id == jid)).one())


def get_job(conn: Connection, p: Principal, job_id: UUID) -> Job:
    row = conn.execute(sa.select(t.jobs).where(t.jobs.c.id == job_id)).first()
    if row is None:
        raise not_found("job")
    require_role(conn, p, row.project_id, "viewer")
    return job_contract(row)


def claim_job(conn: Connection, now: datetime, p: Principal, body: A.ClaimJob) -> A.JobAssignment | None:
    """Worker pulls work. Eligibility (policy, freshness, power, schedule, project) and
    the joint capacity reservation are both checked at claim time."""
    if p.kind != "worker" or p.worker_id != body.worker_id:
        raise not_found("worker")
    pol = policy_contract(_policy_row(conn, body.worker_id))
    types = [jt for jt in body.job_types if jt in pol.allowed_job_types]
    if not types or not pol.allowed_project_ids:
        return None
    jc = t.jobs.c
    candidates = conn.execute(sa.select(t.jobs).where(
        jc.state == "queued", jc.job_type.in_(types), jc.project_id.in_(pol.allowed_project_ids),
        jc.cancel_requested.is_(False)).order_by(jc.created_at).limit(20)).all()
    for job in candidates:
        ok, _ = eligible(conn, now, body.worker_id, job.project_id, ReservationKind.job, job.job_type)
        if not ok:
            continue
        fence = job.fence + 1
        rid = reserve_capacity(conn, now, worker_id=body.worker_id, project_id=job.project_id,
                               kind=ReservationKind.job, subject_id=job.id,
                               requester_user_id=job.requested_by_user_id,
                               resources=ResourceVector.model_validate(job.body["resource_request"]),
                               fence=fence, ttl=JOB_LEASE)
        if rid is None:
            return None  # pool full: do not try smaller jobs out of order
        attempt = new_id()
        n = conn.execute(sa.update(t.jobs).where(jc.id == job.id, jc.state == "queued", jc.fence == job.fence).values(
            state="reserved", fence=fence, attempt_id=attempt, worker_id=body.worker_id, reservation_id=rid,
            lease_expires_at=now + JOB_LEASE, attempts=jc.attempts + 1, revision=jc.revision + 1)).rowcount
        if n != 1:
            release_capacity(conn, now, rid)
            continue
        conn.execute(sa.insert(t.job_attempts).values(
            id=attempt, job_id=job.id, worker_id=body.worker_id, fencing_token=fence, reservation_id=rid,
            state="assigned", started_at=now))
        row = conn.execute(sa.select(t.jobs).where(jc.id == job.id)).one()
        return A.JobAssignment(
            job=job_contract(row),
            attempt=JobAttempt(id=attempt, job_id=job.id, worker_id=body.worker_id, lease_id=attempt,
                               fencing_token=fence, reservation_id=rid, started_at=now),
            lease=Lease(id=attempt, subject=LeaseSubject.job, subject_id=job.id,
                        holder=ActorRef(kind=ActorKind.worker, id=body.worker_id), attempt_id=attempt,
                        fencing_token=fence, acquired_at=now, expires_at=now + JOB_LEASE),
            input_grants={h: f"/v1/jobs/{job.id}/inputs/{h}?attempt_id={attempt}" for h in job.body["input_hashes"]},
            output_grant_expires_at=now + timedelta(seconds=job.body["timeout_seconds"]))
    return None


def attempt_job(conn: Connection, now: datetime, p: Principal, attempt_id: UUID | None, job_id: UUID | None = None):  # noqa: ANN202
    """The job whose CURRENT attempt is `attempt_id`, held by this worker under a live
    lease, not cancelled, and whose device owner is still a project member. This is the
    only way a worker credential reaches project data."""
    if p.kind != "worker" or attempt_id is None:
        raise not_found("job")
    q = sa.select(t.jobs).where(t.jobs.c.attempt_id == attempt_id)
    if job_id is not None:
        q = q.where(t.jobs.c.id == job_id)
    row = conn.execute(q).first()
    if row is None or row.worker_id != p.worker_id:
        raise DomainError(ErrorCode.stale_attempt, "not the current attempt for this worker")
    if (row.state not in ("reserved", "running") or row.cancel_requested
            or row.lease_expires_at is None or row.lease_expires_at <= now):
        raise DomainError(ErrorCode.stale_attempt, "attempt expired, cancelled or finished")
    member = conn.execute(sa.select(t.memberships.c.id).where(
        t.memberships.c.project_id == row.project_id, t.memberships.c.user_id == p.user_id,
        t.memberships.c.revoked_at.is_(None))).first()
    if member is None:
        raise not_found("job")
    return row


def job_input(conn: Connection, now: datetime, p: Principal, store, job_id: UUID, sha256: str,  # noqa: ANN001
              attempt_id: UUID) -> bytes:
    """GET /v1/jobs/{job_id}/inputs/{sha256}?attempt_id=...: bytes of one allow-listed
    input, verified against its hash before they leave the server."""
    import hashlib

    row = attempt_job(conn, now, p, attempt_id, job_id)
    if sha256 not in row.body["input_hashes"]:
        raise not_found("job input")
    a = t.artifacts.c
    art = conn.execute(sa.select(t.artifacts).where(
        a.project_id == row.project_id, a.sha256 == sha256, a.status == "ready",
        sa.or_(a.visibility != "private", a.created_by_user_id == row.requested_by_user_id),
    ).order_by(a.created_at).limit(1)).first()
    if art is None:
        raise not_found("job input")
    data = store.get(art.storage_key)
    if data is None or hashlib.sha256(data).hexdigest() != sha256:
        raise DomainError(ErrorCode.provider_unavailable, "stored input missing or corrupted")
    return data


def _fenced_job(conn: Connection, now: datetime, p: Principal, job_id: UUID, attempt_id: UUID, fence: int):  # noqa: ANN202
    if p.kind != "worker":
        raise DomainError(ErrorCode.permission_denied, "workers only")
    row = conn.execute(sa.select(t.jobs).where(t.jobs.c.id == job_id)).first()
    if row is None:
        raise not_found("job")
    if (row.worker_id != p.worker_id or row.attempt_id != attempt_id or row.fence != fence
            or row.lease_expires_at is None or row.lease_expires_at <= now or row.cancel_requested):
        raise DomainError(ErrorCode.stale_attempt, "attempt superseded, expired or cancelled")
    return row


def start_job(conn: Connection, now: datetime, p: Principal, job_id: UUID, body: AttemptFence) -> Job:
    row = _fenced_job(conn, now, p, job_id, body.attempt_id, body.fencing_token)
    jc = t.jobs.c
    cas(conn, sa.update(t.jobs).where(jc.id == job_id, jc.state == "reserved", jc.fence == body.fencing_token)
        .values(state="running", revision=jc.revision + 1), "job", ErrorCode.invalid_transition)
    conn.execute(sa.update(t.job_attempts).where(t.job_attempts.c.id == body.attempt_id).values(state="running"))
    extend_reservation(conn, row.reservation_id, now + JOB_LEASE)
    return job_contract(conn.execute(sa.select(t.jobs).where(jc.id == job_id)).one())


def complete_job(conn: Connection, now: datetime, p: Principal, job_id: UUID, body: A.CompleteJob) -> Job:
    """Fenced: a late completion from a superseded attempt is rejected and can never
    overwrite the newer attempt's result. Remote results are untrusted evidence."""
    row = _fenced_job(conn, now, p, job_id, body.attempt_id, body.fencing_token)
    for out in body.outputs:
        a = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == out.artifact_id)).first()
        if a is None or a.project_id != row.project_id or a.sha256 != out.sha256 or a.status != "ready":
            raise DomainError(ErrorCode.validation_error, f"output {out.artifact_id} not a finalized project artifact")
        if (a.provenance or {}).get("job_attempt_id") != str(body.attempt_id):
            raise DomainError(ErrorCode.validation_error, f"output {out.artifact_id} was not produced by this attempt")
    jc = t.jobs.c
    state = "succeeded" if body.outcome == "succeeded" else "failed"
    result = body.model_dump(mode="json", exclude={"fencing_token"}) | {"trusted_runner": False,
                                                                        "worker_id": str(p.worker_id)}
    cas(conn, sa.update(t.jobs).where(jc.id == job_id, jc.attempt_id == body.attempt_id, jc.fence == body.fencing_token,
                                      jc.state.in_(["reserved", "running"]))
        .values(state=state, result=result, lease_expires_at=None, revision=jc.revision + 1),
        "job", ErrorCode.stale_attempt)
    conn.execute(sa.update(t.job_attempts).where(t.job_attempts.c.id == body.attempt_id)
                 .values(state=state, ended_at=now))
    release_capacity(conn, now, row.reservation_id)
    return job_contract(conn.execute(sa.select(t.jobs).where(jc.id == job_id)).one())


def cancel_job(conn: Connection, now: datetime, job_id: UUID) -> None:
    jc = t.jobs.c
    row = conn.execute(sa.select(t.jobs).where(jc.id == job_id)).first()
    if row is None or row.state in ("succeeded", "failed", "cancelled"):
        return
    conn.execute(sa.update(t.jobs).where(jc.id == job_id).values(
        state="cancelled", cancel_requested=True, lease_expires_at=None, revision=jc.revision + 1))
    if row.attempt_id:
        conn.execute(sa.update(t.job_attempts).where(t.job_attempts.c.id == row.attempt_id,
                                                     t.job_attempts.c.ended_at.is_(None))
                     .values(state="cancelled", ended_at=now))
    release_capacity(conn, now, row.reservation_id)


def _requeue_or_fail(conn: Connection, now: datetime, row, attempt_state: str) -> None:  # noqa: ANN001
    """Fence the current attempt. Idempotent jobs with attempts left return to the queue
    (any eligible worker may retry); non-idempotent jobs fail visibly instead."""
    jc = t.jobs.c
    retry = row.idempotent and row.attempts < row.max_attempts
    conn.execute(sa.update(t.jobs).where(jc.id == row.id, jc.attempt_id == row.attempt_id).values(
        state="queued" if retry else "failed", attempt_id=None, worker_id=None, reservation_id=None,
        lease_expires_at=None, revision=jc.revision + 1,
        result=None if retry else {"error": "attempt lost; job not idempotent or attempts exhausted"}))
    if row.attempt_id:
        conn.execute(sa.update(t.job_attempts).where(t.job_attempts.c.id == row.attempt_id,
                                                     t.job_attempts.c.ended_at.is_(None))
                     .values(state=attempt_state, ended_at=now))
    release_capacity(conn, now, row.reservation_id, state="expired" if attempt_state == "fenced" else "released")


def expire_jobs(conn: Connection, now: datetime) -> int:
    jc = t.jobs.c
    rows = conn.execute(sa.select(t.jobs).where(jc.state.in_(["reserved", "running"]), jc.lease_expires_at < now)).all()
    for r in rows:
        _requeue_or_fail(conn, now, r, JobAttemptState.fenced.value)
    return len(rows)


def expire_reservations(conn: Connection, now: datetime) -> int:
    """Safety net: a reservation whose subject vanished or whose TTL passed is freed."""
    r = t.resource_reservations.c
    rows = conn.execute(sa.select(t.resource_reservations.c.id).where(
        r.state.in_(["held", "active"]), r.expires_at < now)).scalars().all()
    for rid in rows:
        release_capacity(conn, now, rid, state="expired")
    return len(rows)


# ------------------------------------------------------------------ stop helpers


def _stop_reservation_subject(conn: Connection, now: datetime, r, reason: str) -> None:  # noqa: ANN001
    if r.kind == "job":
        row = conn.execute(sa.select(t.jobs).where(t.jobs.c.id == r.subject_id)).first()
        if row is not None and row.state in ("reserved", "running"):
            _requeue_or_fail(conn, now, row, JobAttemptState.cancelled.value)
            return
    elif r.kind == "delegation":
        from .delegation import requeue_delegation

        requeue_delegation(conn, now, r.subject_id, reason)
        return
    elif r.kind == "inference":
        ic = t.inference_requests.c
        conn.execute(sa.update(t.inference_requests).where(ic.id == r.subject_id, ic.state == "claimed").values(
            state="failed", attempt_id=None, lease_expires_at=None, result={"error": reason[:200]},
            revision=ic.revision + 1))
    release_capacity(conn, now, r.id)


def _stop_worker_work(conn: Connection, now: datetime, worker_id: UUID, kinds: set[str] | None,
                      project_id: UUID | None) -> None:
    rc = t.resource_reservations.c
    q = sa.select(t.resource_reservations).where(rc.worker_id == worker_id, rc.state.in_(["held", "active"]))
    if kinds:
        q = q.where(rc.kind.in_(kinds))
    if project_id:
        q = q.where(rc.project_id == project_id)
    for r in conn.execute(q).all():
        _stop_reservation_subject(conn, now, r, reason="device revoked")


def release_member_work(conn: Connection, now: datetime, project_id: UUID, user_id: UUID) -> None:
    """On membership revocation: release task leases held by the member's agents, requeue
    delegations they hold, and stop their devices' work for this project."""
    from ..contracts.work import TaskState
    from .delegation import requeue_delegation
    from .work import system_transition

    agents = conn.execute(sa.select(t.agent_profiles.c.id).where(t.agent_profiles.c.owner_user_id == user_id)).scalars().all()
    if agents:
        tc = t.tasks.c
        for tid in conn.execute(sa.select(tc.id).where(tc.project_id == project_id, tc.owner_agent_id.in_(agents),
                                                       tc.state.in_(["leased", "running"]))).scalars().all():
            system_transition(conn, now, tid, TaskState.ready)
        dc = t.delegations.c
        for did in conn.execute(sa.select(dc.id).where(dc.project_id == project_id, dc.recipient_agent_id.in_(agents),
                                                       dc.state.in_(["assigned", "accepted", "running"]))).scalars().all():
            requeue_delegation(conn, now, did, "recipient membership revoked")
    for wid in conn.execute(sa.select(t.workers.c.id).where(t.workers.c.owner_user_id == user_id)).scalars().all():
        _stop_worker_work(conn, now, wid, kinds=None, project_id=project_id)
