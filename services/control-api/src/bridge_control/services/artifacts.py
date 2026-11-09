"""Artifact metadata, upload intents, hash-verified finalize and authorized download.

Storage is behind `BlobStore`. Only `LocalBlobStore` (a directory, development use) is
implemented; S3-compatible storage and pre-signed URLs are Codex's packages/artifacts
integration and are NOT implemented here. Uploads go through the API
(PUT /v1/artifacts/{id}/content) so membership is checked on every byte path.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from ..contracts import api as A
from ..contracts.artifacts import Artifact, ArtifactStatus, Provenance
from ..contracts.common import Visibility
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.events import ArtifactReady
from ..core import Principal, new_id, not_found, require_role
from ..db import tables as t
from ..reliability import emit

MAX_BYTES = 256 * 1024 * 1024
UPLOAD_TTL = timedelta(minutes=30)


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...


class LocalBlobStore:
    """Development-only content store. Keys are server-generated (no user paths)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        assert all(c.isalnum() or c in "-/" for c in key), "server-generated keys only"
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None


class MemoryBlobStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.data[key] = data

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)


def contract(r) -> Artifact:  # noqa: ANN001
    return Artifact(id=r.id, project_id=r.project_id, name=r.name, media_type=r.media_type,
                    size_bytes=r.size_bytes, sha256=r.sha256, status=ArtifactStatus(r.status),
                    visibility=Visibility(r.visibility), version=r.version, supersedes_id=r.supersedes_id,
                    provenance=Provenance.model_validate(r.provenance), created_at=r.created_at)


def readable(conn: Connection, p: Principal, artifact_id: UUID):  # noqa: ANN201
    """Membership (and thus revocation) is checked on every lookup and download."""
    row = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == artifact_id)).first()
    if row is None:
        raise not_found("artifact")
    require_role(conn, p, row.project_id, "viewer")
    if row.visibility == "private" and row.created_by_user_id != p.user_id:
        raise not_found("artifact")
    return row


def _worker_output_job(conn: Connection, now: datetime, p: Principal, provenance: dict):  # noqa: ANN202
    """A worker may create/upload/finalize ONLY outputs of its current job attempt, with
    provenance naming itself, that attempt, and the job's task, image and inputs."""
    from .scheduling import attempt_job

    attempt = provenance.get("job_attempt_id")
    job = attempt_job(conn, now, p, UUID(str(attempt)) if attempt else None)
    produced = provenance.get("produced_by") or {}
    b = job.body
    if (produced.get("kind") != "worker" or str(produced.get("id")) != str(p.worker_id)
            or str(provenance.get("task_id")) != str(job.task_id)
            or provenance.get("image_digest") != b["image_digest"]
            or sorted(provenance.get("input_hashes") or []) != sorted(b["input_hashes"])
            or provenance.get("trusted_runner")):
        raise DomainError(ErrorCode.validation_error, "output provenance must match the assigned attempt")
    return job


def _writable(conn: Connection, now: datetime, p: Principal, artifact_id: UUID):  # noqa: ANN202
    row = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == artifact_id)).first()
    if row is None:
        raise not_found("artifact")
    if p.kind == "worker":
        _worker_output_job(conn, now, p, row.provenance or {})
        return row
    readable(conn, p, artifact_id)
    require_role(conn, p, row.project_id, "editor")
    return row


def create_upload_intent(conn: Connection, now: datetime, p: Principal, body: A.UploadIntent) -> A.UploadIntentResponse:
    if p.kind == "worker":
        job = _worker_output_job(conn, now, p, body.provenance.model_dump(mode="json"))
        if body.project_id != job.project_id or body.visibility != "project" or body.supersedes_id is not None:
            raise DomainError(ErrorCode.validation_error, "worker outputs belong to the job's project")
    else:
        require_role(conn, p, body.project_id, "editor")
    if body.size_bytes > MAX_BYTES:
        raise DomainError(ErrorCode.validation_error, "artifact too large for this deployment")
    if body.supersedes_id is not None:
        prev = readable(conn, p, body.supersedes_id)
        if prev.project_id != body.project_id:
            raise not_found("artifact")
        version = prev.version + 1
    else:
        version = 1
    aid = new_id()
    key = f"{body.project_id}/{aid}"
    conn.execute(sa.insert(t.artifacts).values(
        id=aid, project_id=body.project_id, name=body.name, media_type=body.media_type, size_bytes=body.size_bytes,
        sha256=body.sha256, status="pending_upload", visibility=body.visibility, version=version,
        supersedes_id=body.supersedes_id, provenance=body.provenance.model_dump(mode="json"), storage_key=key,
        created_by_user_id=p.user_id, created_at=now))
    return A.UploadIntentResponse(artifact_id=aid, upload_url=f"/v1/artifacts/{aid}/content",
                                  expires_at=now + UPLOAD_TTL)


def upload_content(conn: Connection, now: datetime, p: Principal, store: BlobStore, artifact_id: UUID,
                   data: bytes) -> None:
    row = _writable(conn, now, p, artifact_id)
    if row.status != "pending_upload" or row.created_by_user_id != p.user_id:
        raise DomainError(ErrorCode.invalid_transition, "artifact is not awaiting your upload")
    if row.created_at + UPLOAD_TTL < now:
        raise DomainError(ErrorCode.invalid_transition, "upload intent expired")
    if len(data) > row.size_bytes or len(data) > MAX_BYTES:
        raise DomainError(ErrorCode.validation_error, "content larger than declared")
    store.put(row.storage_key, data)


def finalize(conn: Connection, now: datetime, p: Principal, store: BlobStore, artifact_id: UUID,
             body: A.FinalizeArtifact) -> Artifact:
    """Verifies the stored bytes against the declared hash and size. A mismatch marks the
    artifact rejected (visible), never ready."""
    row = _writable(conn, now, p, artifact_id)
    if row.status != "pending_upload":
        raise DomainError(ErrorCode.invalid_transition, f"artifact is {row.status}")
    data = store.get(row.storage_key)
    ok = (data is not None and body.sha256 == row.sha256 and body.size_bytes == row.size_bytes
          and len(data) == row.size_bytes and hashlib.sha256(data).hexdigest() == row.sha256)
    a = t.artifacts.c
    conn.execute(sa.update(t.artifacts).where(a.id == artifact_id, a.status == "pending_upload")
                 .values(status="ready" if ok else "rejected"))
    row = conn.execute(sa.select(t.artifacts).where(a.id == artifact_id)).one()
    if ok:
        emit(conn, now, row.project_id, row.id, row.version, ArtifactReady(
            artifact_id=row.id, name=row.name, media_type=row.media_type, sha256=row.sha256),
            audience_user_id=row.created_by_user_id if row.visibility == "private" else None)
        return contract(row)
    raise DomainError(ErrorCode.validation_error, "content hash or size mismatch; artifact rejected")


def download(conn: Connection, p: Principal, store: BlobStore, artifact_id: UUID) -> tuple[Artifact, bytes]:
    row = readable(conn, p, artifact_id)
    if row.status != "ready":
        raise DomainError(ErrorCode.invalid_transition, f"artifact is {row.status}")
    data = store.get(row.storage_key)
    if data is None or hashlib.sha256(data).hexdigest() != row.sha256:
        raise DomainError(ErrorCode.provider_unavailable, "stored content missing or corrupted")
    return contract(row), data


def list_for_project(conn: Connection, p: Principal, project_id: UUID, limit: int) -> list[Artifact]:
    require_role(conn, p, project_id, "viewer")
    a = t.artifacts.c
    rows = conn.execute(sa.select(t.artifacts).where(
        a.project_id == project_id, a.status == "ready",
        sa.or_(a.visibility != "private", a.created_by_user_id == p.user_id)
    ).order_by(a.created_at).limit(limit)).all()
    return [contract(r) for r in rows]


def store_internal(conn: Connection, now: datetime, store: BlobStore, *, project_id: UUID, user_id: UUID,
                   name: str, media_type: str, data: bytes, provenance: Provenance, visibility: Visibility,
                   artifact_id: UUID | None = None) -> Artifact:
    """Server-side artifact creation (agent runtime outputs). Idempotent on artifact_id."""
    aid = artifact_id or new_id()
    existing = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == aid)).first()
    if existing is not None:
        return contract(existing)
    key = f"{project_id}/{aid}"
    store.put(key, data)
    digest = hashlib.sha256(data).hexdigest()
    conn.execute(sa.insert(t.artifacts).values(
        id=aid, project_id=project_id, name=name, media_type=media_type, size_bytes=len(data), sha256=digest,
        status="ready", visibility=visibility.value, version=1, provenance=provenance.model_dump(mode="json"),
        storage_key=key, created_by_user_id=user_id, created_at=now))
    row = conn.execute(sa.select(t.artifacts).where(t.artifacts.c.id == aid)).one()
    emit(conn, now, project_id, aid, 1, ArtifactReady(artifact_id=aid, name=name, media_type=media_type, sha256=digest),
         audience_user_id=user_id if visibility is Visibility.private else None)
    return contract(row)
