"""GET /v1/projects/{id}/capabilities: what the caller may discover, with freshness.

Shows only project-relevant data: member agents' presence (with their authoritative
consent), advisory offers from devices whose owner allows this project, and endpoints
shared with the project. Stale offers are returned marked stale, never as capacity.
Nothing here is a reservation.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.common import ResourceVector
from ..contracts.compute import AgentPresence, CapabilitySummary, ResourceOffer
from ..core import Principal, require_role
from ..db import tables as t
from ..domain.policy import STALE_AFTER
from . import models, scheduling


def summary(conn: Connection, now: datetime, p: Principal, project_id: UUID) -> A.CapabilityView:
    require_role(conn, p, project_id, "viewer")
    ap, pr, ac, m = t.agent_profiles.c, t.agent_presence.c, t.agent_consents.c, t.memberships.c
    rows = conn.execute(
        sa.select(t.agent_presence, ap.owner_user_id, ap.skills, ac.accept_delegations)
        .join(t.agent_profiles, ap.id == pr.agent_id)
        .join(t.memberships, sa.and_(m.user_id == ap.owner_user_id, m.project_id == project_id))
        .outerjoin(t.agent_consents, sa.and_(ac.agent_id == pr.agent_id, ac.project_id == project_id))
        .where(pr.project_id == project_id, m.revoked_at.is_(None))
    ).all()
    agents = [AgentPresence(agent_id=r.agent_id, project_id=project_id, owner_user_id=r.owner_user_id,
                            worker_ids=[UUID(w) for w in r.worker_ids], skills=r.skills or [],
                            accepting_delegations=bool(r.accept_delegations) and now - r.last_seen_at < STALE_AFTER,
                            last_seen_at=r.last_seen_at, stale_after=r.last_seen_at + STALE_AFTER)
              for r in rows]
    offers, devices = [], []
    for o in conn.execute(sa.select(t.resource_offers).where(t.resource_offers.c.project_id == project_id)).all():
        w = conn.execute(sa.select(t.workers).where(t.workers.c.id == o.worker_id)).first()
        owner_member = conn.execute(sa.select(m.id).where(m.project_id == project_id, m.user_id == w.owner_user_id,
                                                          m.revoked_at.is_(None))).first()
        pol = scheduling.policy_contract(scheduling._policy_row(conn, w.id))
        if owner_member is None or project_id not in pol.allowed_project_ids or w.revoked_at is not None:
            continue
        b = o.body
        offers.append(ResourceOffer(
            worker_id=w.id, project_id=project_id,
            estimated_available=ResourceVector.model_validate(b["estimated_available"]),
            model_endpoint_ids=b.get("model_endpoint_ids", []), queue_length=b.get("queue_length", 0),
            heartbeat_seq=w.heartbeat_seq, reported_at=o.reported_at, stale_after=o.reported_at + STALE_AFTER))
        devices.append(A.DeviceSummary(
            worker_id=w.id, owner_user_id=w.owner_user_id, display_name=w.display_name,
            availability=scheduling.availability(conn, w, scheduling._policy_row(conn, w.id), now),
            share_compute=pol.share_compute, accept_delegated_tasks=pol.accept_delegated_tasks,
            share_local_inference=pol.share_local_inference,
            offer_is_stale=now - o.reported_at >= STALE_AFTER))
    eps = [e for e in models.list_endpoints(conn, p, project_id) if project_id in e.shared_project_ids
           or e.owner_user_id == p.user_id]
    return A.CapabilityView(
        summary=CapabilitySummary(project_id=project_id, generated_at=now, agents=agents, offers=offers,
                                  model_endpoints=eps),
        devices=devices)
