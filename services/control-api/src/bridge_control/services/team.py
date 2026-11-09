"""Team board: one seat per Claude Code session, so teammates see who is working on what, and
exclusive, expiring file claims so two seats never edit the same file at once."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import ClaimsChanged, SeatLeft, SeatUpdated
from ..contracts.identity import AgentKind
from ..contracts.team import Board, BoardAgent, BoardPerson, Claim, ClaimRequest, ClaimResult, ReleaseRequest, Seat, SeatUpdate
from ..core import Principal, new_id, not_found, require_role
from ..db import tables as t
from ..reliability import emit, encode_cursor

ONLINE_WINDOW = timedelta(minutes=15)
_SEAT_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_DRIVE = re.compile(r"^[A-Za-z]:")
MAX_CLAIM_PATH = 300


def _agent_only(p: Principal) -> UUID:
    if p.kind != "agent" or p.agent_id is None:
        raise DomainError(ErrorCode.permission_denied, "only an agent's session can hold a seat")
    return p.agent_id


def _check_key(seat_key: str) -> None:
    if not _SEAT_KEY.match(seat_key):
        raise DomainError(ErrorCode.validation_error, "seat_key must be 8-64 letters, digits, _ or -")


def _version(now: datetime) -> int:
    return int(now.timestamp() * 1000)  # monotonic per seat; seats carry no revision column


def seat_contract(conn: Connection, row, now: datetime) -> Seat:  # noqa: ANN001
    who = conn.execute(
        sa.select(t.users.c.display_name, t.agent_profiles.c.name).select_from(
            t.agent_profiles.join(t.users, t.users.c.id == t.agent_profiles.c.owner_user_id))
        .where(t.agent_profiles.c.id == row.agent_id)).one()
    return Seat(seat_key=row.seat_key, agent_id=row.agent_id, person_name=who.display_name, label=row.label,
                status=row.status, status_text=row.status_text, task_id=row.task_id,
                last_seen_at=row.last_seen_at, online=now - row.last_seen_at <= ONLINE_WINDOW,
                agent_name=who.name)


def upsert_seat(conn: Connection, now: datetime, p: Principal, project_id: UUID, seat_key: str,
                body: SeatUpdate) -> Seat:
    agent_id = _agent_only(p)
    _check_key(seat_key)
    require_role(conn, p, project_id, "editor")
    if body.task_id is not None:
        ok = conn.execute(sa.select(t.tasks.c.id).where(
            t.tasks.c.id == body.task_id, t.tasks.c.project_id == project_id)).first()
        if ok is None:
            raise DomainError(ErrorCode.validation_error, f"task {body.task_id} not in project")
    s = t.agent_seats.c
    row = conn.execute(sa.select(t.agent_seats).where(
        s.agent_id == agent_id, s.seat_key == seat_key, s.project_id == project_id)).first()
    if row is None:
        sid = new_id()
        conn.execute(sa.insert(t.agent_seats).values(
            id=sid, project_id=project_id, agent_id=agent_id, seat_key=seat_key, label=body.label,
            status=body.status, status_text=body.status_text, task_id=body.task_id,
            last_seen_at=now, created_at=now))
        changed = True
    else:
        sid = row.id
        changed = (row.status, row.status_text, row.task_id) != (body.status, body.status_text, body.task_id)
        conn.execute(sa.update(t.agent_seats).where(s.id == sid).values(
            label=body.label, status=body.status, status_text=body.status_text, task_id=body.task_id,
            last_seen_at=now))
    row = conn.execute(sa.select(t.agent_seats).where(s.id == sid)).one()
    if changed:  # a heartbeat with nothing new emits nothing
        emit(conn, now, project_id, sid, _version(now),
             SeatUpdated(seat_key=seat_key, agent_id=agent_id, label=row.label, status=row.status,
                         status_text=row.status_text, task_id=row.task_id))
    return seat_contract(conn, row, now)


def leave_seat(conn: Connection, now: datetime, p: Principal, project_id: UUID, seat_key: str) -> None:
    agent_id = _agent_only(p)
    _check_key(seat_key)
    require_role(conn, p, project_id, "editor")
    s = t.agent_seats.c
    row = conn.execute(sa.select(t.agent_seats).where(
        s.agent_id == agent_id, s.seat_key == seat_key, s.project_id == project_id)).first()
    if row is None:
        raise not_found("seat")
    _release(conn, now, project_id, agent_id, seat_key, None)
    conn.execute(sa.delete(t.agent_seats).where(s.id == row.id))
    emit(conn, now, project_id, row.id, _version(now), SeatLeft(seat_key=seat_key, agent_id=agent_id))


# ---------------------------------------------------------------- file claims


def _bad_path(message: str) -> DomainError:
    return DomainError(ErrorCode.validation_error, message)


def normalize_claim_path(path: str) -> str:
    """Repository-relative, '/'-separated, lowercase; a trailing '/' marks a folder."""
    s = path.strip().replace("\\", "/")
    if not s:
        raise _bad_path("a claim path is empty")
    if len(s) > MAX_CLAIM_PATH:
        raise _bad_path(f"a claim path is longer than {MAX_CLAIM_PATH} characters")
    if s.startswith("/") or _DRIVE.match(s):
        raise _bad_path(f"{s} is not relative to the repository")
    parts = [seg for seg in s.split("/") if seg not in ("", ".")]  # drops ./ and doubled slashes
    if ".." in parts:
        raise _bad_path(f"{s} goes outside the repository (..)")
    if not parts:
        raise _bad_path("a claim path is empty")
    folder = s.endswith("/") or s.split("/")[-1] == "."  # "src/." is the folder src/
    return ("/".join(parts) + ("/" if folder else "")).lower()


def _overlaps(a: str, b: str) -> bool:
    """Same key, or one is inside the other. A key without a trailing '/' may still be a
    folder ("src"), so it also covers everything under it ("src/a.py")."""
    return a == b or b.startswith(a.rstrip("/") + "/") or a.startswith(b.rstrip("/") + "/")


def _claims_entity(project_id: UUID, agent_id: UUID, seat_key: str) -> UUID:
    """Stable event entity for one seat's claims, distinct from the seat's own entity."""
    return uuid5(project_id, f"claims/{agent_id}/{seat_key}")


def _active_claim_rows(conn: Connection, now: datetime, project_id: UUID) -> list:
    """Claims not released, not expired, whose seat still exists and was seen within 15 minutes."""
    c, s, a = t.file_claims.c, t.agent_seats.c, t.agent_profiles.c
    j = (t.file_claims
         .join(t.agent_seats, sa.and_(s.agent_id == c.agent_id, s.seat_key == c.seat_key,
                                      s.project_id == c.project_id))
         .join(t.agent_profiles, a.id == c.agent_id)
         .join(t.users, t.users.c.id == a.owner_user_id))
    return conn.execute(
        sa.select(c.id, c.agent_id, c.seat_key, c.path_key, c.path, c.intent, c.expires_at,
                  s.label.label("seat_label"), t.users.c.display_name.label("person_name"))
        .select_from(j)
        .where(c.project_id == project_id, c.released_at.is_(None), c.expires_at > now,
               s.last_seen_at >= now - ONLINE_WINDOW)
        .order_by(c.path_key, c.created_at)).all()


def _claim_contract(row) -> Claim:  # noqa: ANN001
    return Claim(path=row.path, intent=row.intent, agent_id=row.agent_id, person_name=row.person_name,
                 seat_label=row.seat_label, expires_at=row.expires_at)


def active_claims(conn: Connection, now: datetime, project_id: UUID) -> list[Claim]:
    return [_claim_contract(r) for r in _active_claim_rows(conn, now, project_id)]


def claim(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: ClaimRequest) -> ClaimResult:
    """All or nothing: if any path overlaps another seat's active claim, nothing is granted and
    `conflicts` lists the blockers. Re-claiming a path this seat holds renews it."""
    agent_id = _agent_only(p)
    _check_key(body.seat_key)
    require_role(conn, p, project_id, "editor")
    wanted: dict[str, str] = {}  # path_key -> path as given
    for raw in body.paths:
        wanted.setdefault(normalize_claim_path(raw), raw.strip())
    # Serialize claimants of this project before reading what is held. PostgreSQL takes the
    # row lock; SQLite ignores FOR UPDATE, but its BEGIN IMMEDIATE already serializes writers.
    conn.execute(sa.select(t.projects.c.id).where(t.projects.c.id == project_id).with_for_update()).one()
    s = t.agent_seats.c
    seat = conn.execute(sa.select(s.id).where(
        s.agent_id == agent_id, s.seat_key == body.seat_key, s.project_id == project_id)).first()
    if seat is None:
        raise not_found("seat")
    # A seat that claims is alive: this counts as its heartbeat.
    conn.execute(sa.update(t.agent_seats).where(s.id == seat.id).values(last_seen_at=now))

    def mine(r) -> bool:  # noqa: ANN001
        return r.agent_id == agent_id and r.seat_key == body.seat_key

    active = _active_claim_rows(conn, now, project_id)
    blockers = [r for r in active if not mine(r) and any(_overlaps(r.path_key, k) for k in wanted)]
    if blockers:
        return ClaimResult(granted=[], conflicts=[_claim_contract(r) for r in blockers])

    c = t.file_claims.c
    # Other seats' lapsed claims on these paths are superseded, so they cannot come back to
    # life when their quiet seat heartbeats again.
    others = conn.execute(sa.select(c.id, c.path_key).where(
        c.project_id == project_id, c.released_at.is_(None),
        sa.not_(sa.and_(c.agent_id == agent_id, c.seat_key == body.seat_key)))).all()
    stale = [r.id for r in others if any(_overlaps(r.path_key, k) for k in wanted)]
    if stale:
        conn.execute(sa.update(t.file_claims).where(c.id.in_(stale)).values(released_at=now))

    expires = now + timedelta(minutes=body.minutes)
    held = {r.path_key: r.id for r in active if mine(r)}
    for key, path in wanted.items():
        if key in held:
            conn.execute(sa.update(t.file_claims).where(c.id == held[key]).values(
                path=path, intent=body.intent, expires_at=expires))
        else:
            conn.execute(sa.insert(t.file_claims).values(
                id=new_id(), project_id=project_id, agent_id=agent_id, seat_key=body.seat_key,
                path_key=key, path=path, intent=body.intent, created_at=now, expires_at=expires))
    emit(conn, now, project_id, _claims_entity(project_id, agent_id, body.seat_key), _version(now),
         ClaimsChanged(seat_key=body.seat_key, agent_id=agent_id, paths=list(wanted.values())))
    granted = [r for r in _active_claim_rows(conn, now, project_id) if mine(r) and r.path_key in wanted]
    return ClaimResult(granted=[_claim_contract(r) for r in granted], conflicts=[])


def _release(conn: Connection, now: datetime, project_id: UUID, agent_id: UUID, seat_key: str,
             keys: set[str] | None) -> int:
    c = t.file_claims.c
    q = sa.select(c.id, c.path).where(
        c.project_id == project_id, c.agent_id == agent_id, c.seat_key == seat_key,
        c.released_at.is_(None), c.expires_at > now)
    if keys is not None:
        q = q.where(c.path_key.in_(keys))
    rows = conn.execute(q.order_by(c.path_key)).all()
    if not rows:
        return 0
    conn.execute(sa.update(t.file_claims).where(c.id.in_([r.id for r in rows])).values(released_at=now))
    emit(conn, now, project_id, _claims_entity(project_id, agent_id, seat_key), _version(now),
         ClaimsChanged(seat_key=seat_key, agent_id=agent_id, paths=[r.path for r in rows]))
    return len(rows)


def release(conn: Connection, now: datetime, p: Principal, project_id: UUID, body: ReleaseRequest) -> int:
    """Release some of a seat's claims, or all of them when `paths` is null. Returns how many."""
    agent_id = _agent_only(p)
    _check_key(body.seat_key)
    require_role(conn, p, project_id, "editor")
    keys = None if body.paths is None else {normalize_claim_path(x) for x in body.paths}
    return _release(conn, now, project_id, agent_id, body.seat_key, keys)


# ---------------------------------------------------------------- the board

FEED_LIMIT = 20
TASK_LIMIT = 20


def _feed_conversation(conn: Connection, project_id: UUID) -> UUID | None:
    inst = conn.execute(sa.select(t.installation.c.feed_conversation_id)
                        .where(t.installation.c.project_id == project_id)).first()
    if inst is not None:
        return inst.feed_conversation_id
    c = t.conversations.c
    return conn.execute(sa.select(c.id).where(c.project_id == project_id, c.scope == "project")
                        .order_by(c.created_at, c.id).limit(1)).scalar()


def board(conn: Connection, now: datetime, p: Principal, project_id: UUID) -> Board:
    """Readable by project members and their agents; anyone else gets 404."""
    from . import local, work  # work imports team-free modules; deferred to keep imports acyclic

    require_role(conn, p, project_id, "viewer")
    # people: active members, each with their personal agent and its seats here
    m = t.memberships.c
    members = conn.execute(
        sa.select(t.users.c.id, t.users.c.display_name, t.memberships.c.created_at)
        .select_from(t.memberships.join(t.users, t.users.c.id == m.user_id))
        .where(m.project_id == project_id, m.revoked_at.is_(None))
        .order_by(m.created_at, t.users.c.id)).all()
    workspace_id = conn.execute(sa.select(t.projects.c.workspace_id)
                                .where(t.projects.c.id == project_id)).scalar_one()
    people = []
    a, s = t.agent_profiles.c, t.agent_seats.c
    for u in members:
        # The main agent first, then the person's other named agents (their Codex, ...) by age.
        rows = conn.execute(
            sa.select(a.id, a.name, a.kind).where(
                a.workspace_id == workspace_id, a.owner_user_id == u.id,
                a.kind.in_((AgentKind.personal_orchestrator.value, AgentKind.specialist.value)))
            .order_by(a.created_at, a.id)).all()
        main = next((r for r in rows if r.kind == AgentKind.personal_orchestrator.value), None)
        ordered = ([main] if main else []) + [r for r in rows if r.kind == AgentKind.specialist.value]
        agents, every = [], []
        for r in ordered:
            seats = [seat_contract(conn, x, now) for x in conn.execute(
                sa.select(t.agent_seats).where(s.agent_id == r.id, s.project_id == project_id)
                .order_by(s.last_seen_at.desc(), s.seat_key)).all()]
            seats.sort(key=lambda x: not x.online)  # stable: online first, newest first within
            agents.append(BoardAgent(agent_id=r.id, name=r.name, label=local.agent_label(u.display_name, r.name),
                                     seats=seats))
            every += seats
        every.sort(key=lambda x: (not x.online, -x.last_seen_at.timestamp(), x.seat_key))
        people.append(BoardPerson(user_id=u.id, name=u.display_name, agent_id=main.id if main else None,
                                  seats=every, agents=agents))
    people.sort(key=lambda x: not any(s.online for s in x.seats))

    c = t.tasks.c
    task_rows = conn.execute(work._visible(
        sa.select(t.tasks).where(c.project_id == project_id, c.state.notin_(("done", "cancelled"))), p)
        .order_by(c.created_at.desc(), c.id.desc()).limit(TASK_LIMIT)).all()

    feed_id = _feed_conversation(conn, project_id)
    feed = []
    if feed_id is not None:
        ms = t.messages.c
        rows = conn.execute(sa.select(t.messages).where(ms.conversation_id == feed_id)
                            .order_by(ms.created_at.desc(), ms.id.desc()).limit(FEED_LIMIT)).all()
        feed = [work._msg(r) for r in reversed(rows)]

    inbox = 0
    if p.kind == "agent" and p.agent_id is not None:
        am = t.agent_messages.c
        inbox = conn.execute(sa.select(sa.func.count()).select_from(t.agent_messages).where(
            am.project_id == project_id, am.recipient_agent_id == p.agent_id,
            am.expires_at > now)).scalar_one()

    head = conn.execute(sa.select(t.projects.c.event_seq).where(t.projects.c.id == project_id)).scalar() or 0
    return Board(project_id=project_id, feed_conversation_id=feed_id, people=people,
                 claims=active_claims(conn, now, project_id),
                 tasks=[work.task_contract(conn, r) for r in task_rows], feed=feed, inbox_count=inbox,
                 head_cursor=encode_cursor(head) if head else None)
