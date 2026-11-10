"""Accounts, sessions, workspaces, projects, memberships, agents and agent consent."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..contracts import api as A
from ..contracts.common import ActorKind, Money
from ..contracts.compute import AgentDelegationConsent, AgentDelegationConsentUpdate
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.identity import AgentKind, AgentProfile, Membership, Project, Role, User, Workspace, WorkspaceAccess, WorkspaceInvite, WorkspaceMemberView
from ..contracts.team import CreateInvite, InviteSession, SiblingAgent, UpdateMe
from ..core import (
    Principal, cas, hash_password, new_id, new_token, not_found, require_human, require_role,
    require_workspace_member, token_hash, verify_password,
)
from ..db import tables as t
from ..settings import Settings, is_private_host
from . import local

SIGN_IN_EXPIRED = "Your sign-in expired. Ask the host for a new invite link."
INVITE_EXPIRED = "This invite link has expired. Ask for a new link."
INVITE_NEEDS_HTTPS = "Set up HTTPS or allow your private network before inviting people from other computers."
INVITE_SIGN_IN_FIRST = "Sign in first, then open the invite link again."


class SignInExpired(DomainError):
    """A self-run team server's person or agent credential that has expired. Routes with
    optional sign-in treat it as signed out, so the person can redeem a new link."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.unauthenticated, SIGN_IN_EXPIRED)


def _user(r) -> User:  # noqa: ANN001
    return User(id=r.id, display_name=r.display_name, email=r.email, created_at=r.created_at)


def project_contract(r) -> Project:  # noqa: ANN001
    return Project(id=r.id, workspace_id=r.workspace_id, slug=r.slug, name=r.name,
                   objective=r.objective, created_at=r.created_at, revision=r.revision)


def _membership(r) -> Membership:  # noqa: ANN001
    return Membership(id=r.id, workspace_id=r.workspace_id, project_id=r.project_id,
                      user_id=r.user_id, role=Role(r.role), revoked_at=r.revoked_at,
                      revision=r.revision)


def agent_contract(r) -> AgentProfile:  # noqa: ANN001
    return AgentProfile(id=r.id, workspace_id=r.workspace_id, owner_user_id=r.owner_user_id,
                        kind=AgentKind(r.kind), name=r.name,
                        lead_model_endpoint_id=r.lead_model_endpoint_id,
                        max_delegation_depth=r.max_delegation_depth, created_at=r.created_at)


# ----------------------------------------------------------------- accounts/sessions


def register(conn: Connection, now: datetime, body: A.RegisterAccount) -> User:
    uid = new_id()
    try:
        conn.execute(sa.insert(t.users).values(
            id=uid, email=body.email.lower(), display_name=body.display_name,
            password_hash=hash_password(body.password.get_secret_value()), created_at=now))
    except IntegrityError as e:
        # Same response shape as a validation error; do not confirm which emails exist.
        raise DomainError(ErrorCode.validation_error, "registration failed") from e
    return _user(conn.execute(sa.select(t.users).where(t.users.c.id == uid)).one())


def login(conn: Connection, now: datetime, body: A.Login, ttl: timedelta) -> A.SessionToken:
    row = conn.execute(sa.select(t.users).where(t.users.c.email == body.email.lower())).first()
    ok = verify_password(body.password.get_secret_value(), row.password_hash if row else None)
    if not ok or row is None or row.disabled_at is not None:
        raise DomainError(ErrorCode.unauthenticated, "invalid credentials")
    return issue_session(conn, now, row, ttl)


def issue_session(conn: Connection, now: datetime, user_row, ttl: timedelta) -> A.SessionToken:  # noqa: ANN001
    """A human session (hs_). Callers have already authenticated the person."""
    token = new_token("hs")
    conn.execute(sa.insert(t.credentials).values(
        id=new_id(), kind="session", token_hash=token_hash(token), user_id=user_row.id,
        created_at=now, expires_at=now + ttl))
    return A.SessionToken(token=token, user=_user(user_row), expires_at=now + ttl)


def user_row(conn: Connection, user_id: UUID):  # noqa: ANN201
    return conn.execute(sa.select(t.users).where(t.users.c.id == user_id)).one()


def me(conn: Connection, p: Principal) -> A.Me:
    u = user_row(conn, p.user_id)
    owner = conn.execute(sa.select(t.installation.c.owner_user_id)).scalar()
    return A.Me(user=_user(u), principal_kind=p.kind, agent_id=p.agent_id, worker_id=p.worker_id,
                dev_auth=p.via_dev_auth, install_owner=p.kind == "user" and owner == p.user_id)


def update_me(conn: Connection, p: Principal, body: UpdateMe) -> A.Me:
    """Change the caller's display name; their personal agents named after them follow."""
    require_human(p, "changing your name")
    old = conn.execute(sa.select(t.users.c.display_name).where(t.users.c.id == p.user_id)).scalar_one()
    if body.display_name != old:
        conn.execute(sa.update(t.users).where(t.users.c.id == p.user_id).values(display_name=body.display_name))
        local.rename_person_agents(conn, p.user_id, old, body.display_name)
    return me(conn, p)


def logout(conn: Connection, now: datetime, token: str) -> None:
    conn.execute(sa.update(t.credentials).where(t.credentials.c.token_hash == token_hash(token))
                 .values(revoked_at=now))


def authenticate(conn: Connection, now: datetime, token: str, *, local_env: bool = False) -> Principal | None:
    """The principal for a bearer token, or None. On a self-run team server (local_env)
    an expired person or agent credential raises 401 telling them how to get back in;
    unknown, revoked and disabled credentials still return None."""
    c = t.credentials.c
    row = conn.execute(
        sa.select(t.credentials, t.users.c.disabled_at)
        .join(t.users, t.users.c.id == c.user_id)
        .where(c.token_hash == token_hash(token), c.revoked_at.is_(None))
    ).first()
    if row is None or row.disabled_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= now:
        if local_env and row.kind in ("session", "agent"):
            raise SignInExpired()
        return None
    if row.kind == "worker":
        w = conn.execute(sa.select(t.workers.c.revoked_at).where(t.workers.c.id == row.worker_id)).first()
        if w is None or w.revoked_at is not None:
            return None
        return Principal(kind="worker", user_id=row.user_id, worker_id=row.worker_id)
    if row.kind == "agent":
        return Principal(kind="agent", user_id=row.user_id, agent_id=row.agent_id)
    if row.kind == "executor":
        return Principal(kind="executor", user_id=row.user_id, binding_id=row.binding_id)
    return Principal(kind="user", user_id=row.user_id)


def dev_principal(conn: Connection, now: datetime, email: str) -> Principal:
    """Development-only: resolve (or create) a user by email. Callers must have checked
    Settings.dev_auth, which cannot be enabled outside development/test."""
    row = conn.execute(sa.select(t.users).where(t.users.c.email == email.lower())).first()
    if row is None:
        uid = new_id()
        conn.execute(sa.insert(t.users).values(id=uid, email=email.lower(),
                                               display_name=email.split("@")[0], created_at=now))
    else:
        uid = row.id
    return Principal(kind="user", user_id=uid, via_dev_auth=True)


# ------------------------------------------------------------- workspaces/projects


def create_workspace(conn: Connection, now: datetime, p: Principal, body: A.CreateWorkspace) -> Workspace:
    require_human(p, "creating a workspace")
    wid = new_id()
    conn.execute(sa.insert(t.workspaces).values(id=wid, name=body.name, created_by=p.user_id, created_at=now))
    conn.execute(sa.insert(t.workspace_members).values(workspace_id=wid, user_id=p.user_id,
                                                       role="owner", created_at=now))
    return Workspace(id=wid, name=body.name, created_at=now)


def _access(row, role: str) -> WorkspaceAccess:  # noqa: ANN001
    return WorkspaceAccess(id=row.id, name=row.name, created_at=row.created_at, role=role)


def list_workspaces(conn: Connection, p: Principal, limit: int) -> list[WorkspaceAccess]:
    require_human(p, "opening the workspace picker")
    ws, members = t.workspaces, t.workspace_members
    rows = conn.execute(sa.select(ws, members.c.role).join(
        members, members.c.workspace_id == ws.c.id,
    ).where(members.c.user_id == p.user_id, members.c.revoked_at.is_(None))
        .order_by(ws.c.created_at, ws.c.id).limit(limit))
    return [_access(row, row.role) for row in rows]


def list_workspace_members(conn: Connection, p: Principal, workspace_id: UUID,
                           limit: int) -> list[WorkspaceMemberView]:
    require_human(p, "viewing workspace members")
    require_workspace_member(conn, p, workspace_id)
    members, users = t.workspace_members, t.users
    rows = conn.execute(sa.select(members.c.user_id, users.c.display_name, members.c.role).join(
        users, members.c.user_id == users.c.id,
    ).where(members.c.workspace_id == workspace_id, members.c.revoked_at.is_(None),
            users.c.disabled_at.is_(None)).order_by(members.c.created_at, members.c.user_id).limit(limit))
    return [WorkspaceMemberView(**row._mapping) for row in rows]


def _invite_base_url(settings: Settings) -> str:
    """The public base URL for join links. A link that leaves this computer must travel
    over HTTPS, or stay on a private network (LAN/Tailscale) the host chose to allow."""
    base = settings.public_base_url.rstrip("/")
    url = urlsplit(base)
    host = (url.hostname or "").lower()
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not (loopback or url.scheme == "https" or (settings.private_network and is_private_host(host))):
        raise DomainError(ErrorCode.validation_error, INVITE_NEEDS_HTTPS)
    return base


def create_workspace_invite(conn: Connection, now: datetime, p: Principal, workspace_id: UUID,
                            body: CreateInvite, settings: Settings) -> WorkspaceInvite:
    """A limited-use join link, valid for 7 days. With a project, joining also grants
    editor on it; only an owner of that project can invite to it."""
    require_human(p, "creating a workspace invitation")
    if require_workspace_member(conn, p, workspace_id) != "owner":
        raise DomainError(ErrorCode.permission_denied, "workspace owner required")
    base = _invite_base_url(settings)
    if body.project_id is not None:
        pr = t.projects.c
        if conn.execute(sa.select(pr.id).where(pr.id == body.project_id,
                                               pr.workspace_id == workspace_id)).first() is None:
            raise not_found("project")
        require_role(conn, p, body.project_id, "owner")
    token = new_token("hwi")
    expires_at = now + timedelta(days=7)
    conn.execute(sa.insert(t.workspace_invites).values(
        token_hash=token_hash(token), workspace_id=workspace_id,
        created_by=p.user_id, expires_at=expires_at,
        project_id=body.project_id, max_uses=body.max_uses, uses=0,
    ))
    return WorkspaceInvite(token=token, expires_at=expires_at, url=f"{base}/#join={token}",
                           max_uses=body.max_uses)


def _use_invite(conn: Connection, now: datetime, token: str):  # noqa: ANN202
    """Take one use of an invite link with a single compare-and-set UPDATE. It matches
    only while the link has uses left, has not expired, and its creator is still an
    active owner of the workspace (and of the invite's project, when set). Returns the
    invite row, or None when the link is spent, expired, unknown or orphaned."""
    inv = t.workspace_invites
    wm, users, pm = t.workspace_members.c, t.users.c, t.memberships.c
    inviter_owns_workspace = sa.select(wm.user_id).select_from(
        t.workspace_members.join(t.users, users.id == wm.user_id),
    ).where(
        wm.workspace_id == inv.c.workspace_id, wm.user_id == inv.c.created_by, wm.role == "owner",
        wm.revoked_at.is_(None), users.disabled_at.is_(None),
    ).correlate(inv).exists()
    inviter_owns_project = sa.select(pm.id).where(
        pm.project_id == inv.c.project_id, pm.user_id == inv.c.created_by, pm.role == "owner",
        pm.revoked_at.is_(None),
    ).correlate(inv).exists()
    h = token_hash(token)
    used = conn.execute(sa.update(inv).where(
        inv.c.token_hash == h, inv.c.uses < inv.c.max_uses, inv.c.expires_at > now,
        inviter_owns_workspace, sa.or_(inv.c.project_id.is_(None), inviter_owns_project),
    ).values(uses=inv.c.uses + 1))
    if used.rowcount != 1:
        return None
    return conn.execute(sa.select(inv).where(inv.c.token_hash == h)).one()


def _ensure_workspace_member(conn: Connection, now: datetime, workspace_id: UUID, user_id: UUID) -> str | None:
    """Add the user as a workspace member if they are not one. Returns their role, or
    None when their membership was revoked (an invite link never undoes a removal)."""
    members = t.workspace_members
    member = sa.select(members.c.role, members.c.revoked_at).where(
        members.c.workspace_id == workspace_id, members.c.user_id == user_id,
    )
    existing = conn.execute(member).first()
    if existing is None:
        try:
            with conn.begin_nested():
                conn.execute(sa.insert(members).values(workspace_id=workspace_id, user_id=user_id,
                                                       role="member", created_at=now))
        except IntegrityError:
            # A concurrent redemption of this link can insert the same membership.
            if conn.execute(member).first() is None:
                raise
        existing = conn.execute(member).one()
    return None if existing.revoked_at is not None else existing.role


def _ensure_project_editor(conn: Connection, now: datetime, workspace_id: UUID, project_id: UUID,
                           user_id: UUID) -> bool:
    """Make the user at least a project member with editor rights when they are not a
    member yet; an existing role is left alone. False when their membership was revoked."""
    m = t.memberships.c
    member = sa.select(m.revoked_at).where(m.project_id == project_id, m.user_id == user_id)
    existing = conn.execute(member).first()
    if existing is None:
        try:
            with conn.begin_nested():
                conn.execute(sa.insert(t.memberships).values(
                    id=new_id(), workspace_id=workspace_id, project_id=project_id, user_id=user_id,
                    role="editor", created_at=now))
        except IntegrityError:
            if conn.execute(member).first() is None:
                raise
        existing = conn.execute(member).one()
    return existing.revoked_at is None


def join_workspace(conn: Connection, now: datetime, p: Principal,
                   body: A.JoinWorkspace) -> WorkspaceAccess:
    """Join with an invite link while signed in. Joining a workspace you are already in
    is idempotent and does not use up the link; otherwise it takes one use."""
    require_human(p, "joining a workspace")
    invites, members, users = t.workspace_invites, t.workspace_members, t.users
    invitation = conn.execute(sa.select(invites.c.workspace_id).join(
        members, sa.and_(members.c.workspace_id == invites.c.workspace_id,
                        members.c.user_id == invites.c.created_by),
    ).join(users, users.c.id == invites.c.created_by).where(
        invites.c.token_hash == token_hash(body.token), invites.c.expires_at > now,
        members.c.role == "owner", members.c.revoked_at.is_(None), users.c.disabled_at.is_(None),
    )).first()
    if invitation is None:
        raise not_found("valid workspace invitation")
    wid = invitation.workspace_id
    already = conn.execute(sa.select(members.c.role).where(
        members.c.workspace_id == wid, members.c.user_id == p.user_id)).first()
    if already is None and _use_invite(conn, now, body.token) is None:
        raise not_found("valid workspace invitation")
    role = _ensure_workspace_member(conn, now, wid, p.user_id)
    if role is None:
        raise not_found("valid workspace invitation")
    row = conn.execute(sa.select(t.workspaces).where(t.workspaces.c.id == wid)).one()
    return _access(row, role)


def redeem_invite(conn: Connection, now: datetime, settings: Settings, token: str, display_name: str,
                  p: Principal | None, with_agent_credential: bool, agent_label: str | None = None) -> InviteSession:
    """Redeem an invite link. Signed out: creates a person with only a name (no email,
    no password) and a session. Signed in: joins that person. Either way they become a
    workspace member, an editor of the invite's project (or, on a self-run server, of the
    team project), and get their personal agent; optionally an agent credential for the
    bridge. Every redemption takes one use. Every failure is the same 404, and the
    transaction rolls back, so a failed redemption uses nothing and creates nothing.

    Signed-out redemption exists only on a self-run team server (BRIDGE_ENV=local): elsewhere
    a person without email or password could never sign in again, so it is refused (401)
    before the link is touched."""
    if p is None and not settings.is_local:
        raise DomainError(ErrorCode.unauthenticated, INVITE_SIGN_IN_FIRST)
    if p is not None:
        require_human(p, "redeeming an invite link")
    gone = DomainError(ErrorCode.not_found, INVITE_EXPIRED)
    invite = _use_invite(conn, now, token)
    if invite is None:
        raise gone
    ttl = timedelta(hours=settings.session_ttl_hours)
    session = None
    if p is None:
        uid = new_id()
        conn.execute(sa.insert(t.users).values(id=uid, email=None, display_name=display_name,
                                               password_hash=None, created_at=now))
        session = issue_session(conn, now, user_row(conn, uid), ttl)
    else:
        uid = p.user_id
    wid = invite.workspace_id
    role = _ensure_workspace_member(conn, now, wid, uid)
    if role is None:
        raise gone
    project_id = invite.project_id
    if project_id is None and settings.is_local:
        inst = local.installation(conn)
        if inst is not None and inst.workspace_id == wid:
            project_id = inst.project_id
    if project_id is not None and not _ensure_project_editor(conn, now, wid, project_id, uid):
        raise gone
    agent_id = local.ensure_person_agent(conn, now, uid, wid, agent_label)
    agent_token = None
    if with_agent_credential:
        agent_token = issue_agent_credential(conn, now, Principal(kind="user", user_id=uid),
                                             agent_id, ttl).token
    ws = conn.execute(sa.select(t.workspaces).where(t.workspaces.c.id == wid)).one()
    return InviteSession(session=session, agent_id=agent_id, agent_token=agent_token,
                         workspace=_access(ws, role), project_id=project_id)


def add_workspace_member(conn: Connection, now: datetime, p: Principal, workspace_id: UUID,
                         body: A.AddWorkspaceMember) -> None:
    require_human(p, "managing workspace members")
    if require_workspace_member(conn, p, workspace_id) != "owner":
        raise DomainError(ErrorCode.permission_denied, "workspace owner required")
    user = conn.execute(sa.select(t.users.c.id).where(t.users.c.email == body.email.lower())).first()
    if user is None:
        raise not_found("user")
    existing = conn.execute(sa.select(t.workspace_members).where(
        t.workspace_members.c.workspace_id == workspace_id,
        t.workspace_members.c.user_id == user.id)).first()
    if existing is None:
        conn.execute(sa.insert(t.workspace_members).values(
            workspace_id=workspace_id, user_id=user.id, role=body.role, created_at=now))
    else:
        conn.execute(sa.update(t.workspace_members).where(
            t.workspace_members.c.workspace_id == workspace_id,
            t.workspace_members.c.user_id == user.id).values(role=body.role, revoked_at=None))


def create_project(conn: Connection, now: datetime, p: Principal, body: A.CreateProject) -> Project:
    require_human(p, "creating a project")
    require_workspace_member(conn, p, body.workspace_id)
    pid = new_id()
    try:
        conn.execute(sa.insert(t.projects).values(
            id=pid, workspace_id=body.workspace_id, slug=body.slug, name=body.name,
            objective=body.objective, currency=body.budget_limit.currency,
            budget_limit_micros=body.budget_limit.amount_micros, created_at=now))
    except IntegrityError as e:
        raise DomainError(ErrorCode.validation_error, "slug already used in workspace") from e
    conn.execute(sa.insert(t.memberships).values(
        id=new_id(), workspace_id=body.workspace_id, project_id=pid, user_id=p.user_id,
        role="owner", created_at=now))
    return get_project(conn, p, pid)


def get_project(conn: Connection, p: Principal, project_id: UUID) -> Project:
    require_role(conn, p, project_id, "viewer")
    return project_contract(conn.execute(sa.select(t.projects).where(t.projects.c.id == project_id)).one())


def list_projects(conn: Connection, p: Principal) -> list[Project]:
    rows = conn.execute(
        sa.select(t.projects).join(t.memberships, t.memberships.c.project_id == t.projects.c.id)
        .where(t.memberships.c.user_id == p.user_id, t.memberships.c.revoked_at.is_(None))
        .order_by(t.projects.c.created_at)
    ).all()
    return [project_contract(r) for r in rows]


def budget_status(conn: Connection, p: Principal, project_id: UUID) -> A.BudgetStatus:
    require_role(conn, p, project_id, "viewer")
    r = conn.execute(sa.select(t.projects).where(t.projects.c.id == project_id)).one()
    cur = r.currency
    return A.BudgetStatus(
        project_id=project_id, limit=Money(amount_micros=r.budget_limit_micros, currency=cur),
        reserved=Money(amount_micros=r.budget_reserved_micros, currency=cur),
        spent=Money(amount_micros=r.budget_spent_micros, currency=cur), revision=r.revision)


def set_budget(conn: Connection, p: Principal, project_id: UUID, body: A.SetBudget) -> A.BudgetStatus:
    require_human(p, "changing the budget")
    require_role(conn, p, project_id, "owner")
    pr = t.projects.c
    cas(conn, sa.update(t.projects).where(
        pr.id == project_id, pr.revision == body.expected_revision,
        pr.budget_reserved_micros + pr.budget_spent_micros <= body.limit.amount_micros,
    ).values(budget_limit_micros=body.limit.amount_micros, revision=pr.revision + 1), "budget")
    return budget_status(conn, p, project_id)


# --------------------------------------------------------------------- memberships


def add_member(conn: Connection, now: datetime, p: Principal, project_id: UUID,
               body: A.AddMember) -> Membership:
    require_human(p, "managing members")
    require_role(conn, p, project_id, "owner")
    proj = conn.execute(sa.select(t.projects).where(t.projects.c.id == project_id)).one()
    user = conn.execute(sa.select(t.users.c.id).where(t.users.c.email == body.email.lower())).first()
    if user is None:
        raise not_found("user")
    ws = conn.execute(sa.select(t.workspace_members).where(
        t.workspace_members.c.workspace_id == proj.workspace_id,
        t.workspace_members.c.user_id == user.id,
        t.workspace_members.c.revoked_at.is_(None))).first()
    if ws is None:
        raise DomainError(ErrorCode.policy_denied, "user must be a workspace member first")
    m = t.memberships.c
    existing = conn.execute(sa.select(t.memberships).where(m.project_id == project_id,
                                                           m.user_id == user.id)).first()
    if existing is None:
        mid = new_id()
        conn.execute(sa.insert(t.memberships).values(
            id=mid, workspace_id=proj.workspace_id, project_id=project_id, user_id=user.id,
            role=body.role.value, created_at=now))
    else:
        mid = existing.id
        conn.execute(sa.update(t.memberships).where(m.id == mid).values(
            role=body.role.value, revoked_at=None, revision=m.revision + 1))
    return _membership(conn.execute(sa.select(t.memberships).where(m.id == mid)).one())


def list_members(conn: Connection, p: Principal, project_id: UUID) -> list[Membership]:
    require_role(conn, p, project_id, "viewer")
    return [_membership(r) for r in conn.execute(
        sa.select(t.memberships).where(t.memberships.c.project_id == project_id)
        .order_by(t.memberships.c.created_at)).all()]


def revoke_member(conn: Connection, now: datetime, p: Principal, project_id: UUID,
                  user_id: UUID, expected_revision: int) -> Membership:
    """Revocation: future access, assignments and credential grants stop. Leased work
    held by the revoked member's agents is released so it can be rescheduled."""
    from . import scheduling  # local import to avoid cycle

    require_human(p, "revoking members")
    require_role(conn, p, project_id, "owner")
    m = t.memberships.c
    row = conn.execute(sa.select(t.memberships).where(m.project_id == project_id,
                                                      m.user_id == user_id)).first()
    if row is None:
        raise not_found("membership")
    if row.role == "owner":
        owners = conn.execute(sa.select(sa.func.count()).where(
            m.project_id == project_id, m.role == "owner", m.revoked_at.is_(None))).scalar()
        if owners <= 1:
            raise DomainError(ErrorCode.policy_denied, "cannot revoke the last owner")
    cas(conn, sa.update(t.memberships).where(m.id == row.id, m.revision == expected_revision,
                                             m.revoked_at.is_(None))
        .values(revoked_at=now, revision=m.revision + 1), "membership")
    conn.execute(sa.update(t.payer_grants).where(
        t.payer_grants.c.project_id == project_id,
        sa.or_(t.payer_grants.c.grantee_user_id == user_id, t.payer_grants.c.grantor_user_id == user_id),
        t.payer_grants.c.revoked_at.is_(None)).values(revoked_at=now))
    scheduling.release_member_work(conn, now, project_id, user_id)
    return _membership(conn.execute(sa.select(t.memberships).where(m.id == row.id)).one())


# --------------------------------------------------------------------------- agents


def create_agent(conn: Connection, now: datetime, p: Principal, body: A.CreateAgent) -> AgentProfile:
    require_human(p, "creating agents")
    require_workspace_member(conn, p, body.workspace_id)
    aid = new_id()
    local.insert_agent(conn,
        id=aid, workspace_id=body.workspace_id, owner_user_id=p.user_id, kind=body.kind.value,
        name=body.name, max_delegation_depth=body.max_delegation_depth, skills=body.skills,
        lead_model_endpoint_id=None, created_at=now)
    return agent_contract(conn.execute(sa.select(t.agent_profiles).where(t.agent_profiles.c.id == aid)).one())


def list_owned_agents(conn: Connection, p: Principal, workspace_id: UUID, limit: int) -> list[AgentProfile]:
    require_human(p, "viewing personal agents")
    require_workspace_member(conn, p, workspace_id)
    agents = t.agent_profiles
    rows = conn.execute(sa.select(agents).where(
        agents.c.workspace_id == workspace_id, agents.c.owner_user_id == p.user_id,
    ).order_by(agents.c.created_at, agents.c.id).limit(limit))
    return [agent_contract(row) for row in rows]


def owned_agent(conn: Connection, p: Principal, agent_id: UUID):  # noqa: ANN201
    row = conn.execute(sa.select(t.agent_profiles).where(t.agent_profiles.c.id == agent_id)).first()
    if row is None or row.owner_user_id != p.user_id:
        raise not_found("agent")
    if p.kind == "agent" and p.agent_id != agent_id:
        raise not_found("agent")
    return row


def issue_agent_credential(conn: Connection, now: datetime, p: Principal, agent_id: UUID,
                           ttl: timedelta) -> A.AgentCredential:
    require_human(p, "issuing agent credentials")
    owned_agent(conn, p, agent_id)
    token = new_token("ha")
    conn.execute(sa.insert(t.credentials).values(
        id=new_id(), kind="agent", token_hash=token_hash(token), user_id=p.user_id,
        agent_id=agent_id, created_at=now, expires_at=now + ttl))
    return A.AgentCredential(agent_id=agent_id, token=token, expires_at=now + ttl)


def create_sibling_agent(conn: Connection, now: datetime, p: Principal, label: str,
                         ttl: timedelta) -> SiblingAgent:
    """Another named agent of the calling agent's person, in the same workspace (e.g. their
    Codex next to their Claude), with a fresh credential. Asking again for an existing label
    reissues that agent's credential: the way back in after losing the key."""
    if p.kind != "agent" or p.agent_id is None:
        raise DomainError(ErrorCode.permission_denied, "Only an agent's bridge can add an agent this way.")
    if label.casefold() in (local.MAIN_LABEL, local.MAIN_ADDRESS_LABEL):
        raise DomainError(ErrorCode.validation_error, "That is your main agent's name. Pick another.")
    a = t.agent_profiles.c
    me_row = conn.execute(sa.select(t.agent_profiles).where(a.id == p.agent_id)).one()
    require_workspace_member(conn, p, me_row.workspace_id)
    person = conn.execute(sa.select(t.users.c.display_name).where(t.users.c.id == me_row.owner_user_id)).scalar_one()
    name = local.sibling_agent_name(person, label)
    owned = conn.execute(sa.select(a.id, a.name, a.kind).where(
        a.workspace_id == me_row.workspace_id, a.owner_user_id == me_row.owner_user_id,
    ).order_by(a.created_at, a.id)).all()
    same = next((r for r in owned if r.name.casefold() == name.casefold()), None)
    if same is not None and same.kind != AgentKind.specialist.value:
        # Only agents added this way are re-keyed here; never the main agent or others.
        raise DomainError(ErrorCode.validation_error, "That name is taken by another of your agents.")
    if same is not None:
        aid, name = same.id, same.name
    else:
        if len(owned) >= local.MAX_AGENTS_PER_PERSON:
            raise DomainError(ErrorCode.validation_error,
                              f"You already have {local.MAX_AGENTS_PER_PERSON} agents. Remove one first.")
        aid = new_id()
        local.insert_agent(conn,
            id=aid, workspace_id=me_row.workspace_id, owner_user_id=me_row.owner_user_id,
            kind=AgentKind.specialist.value, name=name, max_delegation_depth=2, skills=[],
            lead_model_endpoint_id=None, created_at=now)
    # Issued for the person who owns both agents, exactly as their own key would be.
    cred = issue_agent_credential(conn, now, Principal(kind="user", user_id=me_row.owner_user_id), aid, ttl)
    return SiblingAgent(agent_id=aid, name=name, label=local.agent_label(person, name), agent_token=cred.token)


def set_lead_model(conn: Connection, p: Principal, agent_id: UUID, endpoint_id: UUID | None) -> AgentProfile:
    from .models import endpoint_contract_for

    require_human(p, "changing an agent's lead model")
    owned_agent(conn, p, agent_id)
    if endpoint_id is not None:
        ep = endpoint_contract_for(conn, p, endpoint_id)
        if not ep.orchestrator_qualified:
            raise DomainError(ErrorCode.policy_denied,
                              "model has not passed tool-calling and JSON-schema probes")
    conn.execute(sa.update(t.agent_profiles).where(t.agent_profiles.c.id == agent_id)
                 .values(lead_model_endpoint_id=endpoint_id))
    return agent_contract(conn.execute(sa.select(t.agent_profiles).where(t.agent_profiles.c.id == agent_id)).one())


def _consent(r, agent_id: UUID, project_id: UUID, now: datetime) -> AgentDelegationConsent:  # noqa: ANN001
    if r is None:
        return AgentDelegationConsent(agent_id=agent_id, project_id=project_id, updated_at=now)
    return AgentDelegationConsent(agent_id=r.agent_id, project_id=r.project_id,
                                  accept_delegations=r.accept_delegations,
                                  allowed_skills=r.allowed_skills, revision=r.revision,
                                  updated_at=r.updated_at)


def set_consent(conn: Connection, now: datetime, p: Principal, agent_id: UUID, project_id: UUID,
                body: AgentDelegationConsentUpdate) -> AgentDelegationConsent:
    """Human-only: an agent cannot grant itself permission to accept work."""
    require_human(p, "changing delegation consent")
    owned_agent(conn, p, agent_id)
    require_role(conn, p, project_id, "editor")
    c = t.agent_consents.c
    row = conn.execute(sa.select(t.agent_consents).where(c.agent_id == agent_id, c.project_id == project_id)).first()
    if row is None:
        if body.expected_revision != 0:
            raise DomainError(ErrorCode.revision_conflict, "consent revision mismatch", current_revision=0)
        conn.execute(sa.insert(t.agent_consents).values(
            agent_id=agent_id, project_id=project_id, accept_delegations=body.accept_delegations,
            allowed_skills=body.allowed_skills, revision=1, updated_at=now))
    else:
        cas(conn, sa.update(t.agent_consents).where(
            c.agent_id == agent_id, c.project_id == project_id, c.revision == body.expected_revision
        ).values(accept_delegations=body.accept_delegations, allowed_skills=body.allowed_skills,
                 revision=c.revision + 1, updated_at=now), "consent")
    row = conn.execute(sa.select(t.agent_consents).where(c.agent_id == agent_id, c.project_id == project_id)).first()
    return _consent(row, agent_id, project_id, now)


def get_consent(conn: Connection, now: datetime, p: Principal, agent_id: UUID, project_id: UUID) -> AgentDelegationConsent:
    require_role(conn, p, project_id, "viewer")
    c = t.agent_consents.c
    row = conn.execute(sa.select(t.agent_consents).where(c.agent_id == agent_id, c.project_id == project_id)).first()
    return _consent(row, agent_id, project_id, now)


def actor_of(p: Principal) -> tuple[str, UUID]:
    if p.kind == "agent":
        return ActorKind.agent.value, p.agent_id
    if p.kind == "worker":
        return ActorKind.worker.value, p.worker_id
    return ActorKind.human.value, p.user_id
