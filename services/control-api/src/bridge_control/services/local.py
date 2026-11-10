"""Self-run team server (BRIDGE_ENV=local): one-time team bootstrap, the loopback-only
owner session guard, and each person's agent.

Threat model for the owner session: the owner has no password, so the only proof is
"this request comes from a program on the computer Bridge runs on". A web page in the
host's browser (cross-site request, DNS rebinding) or another machine on the network
must never pass `require_loopback`."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts.errors import DomainError, ErrorCode
from ..contracts.identity import AgentKind
from ..core import new_id
from ..db import tables as t
from ..settings import Settings

LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1"})
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
# A same-computer reverse proxy would make every remote caller look like 127.0.0.1.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
REFUSAL = "Open Bridge on the computer it runs on."
_AGENT_SUFFIX = "'s agents"
MAIN_LABEL = "agents"  # the main agent's name is "{person}'s agents"
MAIN_ADDRESS_LABEL = "main"  # the main agent's address is "<person>/main": no other agent may use it
MAX_AGENTS_PER_PERSON = 8  # in one workspace, the main agent included


# ------------------------------------------------------------------ loopback guard


def _split_host(value: str) -> tuple[str, str | None] | None:
    """Parse a Host header value into (hostname, port). IPv6 must be bracketed.
    Returns None when the value is malformed."""
    v = value.strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        host, rest = v[: end + 1], v[end + 1:]
    else:
        host, sep, port = v.partition(":")
        rest = sep + port
    if rest:
        port = rest[1:]
        if not (rest.startswith(":") and port.isascii() and port.isdigit()):
            return None
        return host, port
    return host, None


def require_loopback(request: Request, settings: Settings) -> None:  # noqa: ARG001
    """Raise 403 unless the request is from a program on this computer: loopback peer,
    loopback Host (defeats DNS rebinding), same-origin or no Origin (defeats cross-site
    pages), and the X-Bridge-Local header (forces a CORS preflight that nothing answers).
    Must run before anything is written."""
    refuse = DomainError(ErrorCode.permission_denied, REFUSAL)
    peer = request.client.host if request.client else ""
    if peer not in LOOPBACK_CLIENTS:
        raise refuse
    if any(request.headers.get(h) is not None for h in PROXY_HEADERS):
        raise refuse
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        raise refuse
    parsed = _split_host(hosts[0])
    if parsed is None or parsed[0] not in LOOPBACK_HOSTNAMES:
        raise refuse
    origins = request.headers.getlist("origin")
    if len(origins) > 1:
        raise refuse
    if origins:
        try:
            netloc = urlsplit(origins[0].strip()).netloc.lower()
        except ValueError:
            raise refuse from None
        if not netloc or netloc != hosts[0].strip().lower():
            raise refuse
    if request.headers.getlist("x-bridge-local") != ["1"]:
        raise refuse


# --------------------------------------------------------------------- person agent


def person_agent_name(display_name: str) -> str:
    """"{name}'s agents", kept within the 200-character agent name limit."""
    return f"{display_name[: 200 - len(_AGENT_SUFFIX)]}{_AGENT_SUFFIX}"


def sibling_agent_name(display_name: str, label: str) -> str:
    """"{name}'s {label}", kept within the 200-character agent name limit."""
    tail = f"'s {label}"
    return f"{display_name[: 200 - len(tail)]}{tail}"


def agent_label(person_name: str, agent_name: str) -> str:
    """An agent's name without its person's "{name}'s " prefix ("agents", "Codex")."""
    prefix = f"{person_name}'s "
    if len(agent_name) > len(prefix) and agent_name.casefold().startswith(prefix.casefold()):
        return agent_name[len(prefix):]
    return agent_name


def insert_agent(conn: Connection, **values) -> None:  # noqa: ANN003
    conn.execute(sa.insert(t.agent_profiles).values(**values))
    conn.execute(sa.insert(t.agent_numbers).values(agent_id=values["id"]))


def ensure_person_agent(conn: Connection, now: datetime, user_id: UUID, workspace_id: UUID,
                        label: str | None = None) -> UUID:
    """The user's personal orchestrator in this workspace, created if missing."""
    a = t.agent_profiles.c
    existing = conn.execute(sa.select(a.id).where(
        a.workspace_id == workspace_id, a.owner_user_id == user_id,
        a.kind == AgentKind.personal_orchestrator.value,
    ).order_by(a.created_at, a.id).limit(1)).first()
    if existing is not None:
        return existing.id
    name = conn.execute(sa.select(t.users.c.display_name).where(t.users.c.id == user_id)).scalar_one()
    aid = new_id()
    insert_agent(conn,
        id=aid, workspace_id=workspace_id, owner_user_id=user_id,
        kind=AgentKind.personal_orchestrator.value,
        name=sibling_agent_name(name, label) if label else person_agent_name(name),
        max_delegation_depth=2, skills=[], lead_model_endpoint_id=None, created_at=now)
    return aid


def rename_person_agents(conn: Connection, user_id: UUID, old_name: str, new_name: str) -> None:
    """Follow a display-name change, leaving agents the person renamed themselves alone:
    the main agent "{old}'s agents" and the person's other agents "{old}'s {label}"."""
    a = t.agent_profiles.c
    conn.execute(sa.update(t.agent_profiles).where(
        a.owner_user_id == user_id, a.kind == AgentKind.personal_orchestrator.value,
        a.name == person_agent_name(old_name),
    ).values(name=person_agent_name(new_name)))
    prefix = f"{old_name}'s "
    for row in conn.execute(sa.select(a.id, a.name).where(
            a.owner_user_id == user_id,
            a.kind.in_((AgentKind.personal_orchestrator.value, AgentKind.specialist.value)))).all():
        if row.name.startswith(prefix) and len(row.name) > len(prefix):
            conn.execute(sa.update(t.agent_profiles).where(a.id == row.id).values(
                name=sibling_agent_name(new_name, row.name[len(prefix):])))


# ------------------------------------------------------------------------ bootstrap


def installation(conn: Connection):  # noqa: ANN201
    return conn.execute(sa.select(t.installation).where(t.installation.c.id == 1)).first()


def bootstrap(conn: Connection, now: datetime, settings: Settings, team_name: str = "My team",
              owner_name: str = "Host", agent_label: str | None = None) -> sa.Row:
    """Create the team once: owner (no email, no password), workspace, project "Team",
    the project-scope team feed, the owner's agent and the installation row. A second
    or concurrent call returns the existing row and leaves nothing behind."""
    if not settings.is_local:
        raise RuntimeError("team bootstrap runs only with BRIDGE_ENV=local")
    row = installation(conn)
    if row is not None:
        return row
    try:
        with conn.begin_nested():
            uid, wid, pid, cid = new_id(), new_id(), new_id(), new_id()
            conn.execute(sa.insert(t.users).values(id=uid, email=None, display_name=owner_name,
                                                   password_hash=None, created_at=now))
            conn.execute(sa.insert(t.workspaces).values(id=wid, name=team_name, created_by=uid, created_at=now))
            conn.execute(sa.insert(t.workspace_members).values(workspace_id=wid, user_id=uid,
                                                               role="owner", created_at=now))
            conn.execute(sa.insert(t.projects).values(id=pid, workspace_id=wid, slug="team", name="Team",
                                                      objective="", currency="USD",
                                                      budget_limit_micros=0, created_at=now))
            conn.execute(sa.insert(t.memberships).values(id=new_id(), workspace_id=wid, project_id=pid,
                                                         user_id=uid, role="owner", created_at=now))
            # The team feed: the project's shared conversation.
            conn.execute(sa.insert(t.conversations).values(id=cid, project_id=pid, scope="project",
                                                           owner_user_id=None, created_at=now))
            ensure_person_agent(conn, now, uid, wid, agent_label)
            conn.execute(sa.insert(t.installation).values(
                id=1, owner_user_id=uid, workspace_id=wid, project_id=pid,
                feed_conversation_id=cid, created_at=now))
    except IntegrityError:
        # Lost the race: the savepoint rollback discarded our rows; use the winner's.
        row = installation(conn)
        if row is None:
            raise
        return row
    return installation(conn)
