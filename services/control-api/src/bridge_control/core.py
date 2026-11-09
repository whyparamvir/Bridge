"""Cross-cutting primitives: clock, ids/tokens, password hashing, principals and
authorization. Everything that decides access lives here or in services, never in models."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .contracts.errors import DomainError, ErrorCode
from .db import tables as t

# ------------------------------------------------------------------------- clock


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    """Test clock; thread-safe advance."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, **kw: float) -> datetime:
        with self._lock:
            self._now += timedelta(**kw)
            return self._now


# ----------------------------------------------------------------- ids and secrets


def new_id() -> UUID:
    return uuid4()


def new_token(prefix: str) -> str:
    """High-entropy bearer token (256 bits). Only its SHA-256 is stored."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def canonical_hash(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        hashlib.scrypt(b"x", salt=b"0" * 16, **_SCRYPT)  # equalize timing
        return False
    _, salt_hex, dk_hex = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---------------------------------------------------------------------- principals


@dataclass(frozen=True)
class Principal:
    """Authenticated caller. Agents and workers always act for their owning user, and
    never with more rights than that user; some operations are human-only."""

    kind: Literal["user", "agent", "worker", "executor"]
    user_id: UUID
    agent_id: UUID | None = None
    worker_id: UUID | None = None
    binding_id: UUID | None = None  # repository executor scope
    via_dev_auth: bool = False

    @property
    def scope_key(self) -> str:
        return f"{self.kind}:{self.agent_id or self.worker_id or self.binding_id or self.user_id}"


ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


def not_found(what: str = "resource") -> DomainError:
    return DomainError(ErrorCode.not_found, f"{what} not found")


def require_human(p: Principal, action: str) -> None:
    if p.kind != "user":
        raise DomainError(ErrorCode.permission_denied, f"{action} requires a human user")


def project_role(conn: Connection, p: Principal, project_id: UUID) -> str | None:
    """Device (worker) and repository-executor credentials are NEVER project members:
    they reach data only through attempt- or binding-scoped routes."""
    if p.kind not in ("user", "agent"):
        return None
    row = conn.execute(
        sa.select(t.memberships.c.role).where(
            t.memberships.c.project_id == project_id,
            t.memberships.c.user_id == p.user_id,
            t.memberships.c.revoked_at.is_(None),
        )
    ).first()
    if row is None:
        return None
    if p.kind == "agent":  # an agent is only a member through a non-revoked owner
        owner_ok = conn.execute(
            sa.select(t.agent_profiles.c.id).where(
                t.agent_profiles.c.id == p.agent_id, t.agent_profiles.c.owner_user_id == p.user_id
            )
        ).first()
        if owner_ok is None:
            return None
    return row.role


def require_role(conn: Connection, p: Principal, project_id: UUID, minimum: str = "viewer") -> str:
    """Non-members get not_found (existence is not leaked); members lacking the role get
    permission_denied."""
    role = project_role(conn, p, project_id)
    if role is None:
        raise not_found("project")
    if ROLE_RANK[role] < ROLE_RANK[minimum]:
        raise DomainError(ErrorCode.permission_denied, f"requires {minimum} role")
    return role


def require_workspace_member(conn: Connection, p: Principal, workspace_id: UUID) -> str:
    row = conn.execute(
        sa.select(t.workspace_members.c.role).where(
            t.workspace_members.c.workspace_id == workspace_id,
            t.workspace_members.c.user_id == p.user_id,
            t.workspace_members.c.revoked_at.is_(None),
        )
    ).first()
    if row is None:
        raise not_found("workspace")
    return row.role


def cas(conn: Connection, stmt: sa.Update, what: str, code: ErrorCode = ErrorCode.revision_conflict) -> None:
    """Execute a compare-and-set UPDATE; raise if it matched no row."""
    if conn.execute(stmt).rowcount != 1:
        raise DomainError(code, f"{what}: concurrent change or precondition failed")
