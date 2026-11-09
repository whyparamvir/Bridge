"""Provider connections, payer grants and the extensible model-endpoint registry.

Provider keys never enter the database or any response: a connection stores only a
`secret_ref` naming where the server reads the key (currently `env:VARNAME`). Probes are
bounded calls executed by the agent runtime (outbox topic `model.probe`) and only
results are recorded here; a declared capability never qualifies a model by itself.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.common import Money, Visibility
from ..contracts.compute import EndpointHealth, ExecutionLocation, ModelEndpoint, ProbeResult
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import CapabilityUpdated
from ..contracts.identity import BillingMode, PayerGrant, ProviderConnection
from ..core import Principal, new_id, not_found, require_human, require_role, require_workspace_member
from ..db import tables as t
from ..reliability import emit, enqueue

SECRET_REF = re.compile(r"^env:[A-Z][A-Z0-9_]{1,63}$")
LOCAL_RUNTIMES = {"ollama", "lmstudio", "vllm"}


def connection_contract(r) -> ProviderConnection:  # noqa: ANN001
    return ProviderConnection(id=r.id, workspace_id=r.workspace_id, owner_user_id=r.owner_user_id,
                              billing_mode=BillingMode(r.billing_mode), provider=r.provider,
                              display_name=r.display_name, secret_ref=r.secret_ref, created_at=r.created_at,
                              disabled_at=r.disabled_at)


def create_connection(conn: Connection, now: datetime, p: Principal, body: A.CreateProviderConnection) -> ProviderConnection:
    require_human(p, "adding provider credentials")
    role = require_workspace_member(conn, p, body.workspace_id)
    if body.billing_mode is BillingMode.workspace and role != "owner":
        raise DomainError(ErrorCode.permission_denied, "workspace-funded connections need a workspace owner")
    if body.secret_ref is not None and not SECRET_REF.match(body.secret_ref):
        raise DomainError(ErrorCode.validation_error, "secret_ref must look like env:PROVIDER_API_KEY")
    if body.base_url is not None and body.provider not in LOCAL_RUNTIMES | {"custom_endpoint", "hosted_open_model"}:
        raise DomainError(ErrorCode.validation_error, "base_url overrides are for local/custom runtimes")
    cid = new_id()
    conn.execute(sa.insert(t.provider_connections).values(
        id=cid, workspace_id=body.workspace_id,
        owner_user_id=p.user_id if body.billing_mode is BillingMode.personal else None,
        billing_mode=body.billing_mode.value, provider=body.provider, display_name=body.display_name,
        secret_ref=body.secret_ref, base_url=body.base_url, created_at=now))
    return connection_contract(conn.execute(sa.select(t.provider_connections).where(t.provider_connections.c.id == cid)).one())


def list_connections(conn: Connection, p: Principal, workspace_id: UUID) -> list[ProviderConnection]:
    require_workspace_member(conn, p, workspace_id)
    c = t.provider_connections.c
    rows = conn.execute(sa.select(t.provider_connections).where(
        c.workspace_id == workspace_id, sa.or_(c.owner_user_id == p.user_id, c.billing_mode == "workspace"))).all()
    return [connection_contract(r) for r in rows]


# ---------------------------------------------------------------------- payer grants


def _grant(r) -> PayerGrant:  # noqa: ANN001
    return PayerGrant(id=r.id, connection_id=r.connection_id, project_id=r.project_id,
                      grantee_user_id=r.grantee_user_id,
                      allowed_model_endpoint_ids=[UUID(x) for x in r.allowed_model_endpoint_ids],
                      cap=Money(amount_micros=r.cap_micros, currency=r.currency), expires_at=r.expires_at,
                      revoked_at=r.revoked_at, revision=r.revision)


def create_grant(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: A.CreatePayerGrant) -> PayerGrant:
    """The fourth opt-in. Only the credential's owner can let a teammate spend it."""
    require_human(p, "granting spend")
    require_role(conn, p, project_id, "viewer")
    c = conn.execute(sa.select(t.provider_connections).where(t.provider_connections.c.id == body.connection_id)).first()
    if c is None or c.owner_user_id != p.user_id:
        raise not_found("connection")
    grantee = conn.execute(sa.select(t.memberships.c.id).where(
        t.memberships.c.project_id == project_id, t.memberships.c.user_id == body.grantee_user_id,
        t.memberships.c.revoked_at.is_(None))).first()
    if grantee is None or body.grantee_user_id == p.user_id:
        raise DomainError(ErrorCode.validation_error, "grantee must be another project member")
    if body.expires_at <= now:
        raise DomainError(ErrorCode.validation_error, "expiry in the past")
    gid = new_id()
    conn.execute(sa.insert(t.payer_grants).values(
        id=gid, connection_id=body.connection_id, project_id=project_id, grantor_user_id=p.user_id,
        grantee_user_id=body.grantee_user_id, allowed_model_endpoint_ids=[str(x) for x in body.allowed_model_endpoint_ids],
        currency=body.cap.currency, cap_micros=body.cap.amount_micros, expires_at=body.expires_at, created_at=now))
    return _grant(conn.execute(sa.select(t.payer_grants).where(t.payer_grants.c.id == gid)).one())


def revoke_grant(conn: Connection, now: datetime, p: Principal, grant_id: UUID) -> PayerGrant:
    require_human(p, "revoking spend")
    g = conn.execute(sa.select(t.payer_grants).where(t.payer_grants.c.id == grant_id)).first()
    if g is None or g.grantor_user_id != p.user_id:
        raise not_found("grant")
    conn.execute(sa.update(t.payer_grants).where(t.payer_grants.c.id == grant_id)
                 .values(revoked_at=now, revision=t.payer_grants.c.revision + 1))
    return _grant(conn.execute(sa.select(t.payer_grants).where(t.payer_grants.c.id == grant_id)).one())


def list_grants(conn: Connection, p: Principal, project_id: UUID) -> list[PayerGrant]:
    require_role(conn, p, project_id, "viewer")
    g = t.payer_grants.c
    rows = conn.execute(sa.select(t.payer_grants).where(
        g.project_id == project_id, sa.or_(g.grantor_user_id == p.user_id, g.grantee_user_id == p.user_id))).all()
    return [_grant(r) for r in rows]


# ------------------------------------------------------------------ model endpoints


def endpoint_contract(r) -> ModelEndpoint:  # noqa: ANN001
    return ModelEndpoint.model_validate({**r.body, "revision": r.revision})


def _shared_projects(conn: Connection, eid: UUID) -> list[UUID]:
    return list(conn.execute(sa.select(t.model_endpoint_projects.c.project_id)
                             .where(t.model_endpoint_projects.c.model_endpoint_id == eid)).scalars())


def endpoint_contract_for(conn: Connection, p: Principal, eid: UUID) -> ModelEndpoint:
    row = conn.execute(sa.select(t.model_endpoints).where(t.model_endpoints.c.id == eid)).first()
    if row is None:
        raise not_found("model endpoint")
    if row.owner_user_id != p.user_id:
        shared = _shared_projects(conn, eid)
        if not any(_is_member(conn, p, pid) for pid in shared):
            raise not_found("model endpoint")
    return endpoint_contract(row)


def _is_member(conn: Connection, p: Principal, pid: UUID) -> bool:
    try:
        require_role(conn, p, pid, "viewer")
        return True
    except DomainError:
        return False


def create_endpoint(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                    body: A.CreateModelEndpoint) -> ModelEndpoint:
    require_human(p, "registering models")
    require_role(conn, p, project_id, "editor")
    proj = conn.execute(sa.select(t.projects.c.workspace_id).where(t.projects.c.id == project_id)).one()
    if body.execution_location is ExecutionLocation.cloud:
        c = conn.execute(sa.select(t.provider_connections).where(t.provider_connections.c.id == body.connection_id)).first() \
            if body.connection_id else None
        if c is None or c.workspace_id != proj.workspace_id or (c.owner_user_id not in (None, p.user_id)):
            raise not_found("connection")
        endpoint_ref = f"connection:{c.id}"
    else:
        w = conn.execute(sa.select(t.workers).where(t.workers.c.id == body.worker_id)).first() if body.worker_id else None
        if w is None or w.owner_user_id != p.user_id:
            raise not_found("worker")
        if body.runtime.value not in LOCAL_RUNTIMES | {"custom_endpoint"}:
            raise DomainError(ErrorCode.validation_error, "device endpoints use a local runtime")
        endpoint_ref = f"worker:{w.id}:{body.runtime.value}"  # proxied by the worker; never a public URL
    for pid in body.shared_project_ids:
        require_role(conn, p, pid, "viewer")
    shared = sorted(set(body.shared_project_ids), key=str)
    eid = new_id()
    ep = ModelEndpoint(
        id=eid, workspace_id=proj.workspace_id, owner_user_id=p.user_id, runtime=body.runtime,
        model_id=body.model_id, model_revision=body.model_revision, quantization=body.quantization,
        context_limit_tokens=body.context_limit_tokens, declared_capabilities=body.declared_capabilities,
        license=body.license, execution_location=body.execution_location, worker_id=body.worker_id,
        connection_id=body.connection_id, endpoint_ref=endpoint_ref,
        visibility=Visibility.project if shared else Visibility.private, shared_project_ids=shared,
        memory_needs=body.memory_needs, created_at=now)
    conn.execute(sa.insert(t.model_endpoints).values(
        id=eid, workspace_id=proj.workspace_id, owner_user_id=p.user_id,
        body=ep.model_dump(mode="json", exclude={"revision"}), runtime=body.runtime.value,
        execution_location=body.execution_location.value, worker_id=body.worker_id,
        connection_id=body.connection_id, created_at=now))
    for pid in shared:
        conn.execute(sa.insert(t.model_endpoint_projects).values(model_endpoint_id=eid, project_id=pid))
        emit(conn, now, pid, eid, 0, CapabilityUpdated(model_endpoint_id=eid))
    if body.run_capability_probe:
        enqueue(conn, now, "model.probe", eid, {"requested_by": str(p.user_id), "project_id": str(project_id)})
    return endpoint_contract(conn.execute(sa.select(t.model_endpoints).where(t.model_endpoints.c.id == eid)).one())


def list_endpoints(conn: Connection, p: Principal, project_id: UUID) -> list[ModelEndpoint]:
    require_role(conn, p, project_id, "viewer")
    me, mp = t.model_endpoints.c, t.model_endpoint_projects.c
    rows = conn.execute(sa.select(t.model_endpoints).where(sa.or_(
        me.id.in_(sa.select(mp.model_endpoint_id).where(mp.project_id == project_id)),
        sa.and_(me.owner_user_id == p.user_id,
                me.workspace_id == sa.select(t.projects.c.workspace_id).where(t.projects.c.id == project_id).scalar_subquery()),
    )).order_by(me.created_at)).all()
    return [endpoint_contract(r) for r in rows]


def request_probe(conn: Connection, now: datetime, p: Principal, eid: UUID) -> None:
    ep = endpoint_contract_for(conn, p, eid)
    if ep.owner_user_id != p.user_id:
        raise DomainError(ErrorCode.permission_denied, "only the owner probes an endpoint")
    enqueue(conn, now, "model.probe", eid, {"requested_by": str(p.user_id)})


def record_probe(conn: Connection, now: datetime, eid: UUID, results: list[ProbeResult],
                 health: EndpointHealth) -> ModelEndpoint:
    """Called by the runtime (trusted server code) after a bounded probe."""
    row = conn.execute(sa.select(t.model_endpoints).where(t.model_endpoints.c.id == eid)).one()
    ep = endpoint_contract(row)
    ep = ep.model_copy(update={"probes": [*ep.probes, *results][-20:], "health": health})
    me = t.model_endpoints.c
    conn.execute(sa.update(t.model_endpoints).where(me.id == eid).values(
        body=ep.model_dump(mode="json", exclude={"revision"}), revision=me.revision + 1))
    for pid in _shared_projects(conn, eid):
        emit(conn, now, pid, eid, row.revision + 1, CapabilityUpdated(model_endpoint_id=eid))
    return endpoint_contract(conn.execute(sa.select(t.model_endpoints).where(me.id == eid)).one())


def endpoint_row(conn: Connection, eid: UUID):  # noqa: ANN201
    return conn.execute(sa.select(t.model_endpoints).where(t.model_endpoints.c.id == eid)).first()


def connection_row(conn: Connection, cid: UUID):  # noqa: ANN201
    return conn.execute(sa.select(t.provider_connections).where(t.provider_connections.c.id == cid)).first()
