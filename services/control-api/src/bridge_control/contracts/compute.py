"""Workers, jobs, leases and section 20: model registry, presence, offers, sharing
policy, delegation and resource reservations.

Key distinctions the UI and scheduler must preserve (section 20 acceptance gates):
- ResourceOffer = advisory telemetry estimate. Never a reservation.
- ResourceReservation = atomic server-side hold. The only thing that means "capacity is ours".
- JobAttempt/Delegation in `running` = actual execution.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from .common import (
    ActorRef,
    Contract,
    FencingToken,
    GitSha,
    Id,
    ImageDigest,
    Money,
    ResourceVector,
    Revision,
    Sha256,
    ShortText,
    Slug,
    Timestamp,
    Visibility,
)

# --------------------------------------------------------------------------- devices


class PowerState(Contract):
    on_ac: bool
    battery_percent: int | None = Field(None, ge=0, le=100)


class Worker(Contract):
    """An enrolled device. Device identity is not agent identity."""

    id: Id
    workspace_id: Id
    owner_user_id: Id
    display_name: ShortText
    os: Literal["linux", "windows", "macos"]
    arch: Literal["x86_64", "arm64"]
    isolation: Literal["linux_vm", "hardened_container", "none"] = Field(
        description="'none' workers are never eligible for untrusted code jobs."
    )
    runtime_images: list[ImageDigest] = Field(default_factory=list)
    gpu_model: str | None = Field(None, max_length=120)
    device_limits: ResourceVector = Field(
        description="Owner-configured global ceiling across ALL projects, jobs and inference."
    )
    timezone: str = Field(
        description="IANA zone (e.g. 'Europe/Berlin'). SharingPolicy.schedule is evaluated "
        "in this zone's wall-clock time, never silently as UTC."
    )
    enrolled_at: Timestamp
    last_heartbeat_at: Timestamp | None = Field(
        None, description="SERVER receipt time of the last heartbeat; never worker-reported."
    )
    revoked_at: Timestamp | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, v: str) -> str:
        return validate_iana_timezone(v)


def validate_iana_timezone(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown IANA timezone: {v!r}") from e
    return v


class DeviceAvailability(StrEnum):
    """Derived for UI from heartbeat freshness + policy + reservations."""

    available = "available"
    busy = "busy"
    paused = "paused"
    offline = "offline"


class TimeWindow(Contract):
    """Half-open [start_minute, end_minute) of local wall-clock time on `weekday`
    (0 = Monday) in Worker.timezone.

    Rules: end must be after start (no reversed or zero-length windows). Overnight
    availability is two windows (e.g. Mon 22:00-24:00 and Tue 00:00-06:00).
    DST: the UTC instant is converted to local wall-clock time; during a fall-back
    repeated hour both occurrences match a window covering it, and minutes skipped by
    spring-forward simply never occur."""

    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end_minute <= self.start_minute:
            raise ValueError("end_minute must be after start_minute; split overnight windows")
        return self


class RevocationMode(StrEnum):
    finish_current = "finish_current"  # no new assignments; running work completes
    stop_now = "stop_now"  # no new assignments; running work is cancelled and fenced


class SharingPolicySettings(Contract):
    """Owner-settable sharing fields (the PUT body). All three device opt-ins default
    OFF and are independent. The fourth opt-in (spending API credits) is a PayerGrant."""

    worker_id: Id
    owner_user_id: Id
    share_compute: bool = False
    accept_delegated_tasks: bool = False
    share_local_inference: bool = False
    paused: bool = False
    allowed_project_ids: list[Id] = Field(default_factory=list, description="Empty = none.")
    allowed_job_types: list[Slug] = Field(default_factory=list, description="Empty = none.")
    shared_limits: ResourceVector = Field(
        default_factory=ResourceVector, description="Must fit within Worker.device_limits."
    )
    max_concurrent_jobs: int = Field(1, ge=0, le=8)
    network_egress: Literal["none", "allowlist"] = "none"
    egress_allowlist: list[str] = Field(default_factory=list)
    run_on_battery: bool = False
    pause_during_interactive_use: bool = True
    schedule: list[TimeWindow] = Field(default_factory=list, description="Empty = any time.")

    def any_sharing_enabled(self) -> bool:
        return self.share_compute or self.accept_delegated_tasks or self.share_local_inference


class SharingPolicy(SharingPolicySettings):
    """Stored policy (server adds revision and update time)."""

    revision: Revision = 0
    updated_at: Timestamp


class SharingPolicyUpdate(Contract):
    """PUT /v1/workers/{id}/sharing-policy. Turning a flag off (or pausing) is a
    revocation and must state what happens to running work."""

    expected_revision: Revision
    policy: SharingPolicySettings
    on_revocation: RevocationMode | None = Field(
        None, description="Required when this update removes eligibility of running work."
    )


# ----------------------------------------------------------------------- model registry


class RuntimeKind(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    google = "google"
    ollama = "ollama"
    lmstudio = "lmstudio"
    vllm = "vllm"
    hosted_open_model = "hosted_open_model"
    custom_endpoint = "custom_endpoint"


class ExecutionLocation(StrEnum):
    cloud = "cloud"
    device = "device"  # proxied through the enrolled worker's outbound connection


class LicenseInfo(Contract):
    """Open weights and open-source licensing are distinct."""

    spdx_id: str | None = Field(None, max_length=80)
    name: str | None = Field(None, max_length=200)
    open_weights: bool = False
    osi_open_source: bool = False
    permitted_uses_note: str | None = Field(None, max_length=1000)


class Capability(StrEnum):
    tool_calling = "tool_calling"
    json_schema_output = "json_schema_output"
    vision = "vision"
    streaming = "streaming"
    cancellation = "cancellation"


class ProbeResult(Contract):
    capability: Capability
    passed: bool
    probed_at: Timestamp
    detail: ShortText | None = None


class EndpointHealth(StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"


class ModelEndpoint(Contract):
    id: Id
    workspace_id: Id
    owner_user_id: Id
    runtime: RuntimeKind
    model_id: str = Field(min_length=1, max_length=200)
    model_revision: str | None = Field(None, max_length=200)
    quantization: str | None = Field(None, max_length=40)
    context_limit_tokens: int | None = Field(None, ge=1)
    declared_capabilities: list[Capability] = Field(default_factory=list)
    probes: list[ProbeResult] = Field(default_factory=list)
    license: LicenseInfo = Field(default_factory=LicenseInfo)
    execution_location: ExecutionLocation
    worker_id: Id | None = Field(None, description="Required for device endpoints.")
    connection_id: Id | None = Field(None, description="Required for cloud endpoints.")
    endpoint_ref: str = Field(
        max_length=200,
        description="Opaque server-side reference. Never a public URL for device runtimes.",
    )
    visibility: Visibility = Visibility.private
    shared_project_ids: list[Id] = Field(default_factory=list)
    health: EndpointHealth = EndpointHealth.unknown
    memory_needs: ResourceVector = Field(default_factory=ResourceVector)
    revision: Revision = 0
    created_at: Timestamp

    @model_validator(mode="after")
    def _location_refs(self) -> ModelEndpoint:
        if self.execution_location is ExecutionLocation.device and self.worker_id is None:
            raise ValueError("device endpoints require worker_id")
        if self.execution_location is ExecutionLocation.cloud and self.connection_id is None:
            raise ValueError("cloud endpoints require connection_id")
        return self

    def probed(self, cap: Capability) -> bool:
        """Latest probe for `cap` passed. Declared capability alone is insufficient."""
        latest = [p for p in self.probes if p.capability is cap]
        return bool(latest) and max(latest, key=lambda p: p.probed_at).passed

    @property
    def orchestrator_qualified(self) -> bool:
        return self.probed(Capability.tool_calling) and self.probed(Capability.json_schema_output)


# ------------------------------------------------------------------ discovery records


class AgentPresence(Contract):
    agent_id: Id
    project_id: Id
    owner_user_id: Id
    worker_ids: list[Id] = Field(default_factory=list)
    skills: list[Slug] = Field(default_factory=list)
    accepting_delegations: bool = False
    last_seen_at: Timestamp
    stale_after: Timestamp


class ResourceOffer(Contract):
    """ADVISORY. Project-relevant capacity only; never the owner's private apps or files."""

    worker_id: Id
    project_id: Id
    estimated_available: ResourceVector
    model_endpoint_ids: list[Id] = Field(default_factory=list)
    queue_length: int = Field(0, ge=0)
    heartbeat_seq: int = Field(ge=0)
    reported_at: Timestamp
    stale_after: Timestamp = Field(description="Suggested reported_at + 30s; tune in pilot.")


class CapabilitySummary(Contract):
    """GET /v1/projects/{id}/capabilities — what *this caller* is permitted to see."""

    project_id: Id
    generated_at: Timestamp
    agents: list[AgentPresence]
    offers: list[ResourceOffer]
    model_endpoints: list[ModelEndpoint]


# ----------------------------------------------------------------- leases & reservations


class LeaseSubject(StrEnum):
    task = "task"
    job = "job"
    delegation = "delegation"


class Lease(Contract):
    id: Id
    subject: LeaseSubject
    subject_id: Id
    holder: ActorRef
    attempt_id: Id
    fencing_token: FencingToken
    acquired_at: Timestamp
    expires_at: Timestamp
    released_at: Timestamp | None = None


class ReservationKind(StrEnum):
    job = "job"
    inference = "inference"
    delegation = "delegation"


class ResourceReservationState(StrEnum):
    held = "held"
    active = "active"
    released = "released"
    expired = "expired"


class ResourceReservation(Contract):
    """Atomic hold against Worker.device_limits AND SharingPolicy.shared_limits. Inference
    and execution draw from the same pool."""

    id: Id
    worker_id: Id
    project_id: Id
    kind: ReservationKind
    subject_id: Id
    requester_user_id: Id
    resources: ResourceVector
    state: ResourceReservationState = ResourceReservationState.held
    fencing_token: FencingToken
    created_at: Timestamp
    expires_at: Timestamp


# -------------------------------------------------------------------------------- jobs


class JobState(StrEnum):
    queued = "queued"
    reserved = "reserved"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class Job(Contract):
    """Authoritative job schema (section 7) — the deterministic remote-job mode."""

    id: Id
    project_id: Id
    task_id: Id
    job_type: Slug
    input_hashes: list[Sha256]
    repo_commit: GitSha | None = None
    image_digest: ImageDigest
    resource_request: ResourceVector
    timeout_seconds: int = Field(ge=1, le=86_400)
    allowed_network_policy: Literal["none", "allowlist"] = "none"
    egress_allowlist: list[str] = Field(default_factory=list)
    idempotent: bool = Field(description="Non-idempotent jobs are never auto-retried elsewhere.")
    supports_checkpoint: bool = False
    state: JobState = JobState.queued
    attempt_id: Id | None = None
    max_attempts: int = Field(3, ge=1, le=10)
    revision: Revision = 0
    created_at: Timestamp


class JobAttemptState(StrEnum):
    assigned = "assigned"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    fenced = "fenced"


class JobAttempt(Contract):
    id: Id
    job_id: Id
    worker_id: Id
    lease_id: Id
    fencing_token: FencingToken
    reservation_id: Id
    state: JobAttemptState = JobAttemptState.assigned
    started_at: Timestamp
    last_heartbeat_at: Timestamp | None = None
    ended_at: Timestamp | None = None


# -------------------------------------------------------------------------- delegation


class AgentDelegationConsent(Contract):
    """Authoritative per-agent, per-project consent to accept delegated tasks.
    AgentPresence.accepting_delegations is discovery state only. Device execution also
    needs SharingPolicy.accept_delegated_tasks on the device that runs it."""

    agent_id: Id
    project_id: Id
    accept_delegations: bool = False
    allowed_skills: list[Slug] = Field(default_factory=list, description="Empty = any.")
    revision: Revision = 0
    updated_at: Timestamp


class AgentDelegationConsentUpdate(Contract):
    expected_revision: Revision
    accept_delegations: bool
    allowed_skills: list[Slug] = Field(default_factory=list)


class DelegationMode(StrEnum):
    deterministic_job = "deterministic_job"
    agent_task = "agent_task"


class DelegationState(StrEnum):
    requested = "requested"
    assigned = "assigned"  # device reserved, recipient-bound assignment token issued
    accepted = "accepted"
    running = "running"
    completed = "completed"  # result returned; not yet verified by requester
    verified = "verified"
    rejected_result = "rejected_result"
    declined = "declined"
    expired = "expired"
    cancelled = "cancelled"
    failed = "failed"


class PayerRef(Contract):
    kind: Literal["requester", "workspace", "grant"]
    user_id: Id | None = None
    payer_grant_id: Id | None = Field(None, description="Required when kind='grant'.")

    @model_validator(mode="after")
    def _grant_needs_id(self) -> PayerRef:
        if self.kind == "grant" and self.payer_grant_id is None:
            raise ValueError("grant payer requires payer_grant_id")
        return self


class DelegationBounds(Contract):
    """Child delegations may only narrow these; never widen (section 20)."""

    budget: Money
    deadline: Timestamp
    remaining_depth: int = Field(ge=0, le=5)
    allowed_tools: list[Slug] = Field(default_factory=list)
    allowed_model_endpoint_ids: list[Id] = Field(default_factory=list)
    data_visibility: Visibility = Visibility.task


class DelegationRequest(Contract):
    id: Id
    project_id: Id
    task_id: Id
    parent_delegation_id: Id | None = None
    mode: DelegationMode
    requester_agent_id: Id
    requester_user_id: Id
    recipient_agent_id: Id | None = Field(None, description="Null = coordinator selects.")
    input_artifact_ids: list[Id] = Field(default_factory=list)
    acceptance_criteria: list[ShortText] = Field(min_length=1)
    resource_requirements: ResourceVector
    payer: PayerRef
    bounds: DelegationBounds
    state: DelegationState = DelegationState.requested
    reservation_id: Id | None = None
    worker_id: Id | None = None
    attempt_id: Id | None = Field(None, description="Current attempt; changes on reassignment.")
    fencing_token: FencingToken | None = None
    lease_expires_at: Timestamp | None = None
    blocked_reason: ShortText | None = Field(None, description="Why it is queued, if it is.")
    declined_agent_ids: list[Id] = Field(default_factory=list)
    decline_count: int = Field(0, ge=0)
    max_alternates: int = Field(2, ge=0, le=5)
    idempotency_key: str = Field(min_length=8, max_length=128)
    revision: Revision = 0
    created_at: Timestamp


class AssignmentOperation(StrEnum):
    accept = "accept"
    decline = "decline"
    heartbeat = "heartbeat"
    complete = "complete"


class DelegationAssignment(Contract):
    """Recipient-only delivery: POST /v1/delegations/{id}/assignment, authenticated as the
    recipient agent. Never appears in discovery, lists or events.

    Flow: requester creates -> coordinator reserves + selects recipient (state=assigned,
    new attempt_id + fence) -> recipient fetches this (token minted; any previous token for
    the delegation is revoked) -> accept/decline -> heartbeat to renew lease -> complete.
    On lease expiry or decline the server reassigns with a NEW attempt_id and a higher
    fence; old tokens stop working. Token possession never overrides membership/policy."""

    delegation: DelegationRequest
    attempt_id: Id
    fencing_token: FencingToken
    assignment_token: str = Field(description="High-entropy, shown once, hashed at rest.")
    token_expires_at: Timestamp
    lease_expires_at: Timestamp
    reservation: ResourceReservation | None = None
    input_grants: dict[str, str] = Field(
        default_factory=dict, description="artifact_id -> short-lived download path; bounded."
    )
    allowed_operations: list[AssignmentOperation]


class AssignmentProof(Contract):
    assignment_token: str = Field(min_length=32, max_length=256)
    attempt_id: Id
    fencing_token: FencingToken


class DelegationAccept(AssignmentProof):
    expected_revision: Revision


class DelegationDecline(AssignmentProof):
    expected_revision: Revision
    reason: Literal["policy", "capacity", "unavailable", "out_of_scope", "other"]


class DelegationHeartbeat(AssignmentProof):
    progress_summary: ShortText | None = None


class DelegationHeartbeatResponse(Contract):
    lease_expires_at: Timestamp
    cancel_requested: bool = False


class DelegationVerification(Contract):
    """Requester-side verdict on a completed (untrusted) result."""

    expected_revision: Revision
    accepted: bool
    notes: ShortText | None = None


class CheckEvidence(Contract):
    name: ShortText
    passed: bool
    log_artifact_id: Id | None = None


class EnvironmentDetails(Contract):
    worker_id: Id | None = None
    image_digest: ImageDigest | None = None
    model_endpoint_id: Id | None = None
    provider_model_id: str | None = Field(None, max_length=200)
    model_revision: str | None = Field(None, max_length=200)


class ArtifactRef(Contract):
    artifact_id: Id
    sha256: Sha256


class DelegationResult(AssignmentProof):
    """POST /v1/delegations/{id}/complete body. Untrusted until the requester verifies."""

    outcome: Literal["succeeded", "failed"]
    summary: str = Field(max_length=4000)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    source_commit: GitSha | None = None
    environment: EnvironmentDetails
    tests: list[CheckEvidence] = Field(default_factory=list)
    actual_usage: Money


class AttemptFence(Contract):
    attempt_id: Id
    fencing_token: FencingToken


class OfferReport(Contract):
    """Worker-reported advisory capacity; the server stamps times and freshness."""

    project_id: Id
    estimated_available: ResourceVector
    model_endpoint_ids: list[Id] = Field(default_factory=list)
    queue_length: int = Field(0, ge=0)


class WorkerHeartbeat(Contract):
    """No client timestamps: freshness uses server receipt time only."""

    seq: int = Field(ge=0)
    power: PowerState
    interactive_use: bool
    running_attempts: list[AttemptFence] = Field(default_factory=list)
    offers: list[OfferReport] = Field(default_factory=list)


class HeartbeatResponse(Contract):
    """Server tells a reconnecting worker which attempts are stale and must stop."""

    server_received_at: Timestamp
    stop_attempt_ids: list[Id] = Field(default_factory=list)
    renewed_until: Timestamp
    next_heartbeat_seconds: int = 10


# ------------------------------------------------------------- inference bridge


class InferenceState(StrEnum):
    queued = "queued"
    claimed = "claimed"  # a worker holds the fenced attempt and is calling its local runtime
    succeeded = "succeeded"
    failed = "failed"
    expired = "expired"
    cancelled = "cancelled"


class InferenceRequestView(Contract):
    """A bounded TEXT task for a device-local model, proxied through the device's
    enrolled worker over its outbound connection. No public model port exists. The
    bridge is text-only: tool calling over device models is not offered."""

    id: Id
    project_id: Id
    model_endpoint_id: Id
    worker_id: Id
    requester_user_id: Id
    state: InferenceState
    max_tokens: int
    deadline: Timestamp
    attempt_id: Id | None = None
    fencing_token: FencingToken | None = None
    text: str | None = Field(None, description="Result; visible to the requester only.")
    provider_model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    runtime_ms: int | None = None
    error: ShortText | None = None
    executed_on: Literal["device"] = "device"
    created_at: Timestamp


class InferenceAssignment(Contract):
    """Delivered only to the worker that hosts the endpoint."""

    request_id: Id
    attempt_id: Id
    fencing_token: FencingToken
    runtime: RuntimeKind
    model_id: str
    prompt: str = Field(max_length=16_000)
    max_tokens: int = Field(ge=1, le=2048)
    lease_expires_at: Timestamp
    reservation_id: Id


class InferenceCompletion(Contract):
    attempt_id: Id
    fencing_token: FencingToken
    outcome: Literal["succeeded", "failed"]
    text: str | None = Field(None, max_length=64_000)
    provider_model_id: str | None = Field(None, max_length=200)
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    runtime_ms: int = Field(ge=0)
    error: ShortText | None = None
