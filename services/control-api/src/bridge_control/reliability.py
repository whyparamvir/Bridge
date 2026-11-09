"""Events (ordered, reconnectable), transactional outbox, idempotency records and
external-operation records. All writes happen inside the caller's transaction."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import TypeAdapter
from sqlalchemy.engine import Connection

from .contracts.errors import DomainError, ErrorCode
from .contracts.events import EventEnvelope, EventPayload
from .core import Principal, canonical_hash, new_id
from .db import tables as t

_payload = TypeAdapter(EventPayload)

# ------------------------------------------------------------------------- events


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{seq}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        version, seq = raw.split(":")
        if version != "v1":
            raise ValueError
        return int(seq)
    except Exception as e:  # noqa: BLE001
        raise DomainError(ErrorCode.validation_error, "malformed cursor") from e


def emit(
    conn: Connection,
    now: datetime,
    project_id: UUID,
    entity_id: UUID,
    entity_version: int,
    payload: Any,
    audience_user_id: UUID | None = None,
) -> int:
    """Persist an event in the caller's transaction. Incrementing the project's
    event_seq takes its row lock, so commits are ordered by project_seq."""
    payload = _payload.validate_python(payload)
    seq = conn.execute(
        sa.update(t.projects)
        .where(t.projects.c.id == project_id)
        .values(event_seq=t.projects.c.event_seq + 1)
        .returning(t.projects.c.event_seq)
    ).scalar_one()
    conn.execute(
        sa.insert(t.events).values(
            event_id=new_id(), project_id=project_id, project_seq=seq, entity_id=entity_id,
            entity_version=entity_version, type=payload.type,
            payload=payload.model_dump(mode="json"), audience_user_id=audience_user_id,
            created_at=now,
        )
    )
    return seq


def read_events(
    conn: Connection,
    p: Principal,
    project_id: UUID,
    after: str | None,
    limit: int,
    now: datetime,
    retention: timedelta,
) -> tuple[list[EventEnvelope], str | None, str | None]:
    """Return (events, next_cursor, head_cursor). Caller has checked membership.
    Private-audience events are only returned to their audience user."""
    ev = t.events.c
    oldest = conn.execute(
        sa.select(sa.func.min(ev.project_seq)).where(ev.project_id == project_id)
    ).scalar()
    head = conn.execute(
        sa.select(t.projects.c.event_seq).where(t.projects.c.id == project_id)
    ).scalar() or 0
    if oldest is None:  # everything pruned (or nothing emitted yet)
        oldest = head + 1
    after_seq = decode_cursor(after) if after else 0
    if after_seq > head:
        raise DomainError(ErrorCode.validation_error, "cursor is ahead of this project")
    if after and after_seq < oldest - 1:
        raise DomainError(ErrorCode.cursor_expired, "cursor older than retention; resync")
    rows = conn.execute(
        sa.select(t.events)
        .where(
            ev.project_id == project_id,
            ev.project_seq > after_seq,
            sa.or_(ev.audience_user_id.is_(None), ev.audience_user_id == p.user_id),
        )
        .order_by(ev.project_seq)
        .limit(limit)
    ).all()
    out = [
        EventEnvelope(
            event_id=r.event_id, cursor=encode_cursor(r.project_seq), project_id=r.project_id,
            entity_id=r.entity_id, entity_version=r.entity_version, type=r.type,
            timestamp=r.created_at, payload=r.payload,
        )
        for r in rows
    ]
    next_cursor = out[-1].cursor if out else (after or None)
    return out, next_cursor, (encode_cursor(head) if head else None)


def prune_events(conn: Connection, now: datetime, retention: timedelta) -> int:
    return conn.execute(sa.delete(t.events).where(t.events.c.created_at < now - retention)).rowcount


# ------------------------------------------------------------------------- outbox


def enqueue(conn: Connection, now: datetime, topic: str, subject_id: UUID,
            payload: dict | None = None, delay: timedelta = timedelta(0)) -> None:
    conn.execute(
        sa.insert(t.outbox).values(
            topic=topic, subject_id=subject_id, payload=payload or {}, available_at=now + delay,
            created_at=now,
        )
    )


def claim_outbox(conn: Connection, now: datetime, worker: str, topics: list[str],
                 limit: int = 10, lease: timedelta = timedelta(seconds=60)) -> list[sa.Row]:
    """Claim due items by CAS so two dispatchers never process the same item at once."""
    o = t.outbox.c
    candidates = conn.execute(
        sa.select(t.outbox)
        .where(o.done_at.is_(None), o.available_at <= now, o.topic.in_(topics),
               o.attempts < o.max_attempts,
               sa.or_(o.claimed_until.is_(None), o.claimed_until < now))
        .order_by(o.seq).limit(limit)
    ).all()
    claimed = []
    for c in candidates:
        n = conn.execute(
            sa.update(t.outbox)
            .where(o.seq == c.seq, o.done_at.is_(None),
                   sa.or_(o.claimed_until.is_(None), o.claimed_until < now))
            .values(claimed_by=worker, claimed_until=now + lease, attempts=o.attempts + 1)
        ).rowcount
        if n == 1:
            claimed.append(c)
    return claimed


def complete_outbox(conn: Connection, now: datetime, seq: int) -> None:
    conn.execute(sa.update(t.outbox).where(t.outbox.c.seq == seq).values(done_at=now))


def fail_outbox(conn: Connection, now: datetime, seq: int, error: str, attempts: int) -> None:
    """Bounded exponential backoff; after max_attempts it stays visible as failed."""
    backoff = timedelta(seconds=min(300, 2 ** attempts))
    conn.execute(
        sa.update(t.outbox).where(t.outbox.c.seq == seq)
        .values(claimed_by=None, claimed_until=None, available_at=now + backoff,
                last_error=error[:500])
    )


def failed_outbox(conn: Connection) -> list[sa.Row]:
    o = t.outbox.c
    return conn.execute(
        sa.select(t.outbox).where(o.done_at.is_(None), o.attempts >= o.max_attempts)
    ).all()


# -------------------------------------------------------------------- idempotency


def idempotent(
    conn: Connection,
    now: datetime,
    p: Principal,
    route: str,
    key: str | None,
    request: Any,
    fn: Callable[[], tuple[int, Any]],
) -> tuple[int, Any]:
    """Replay a stored response for a repeated key; reject a reused key with a
    different body. The record is written in the same transaction as the effect."""
    if not key:
        return fn()
    scope = f"{p.scope_key}:{route}"
    req_hash = canonical_hash(request)
    r = t.idempotency_records.c
    prior = conn.execute(
        sa.select(t.idempotency_records).where(r.scope == scope, r.key == key)
    ).first()
    if prior is not None:
        if prior.request_hash != req_hash:
            raise DomainError(ErrorCode.idempotency_conflict, "key reused with a different body")
        return prior.status_code, prior.response
    status, body = fn()
    conn.execute(
        sa.insert(t.idempotency_records).values(
            scope=scope, key=key, request_hash=req_hash, status_code=status,
            response=body, created_at=now,
        )
    )
    return status, body


# --------------------------------------------------------------------- operations


def operation(conn_factory, now_fn, key: str, kind: str, fn: Callable[[], dict]) -> dict:  # noqa: ANN001
    """Record an external side effect so a restart replays its result instead of
    repeating it. `conn_factory` opens a fresh transaction (the side effect itself runs
    outside any DB transaction)."""
    o = t.operations.c
    with conn_factory() as conn:
        row = conn.execute(sa.select(t.operations).where(o.key == key)).first()
        if row is not None and row.status == "succeeded":
            return row.result
        if row is None:
            conn.execute(sa.insert(t.operations).values(
                key=key, kind=kind, status="started", created_at=now_fn(), updated_at=now_fn()))
    result = fn()
    with conn_factory() as conn:
        conn.execute(sa.update(t.operations).where(o.key == key)
                     .values(status="succeeded", result=result, updated_at=now_fn()))
    return result
