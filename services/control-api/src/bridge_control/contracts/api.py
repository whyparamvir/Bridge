"""HTTP request/response bodies. Routes live in bridge_control.api; OpenAPI is generated
from those routes plus these models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator

from . import artifacts, compute, identity, messaging, work
from .common import (
    Contract, FencingToken, GitSha, Id, IdempotencyKey, ImageDigest, LongText, Money, Page,
    ResourceVector, Revision, Sha256, ShortText, Slug, Timestamp,
)
from .compute import validate_iana_timezone
from .events import EventEnvelope

Email = identity.Email

# ------------------------------------------------------------------- accounts


class RegisterAccount(Contract):
    email: Email = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: ShortText
    password: SecretStr = Field(min_length=12, max_length=256)


class Login(Contract):
    email: Email = Field(min_length=3, max_length=320)
    password: SecretStr = Field(max_length=256)


class SessionToken(Contract):
    token: str = Field(description="Bearer token. Shown once; only its hash is stored.")
    user: identity.User
    expires_at: Timestamp


class Me(Contract):
    user: identity.User
    principal_kind: Literal["user", "agent", "worker"]
    agent_id: Id | None = None
    worker_id: Id | None = None
    dev_auth: bool = False
    install_owner: bool = Field(False, description="The person who runs this self-run team server.")


# --------------------------------------------------------- workspaces/projects


class CreateWorkspace(Contract):
    name: ShortText


class AddWorkspaceMember(Contract):
    email: Email = Field(min_length=3, max_length=320)
    role: Literal["owner", "member"] = "member"


class CreateProject(Contract):
    workspace_id: Id
    slug: Slug
    name: ShortText
    objective: str = Field("", max_length=4000)
    budget_limit: Money = Field(default_factory=lambda: Money(amount_micros=0))


class BudgetStatus(Contract):
    project_id: Id
    limit: Money
    reserved: Money
    spent: Money
    revision: Revision


class SetBudget(Contract):
    expected_revision: Revision
    limit: Money


class AddMember(Contract):
    email: Email = Field(min_length=3, max_length=320)
    role: identity.Role


class RevokeMember(Contract):
    expected_revision: Revision


class CreateAgent(Contract):
    workspace_id: Id
    kind: identity.AgentKind = identity.AgentKind.personal_orchestrator
    name: ShortText
    max_delegation_depth: int = Field(2, ge=0, le=5)
    skills: list[Slug] = Field(default_factory=list)


class AgentCredential(Contract):
    agent_id: Id
    token: str = Field(description="Agent bearer token, shown once.")
    expires_at: Timestamp


class SetLeadModel(Contract):
    model_endpoint_id: Id | None


# ------------------------------------------------------------- conversations


class CreateConversation(Contract):
    scope: work.ConversationScope


class PostMessage(Contract):
    body: LongText
    artifact_refs: list[Id] = Field(default_factory=list)
    idempotency_key: IdempotencyKey | None = None


# ------------------------------------------------------------------------ runs


class CreateRun(Contract):
    conversation_id: Id
    goal: LongText
    budget_limit: Money
    idempotency_key: IdempotencyKey
    orchestrator_agent_id: Id | None = Field(None, description="Required for human callers.")
    model_endpoint_id: Id | None = Field(None, description="Lead model; default = agent's lead model.")
    publish_tasks: bool = Field(False, description="From a private chat, share planned tasks with the project.")
    max_tasks: int = Field(5, ge=1, le=12)


class CancelRun(Contract):
    reason: ShortText | None = None


# ----------------------------------------------------------------------- tasks


class CreateTask(Contract):
    title: ShortText
    description: LongText = ""
    acceptance_criteria: list[ShortText] = Field(min_length=1)
    dependencies: list[Id] = Field(default_factory=list)
    budget_limit: Money
    idempotency_key: IdempotencyKey | None = None


class ClaimTask(Contract):
    agent_id: Id | None = Field(None, description="Required for human callers.")
    lease_seconds: int = Field(120, ge=10, le=3600)


class ClaimResponse(Contract):
    task: work.Task
    lease: compute.Lease


class RenewLease(Contract):
    attempt_id: Id
    lease_token: FencingToken
    lease_seconds: int = Field(120, ge=10, le=3600)


class TransitionTask(Contract):
    to_state: work.TaskState
    expected_revision: Revision
    attempt_id: Id | None = Field(None, description="Required with lease_token.")
    lease_token: FencingToken | None = Field(None, description="Required for leased transitions.")
    reason: ShortText | None = None
    result_summary: str | None = Field(None, max_length=4000)
    artifact_refs: list[Id] = Field(default_factory=list)


# ------------------------------------------------------------------ decisions


class ProposeDecision(Contract):
    key: ShortText
    body: LongText
    source_refs: list[Id] = Field(default_factory=list)
    supersedes_id: Id | None = None


class AcceptDecision(Contract):
    expected_revision: Revision
    supersedes_expected_revision: Revision | None = None


# ------------------------------------------------------------- agent messages


class PostAgentMessage(Contract):
    recipient_agent_id: Id
    task_id: Id | None = None
    correlation_id: Id
    payload: messaging.AgentMessagePayload
    expires_at: Timestamp
    deduplication_key: IdempotencyKey


# ------------------------------------------------------------------ artifacts


class UploadIntent(Contract):
    project_id: Id
    name: ShortText
    media_type: str = Field(max_length=120)
    size_bytes: int = Field(ge=0, le=5 * 1024**3)
    sha256: Sha256
    visibility: Literal["project", "private", "task"] = "project"
    supersedes_id: Id | None = None
    provenance: artifacts.Provenance


class UploadIntentResponse(Contract):
    artifact_id: Id
    upload_url: str = Field(description="PUT raw bytes here (authenticated, same principal).")
    expires_at: Timestamp


class FinalizeArtifact(Contract):
    sha256: Sha256
    size_bytes: int = Field(ge=0)


# --------------------------------------------------------- workers and jobs


class CreateEnrollmentGrant(Contract):
    workspace_id: Id


class EnrollmentGrant(Contract):
    enrollment_grant: str = Field(description="Single use; expires in 15 minutes.")
    expires_at: Timestamp


class RegisterWorker(Contract):
    enrollment_grant: str = Field(min_length=32, max_length=256)
    display_name: ShortText
    os: Literal["linux", "windows", "macos"]
    arch: Literal["x86_64", "arm64"]
    isolation: Literal["linux_vm", "hardened_container", "none"]
    timezone: str = Field(description="IANA zone; schedules are evaluated in it.")
    runtime_images: list[ImageDigest] = Field(default_factory=list)
    gpu_model: str | None = Field(None, max_length=120)
    device_limits: ResourceVector

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return validate_iana_timezone(v)


class RegisterWorkerResponse(Contract):
    worker: compute.Worker
    worker_credential: str = Field(description="Shown once; device-scoped bearer token.")


class WorkerStatus(Contract):
    worker: compute.Worker
    availability: compute.DeviceAvailability
    policy: compute.SharingPolicy
    reserved: ResourceVector = Field(description="Sum of live reservations (actual holds).")
    reservations: list[compute.ResourceReservation]
    power: compute.PowerState


WorkerHeartbeat = compute.WorkerHeartbeat


class CreateJob(Contract):
    task_id: Id
    job_type: Slug
    input_hashes: list[Sha256] = Field(default_factory=list)
    repo_commit: GitSha | None = None
    image_digest: ImageDigest
    resource_request: ResourceVector
    timeout_seconds: int = Field(600, ge=1, le=86_400)
    allowed_network_policy: Literal["none", "allowlist"] = "none"
    egress_allowlist: list[str] = Field(default_factory=list)
    idempotent: bool = True
    supports_checkpoint: bool = False
    max_attempts: int = Field(3, ge=1, le=10)
    idempotency_key: IdempotencyKey | None = None


class ClaimJob(Contract):
    worker_id: Id
    job_types: list[Slug] = Field(min_length=1)


class JobAssignment(Contract):
    job: compute.Job
    attempt: compute.JobAttempt
    lease: compute.Lease
    input_grants: dict[str, str] = Field(description="sha256 -> authenticated download path")
    output_grant_expires_at: Timestamp


class ClaimJobResponse(Contract):
    assignment: JobAssignment | None = Field(description="Null when nothing eligible fits.")


class CompleteJob(Contract):
    attempt_id: Id
    fencing_token: FencingToken
    outcome: Literal["succeeded", "failed"]
    exit_code: int | None = None
    outputs: list[compute.ArtifactRef] = Field(default_factory=list)
    source_commit: GitSha | None = None
    runtime_ms: int = Field(ge=0)
    log_summary: str | None = Field(None, max_length=4000)


# --------------------------------------------------------------- delegation


class CreateDelegation(Contract):
    task_id: Id
    mode: compute.DelegationMode = compute.DelegationMode.agent_task
    requester_agent_id: Id | None = Field(None, description="Required for human callers.")
    parent_delegation_id: Id | None = None
    recipient_agent_id: Id | None = None
    input_artifact_ids: list[Id] = Field(default_factory=list, max_length=50)
    acceptance_criteria: list[ShortText] = Field(min_length=1)
    resource_requirements: ResourceVector = Field(default_factory=ResourceVector)
    payer: compute.PayerRef
    bounds: compute.DelegationBounds
    max_alternates: int = Field(2, ge=0, le=5)
    idempotency_key: IdempotencyKey


class DelegationView(Contract):
    delegation: compute.DelegationRequest
    result: dict[str, Any] | None = Field(None, description="Untrusted until state=verified.")


class ReportPresence(Contract):
    worker_ids: list[Id] = Field(default_factory=list, max_length=8)


# ------------------------------------------------------ providers and models


class CreateProviderConnection(Contract):
    workspace_id: Id
    provider: Slug
    billing_mode: identity.BillingMode
    display_name: ShortText
    secret_ref: str | None = Field(None, description="e.g. env:ANTHROPIC_API_KEY. Never the key.")
    base_url: str | None = Field(None, max_length=500, description="Local/custom runtimes only.")


class CreatePayerGrant(Contract):
    connection_id: Id
    grantee_user_id: Id
    cap: Money
    expires_at: Timestamp
    allowed_model_endpoint_ids: list[Id] = Field(default_factory=list)


class CreateModelEndpoint(Contract):
    runtime: compute.RuntimeKind
    model_id: str = Field(min_length=1, max_length=200)
    model_revision: str | None = None
    quantization: str | None = None
    context_limit_tokens: int | None = Field(None, ge=1)
    declared_capabilities: list[compute.Capability] = Field(default_factory=list)
    execution_location: compute.ExecutionLocation
    worker_id: Id | None = None
    connection_id: Id | None = None
    license: compute.LicenseInfo = Field(default_factory=compute.LicenseInfo)
    shared_project_ids: list[Id] = Field(default_factory=list)
    memory_needs: ResourceVector = Field(default_factory=ResourceVector)
    run_capability_probe: bool = Field(False, description="Queues a bounded probe call.")


class DeviceSummary(Contract):
    worker_id: Id
    owner_user_id: Id
    display_name: ShortText
    availability: compute.DeviceAvailability
    share_compute: bool
    accept_delegated_tasks: bool
    share_local_inference: bool
    offer_is_stale: bool


class CapabilityView(Contract):
    summary: compute.CapabilitySummary
    devices: list[DeviceSummary]


# ------------------------------------------------------------------- events


class EventPage(Contract):
    events: list[EventEnvelope]
    next_cursor: str | None = Field(description="Pass as ?after= to continue; null if none yet.")
    head_cursor: str | None = Field(description="Newest cursor (resync point).")


class Accepted(Contract):
    accepted: bool = True
    detail: str | None = None


# ------------------------------------------------------------------- pages


class ProjectPage(Page[identity.Project]): ...
class MembershipPage(Page[identity.Membership]): ...
class ConversationPage(Page[work.Conversation]): ...
class MessagePage(Page[work.Message]): ...
class TaskPage(Page[work.Task]): ...
class DecisionPage(Page[work.Decision]): ...
class RunPage(Page[work.Run]): ...
class ArtifactPage(Page[artifacts.Artifact]): ...
class AgentMessagePage(Page[messaging.AgentMessage]): ...
class DelegationPage(Page[compute.DelegationRequest]): ...
class ModelEndpointPage(Page[compute.ModelEndpoint]): ...
class ProviderConnectionPage(Page[identity.ProviderConnection]): ...
class PayerGrantPage(Page[identity.PayerGrant]): ...
class WorkerStatusPage(Page[WorkerStatus]): ...


__all__ = [n for n in dir() if not n.startswith("_")]
_unused = (LongText, Revision)


# ------------------------------------------------------------ inference bridge


class CreateInferenceRequest(Contract):
    model_endpoint_id: Id
    prompt: str = Field(min_length=1, max_length=16_000)
    max_tokens: int = Field(256, ge=1, le=2048)
    deadline_seconds: int = Field(300, ge=10, le=3600)


class ClaimInference(Contract):
    worker_id: Id


class ClaimInferenceResponse(Contract):
    assignment: compute.InferenceAssignment | None


class JoinWorkspace(Contract):
    token: str = Field(pattern=r"^hwi_[A-Za-z0-9_-]{43}$")


class WorkspacePage(Contract):
    items: list[identity.WorkspaceAccess]
    next_cursor: str | None = None


class WorkspaceMemberPage(Contract):
    items: list[identity.WorkspaceMemberView]
    next_cursor: str | None = None


class WorkspaceAgentPage(Page[identity.AgentProfile]): ...
