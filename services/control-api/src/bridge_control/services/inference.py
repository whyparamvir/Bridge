"""Worker inference bridge (section 20): bounded TEXT generation on a teammate's
device-local model, proxied over the device's enrolled worker connection.

- The device's local server stays bound to loopback; the worker pulls requests
  (outbound HTTPS) and calls its runtime. There is no public model port.
- Requires the device owner's `share_local_inference` opt-in, project allowlist, fresh
  heartbeat, power/interactive policy and a reservation from the SAME capacity pool as
  jobs and delegations (kind=inference, sized by the endpoint's memory_needs).
- Attempts are fenced; a late completion from an expired attempt is rejected.
- Local inference spends no API credits; usage is still recorded (cost 0).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.common import ResourceVector
from ..contracts.compute import (
    ExecutionLocation, InferenceAssignment, InferenceCompletion, InferenceRequestView, InferenceState,
    ReservationKind, RuntimeKind,
)
from ..contracts.errors import DomainError, ErrorCode
from ..core import Principal, cas, new_id, not_found, require_role
from ..db import tables as t
from . import models, scheduling

LEASE = timedelta(seconds=120)


def view(r) -> InferenceRequestView:  # noqa: ANN001
    res = r.result or {}
    return InferenceRequestView(
        id=r.id, project_id=r.project_id, model_endpoint_id=r.model_endpoint_id, worker_id=r.worker_id,
        requester_user_id=r.requester_user_id, state=InferenceState(r.state), max_tokens=r.body["max_tokens"],
        deadline=r.deadline, attempt_id=r.attempt_id, fencing_token=r.fence or None, text=res.get("text"),
        provider_model_id=res.get("provider_model_id"), input_tokens=res.get("input_tokens"),
        output_tokens=res.get("output_tokens"), runtime_ms=res.get("runtime_ms"), error=res.get("error"),
        created_at=r.created_at)


def _row(conn: Connection, rid: UUID):  # noqa: ANN202
    row = conn.execute(sa.select(t.inference_requests).where(t.inference_requests.c.id == rid)).first()
    if row is None:
        raise not_found("inference request")
    return row


def submit(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreateInferenceRequest,
           *, purpose: str = "task") -> InferenceRequestView:
    require_role(conn, p, project_id, "editor")
    ep = models.endpoint_contract_for(conn, p, body.model_endpoint_id)
    if ep.execution_location is not ExecutionLocation.device or ep.worker_id is None:
        raise DomainError(ErrorCode.validation_error, "the bridge serves device-local endpoints only")
    if ep.owner_user_id != p.user_id and project_id not in ep.shared_project_ids:
        raise not_found("model endpoint")
    rid = new_id()
    conn.execute(sa.insert(t.inference_requests).values(
        id=rid, project_id=project_id, model_endpoint_id=ep.id, worker_id=ep.worker_id, requester_user_id=p.user_id,
        purpose=purpose, body={"prompt": body.prompt, "max_tokens": body.max_tokens, "model_id": ep.model_id,
                               "runtime": ep.runtime.value},
        state="queued", deadline=now + timedelta(seconds=body.deadline_seconds), created_at=now))
    return view(_row(conn, rid))


def get(conn: Connection, p: Principal, rid: UUID) -> InferenceRequestView:
    row = _row(conn, rid)
    require_role(conn, p, row.project_id, "viewer")
    if row.requester_user_id != p.user_id:
        raise not_found("inference request")
    return view(row)


def claim(conn: Connection, now: datetime, p: Principal, body: A.ClaimInference) -> InferenceAssignment | None:
    if p.kind != "worker" or p.worker_id != body.worker_id:
        raise not_found("worker")
    ic = t.inference_requests.c
    rows = conn.execute(sa.select(t.inference_requests).where(
        ic.worker_id == body.worker_id, ic.state == "queued", ic.deadline > now).order_by(ic.created_at).limit(10)).all()
    for r in rows:
        ok, _ = scheduling.eligible(conn, now, body.worker_id, r.project_id, ReservationKind.inference, None)
        if not ok:
            continue
        ep = models.endpoint_contract(models.endpoint_row(conn, r.model_endpoint_id))
        fence = r.fence + 1
        rid = scheduling.reserve_capacity(conn, now, worker_id=body.worker_id, project_id=r.project_id,
                                          kind=ReservationKind.inference, subject_id=r.id,
                                          requester_user_id=r.requester_user_id,
                                          resources=ep.memory_needs or ResourceVector(), fence=fence, ttl=LEASE)
        if rid is None:
            return None
        attempt = new_id()
        n = conn.execute(sa.update(t.inference_requests).where(ic.id == r.id, ic.state == "queued", ic.fence == r.fence)
                         .values(state="claimed", fence=fence, attempt_id=attempt, reservation_id=rid,
                                 lease_expires_at=now + LEASE, revision=ic.revision + 1)).rowcount
        if n != 1:
            scheduling.release_capacity(conn, now, rid)
            continue
        return InferenceAssignment(request_id=r.id, attempt_id=attempt, fencing_token=fence,
                                   runtime=RuntimeKind(r.body["runtime"]), model_id=r.body["model_id"],
                                   prompt=r.body["prompt"], max_tokens=r.body["max_tokens"],
                                   lease_expires_at=now + LEASE, reservation_id=rid)
    return None


def complete(conn: Connection, now: datetime, p: Principal, rid: UUID, body: InferenceCompletion) -> InferenceRequestView:
    row = _row(conn, rid)
    if p.kind != "worker" or row.worker_id != p.worker_id:
        raise not_found("inference request")
    if (row.attempt_id != body.attempt_id or row.fence != body.fencing_token or row.state != "claimed"
            or row.lease_expires_at is None or row.lease_expires_at <= now):
        raise DomainError(ErrorCode.stale_attempt, "inference attempt superseded or expired")
    if body.outcome == "succeeded" and not body.text:
        raise DomainError(ErrorCode.validation_error, "successful inference needs text")
    ic = t.inference_requests.c
    result = body.model_dump(mode="json", exclude={"attempt_id", "fencing_token", "outcome"})
    cas(conn, sa.update(t.inference_requests).where(ic.id == rid, ic.attempt_id == body.attempt_id,
                                                    ic.fence == body.fencing_token, ic.state == "claimed")
        .values(state=body.outcome, result=result, lease_expires_at=None, revision=ic.revision + 1),
        "inference", ErrorCode.stale_attempt)
    scheduling.release_capacity(conn, now, row.reservation_id)
    conn.execute(sa.insert(t.usage_entries).values(
        id=new_id(), project_id=row.project_id, kind="model_tokens", model_endpoint_id=row.model_endpoint_id,
        provider_model_id=body.provider_model_id, input_tokens=body.input_tokens, output_tokens=body.output_tokens,
        runtime_ms=body.runtime_ms, cost_micros=0, provider_reported=False,
        operation_key=f"inference:{rid}:{body.attempt_id}", recorded_at=now))
    return view(_row(conn, rid))


def renew(conn: Connection, now: datetime, worker_id: UUID, attempt_id: UUID, fence: int) -> bool:
    ic = t.inference_requests.c
    row = conn.execute(sa.select(t.inference_requests).where(ic.attempt_id == attempt_id)).first()
    if row is None or row.worker_id != worker_id or row.fence != fence or row.state != "claimed" \
            or row.lease_expires_at is None or row.lease_expires_at <= now:
        return False
    conn.execute(sa.update(t.inference_requests).where(ic.id == row.id, ic.attempt_id == attempt_id)
                 .values(lease_expires_at=now + LEASE))
    scheduling.extend_reservation(conn, row.reservation_id, now + LEASE)
    return True


def expire(conn: Connection, now: datetime) -> int:
    """Lapsed attempts are fenced and re-queued until the deadline; then expired."""
    ic = t.inference_requests.c
    n = 0
    for r in conn.execute(sa.select(t.inference_requests).where(ic.state == "claimed", ic.lease_expires_at < now)).all():
        scheduling.release_capacity(conn, now, r.reservation_id, state="expired")
        retry = r.deadline > now
        conn.execute(sa.update(t.inference_requests).where(ic.id == r.id, ic.attempt_id == r.attempt_id).values(
            state="queued" if retry else "expired", attempt_id=None, reservation_id=None, lease_expires_at=None,
            revision=ic.revision + 1))
        n += 1
    n += conn.execute(sa.update(t.inference_requests).where(ic.state == "queued", ic.deadline <= now)
                      .values(state="expired", revision=ic.revision + 1)).rowcount
    return n
