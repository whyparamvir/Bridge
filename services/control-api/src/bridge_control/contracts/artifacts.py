"""Artifacts, repository bindings, budgets, usage and audit (sections 7, 9, 11)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import (
    ActorRef,
    Contract,
    GitSha,
    Id,
    ImageDigest,
    Micros,
    Money,
    Revision,
    Sha256,
    ShortText,
    Timestamp,
    Visibility,
)


class ArtifactStatus(StrEnum):
    pending_upload = "pending_upload"
    ready = "ready"  # metadata + hash verified on finalize
    rejected = "rejected"  # hash/size mismatch on finalize


class Provenance(Contract):
    """Where an artifact came from. Remote results are untrusted evidence until verified."""

    produced_by: ActorRef
    task_id: Id | None = None
    job_attempt_id: Id | None = None
    source_commit: GitSha | None = None
    image_digest: ImageDigest | None = None
    model_endpoint_id: Id | None = None
    model_revision: str | None = Field(None, max_length=200)
    input_hashes: list[Sha256] = Field(default_factory=list)
    trusted_runner: bool = Field(False, description="True only for project-trusted runners.")


class Artifact(Contract):
    id: Id
    project_id: Id
    name: ShortText
    media_type: str = Field(max_length=120)
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    status: ArtifactStatus = ArtifactStatus.pending_upload
    visibility: Visibility = Visibility.project
    version: int = Field(1, ge=1)
    supersedes_id: Id | None = None
    provenance: Provenance
    created_at: Timestamp


class RepositoryBinding(Contract):
    """A narrowly scoped GitHub App installation reference. The server stores IDs only:
    the App private key and installation tokens live in Codex's repository adapter."""

    id: Id
    project_id: Id
    provider: str = Field("github", pattern=r"^github$")
    owner: ShortText
    repo: ShortText
    repository_id: int | None = Field(None, ge=1, description="GitHub numeric repository id.")
    installation_id: int = Field(ge=1, description="GitHub App installation, not a user token.")
    default_branch: ShortText = "main"
    created_by_user_id: Id | None = None
    revoked_at: Timestamp | None = None
    revision: Revision = 0
    created_at: Timestamp


class ReservationState(StrEnum):
    held = "held"
    reconciled = "reconciled"
    released = "released"


class BudgetReservation(Contract):
    """Reserved estimate before concurrent work; reconciled with actual usage after.
    Limits carry a safety margin and are not an exact billing guarantee."""

    id: Id
    project_id: Id
    payer_user_id: Id | None = Field(None, description="Null = workspace-funded.")
    payer_grant_id: Id | None = Field(None, description="Required when payer != requester.")
    task_id: Id | None = None
    run_id: Id | None = None
    estimate: Money
    actual: Money | None = None
    state: ReservationState = ReservationState.held
    revision: Revision = 0
    created_at: Timestamp


class UsageKind(StrEnum):
    model_tokens = "model_tokens"
    tool_charge = "tool_charge"
    worker_runtime = "worker_runtime"
    storage = "storage"


class UsageEntry(Contract):
    id: Id
    project_id: Id
    reservation_id: Id | None = None
    kind: UsageKind
    model_endpoint_id: Id | None = None
    provider_model_id: str | None = Field(None, max_length=200, description="Exact, as reported.")
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    runtime_ms: int | None = Field(None, ge=0)
    cost_micros: Micros
    currency: str = "USD"
    recorded_at: Timestamp
    provider_reported: bool = Field(description="False when estimated pending provider data.")


class AuditEvent(Contract):
    id: Id
    workspace_id: Id
    project_id: Id | None = None
    actor: ActorRef
    action: ShortText = Field(description="e.g. 'membership.revoke', 'sharing_policy.update'")
    target_id: Id | None = None
    detail: dict[str, str] = Field(default_factory=dict)
    created_at: Timestamp


__all__ = [
    "Artifact",
    "ArtifactStatus",
    "AuditEvent",
    "BudgetReservation",
    "Money",
    "Provenance",
    "RepositoryBinding",
    "ReservationState",
    "UsageEntry",
    "UsageKind",
]
