"""Event envelope and typed payloads (section 7).

Events are persisted before streaming. Clients reconnect with
GET /v1/projects/{id}/events?after={cursor} and deduplicate on event_id;
entity_version lets clients drop out-of-order stale updates.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import SCHEMA_VERSION, Contract, Id, Money, ResourceVector, ShortText, Timestamp
from .compute import DelegationState, DeviceAvailability, ResourceReservationState
from .work import RunStatus, TaskState


class TaskUpdated(Contract):
    type: Literal["task.updated"] = "task.updated"
    state: TaskState
    previous_state: TaskState | None = None
    owner_agent_id: Id | None = None
    blocked_reason: ShortText | None = None
    created_by_agent_id: Id | None = Field(
        None, description="On a task's creation event: the agent that created it, if an agent did.")


class RunProgress(Contract):
    type: Literal["run.progress"] = "run.progress"
    status: RunStatus
    summary: ShortText = Field(description="Execution summary, not hidden reasoning.")
    task_id: Id | None = None
    model_endpoint_id: Id | None = None


class AgentMessagePosted(Contract):
    type: Literal["agent.message"] = "agent.message"
    message_id: Id
    sender_agent_id: Id
    recipient_agent_id: Id
    message_type: str


class ArtifactReady(Contract):
    type: Literal["artifact.ready"] = "artifact.ready"
    artifact_id: Id
    name: ShortText
    media_type: str
    sha256: str


class WorkerUpdated(Contract):
    type: Literal["worker.updated"] = "worker.updated"
    worker_id: Id
    availability: DeviceAvailability
    current_task_id: Id | None = None


class BudgetUpdated(Contract):
    type: Literal["budget.updated"] = "budget.updated"
    reserved: Money
    spent: Money
    limit: Money


# --- proposed section 20 additions ---------------------------------------------------


class DelegationUpdated(Contract):
    type: Literal["delegation.updated"] = "delegation.updated"
    delegation_id: Id
    state: DelegationState
    worker_id: Id | None = None
    recipient_agent_id: Id | None = None


class ReservationUpdated(Contract):
    type: Literal["reservation.updated"] = "reservation.updated"
    reservation_id: Id
    worker_id: Id
    state: ResourceReservationState
    resources: ResourceVector


class CapabilityUpdated(Contract):
    type: Literal["capability.updated"] = "capability.updated"
    worker_id: Id | None = None
    agent_id: Id | None = None
    model_endpoint_id: Id | None = None
    stale: bool = False


class RepositoryOperationUpdated(Contract):
    type: Literal["repository.operation"] = "repository.operation"
    operation_id: Id
    task_id: Id
    binding_id: Id
    kind: str
    state: str
    pr_url: str | None = None


class SeatUpdated(Contract):
    type: Literal["seat.updated"] = "seat.updated"
    seat_key: str
    agent_id: Id
    label: ShortText
    status: Literal["working", "idle", "away"]
    status_text: ShortText | None = None
    task_id: Id | None = None


class SeatLeft(Contract):
    type: Literal["seat.left"] = "seat.left"
    seat_key: str
    agent_id: Id


class ClaimsChanged(Contract):
    """A seat's file claims were granted, renewed or released; re-read the board."""

    type: Literal["claims.changed"] = "claims.changed"
    seat_key: str
    agent_id: Id
    paths: list[str] = Field(description="The paths granted or released, as given.")


EventPayload = Annotated[
    SeatUpdated
    | SeatLeft
    | ClaimsChanged
    | RepositoryOperationUpdated
    | TaskUpdated
    | RunProgress
    | AgentMessagePosted
    | ArtifactReady
    | WorkerUpdated
    | BudgetUpdated
    | DelegationUpdated
    | ReservationUpdated
    | CapabilityUpdated,
    Field(discriminator="type"),
]


class EventEnvelope(Contract):
    """Replay semantics (GET /v1/projects/{id}/events and /events/stream):
    - JSON page: ?after=<cursor>&limit=; omitted `after` = from the oldest retained event.
    - SSE: each frame is `id: <cursor>`, `event: <type>`, `data: <EventEnvelope JSON>`.
      On reconnect the `Last-Event-ID` header takes precedence over `?after=`.
    - A cursor older than retention -> 410 `cursor_expired`; clients refetch current state
      (GET snapshot resources) and resume from `EventPage.head_cursor`.
    - Delivery is at-least-once: dedupe on event_id, drop entity_version <= last seen."""

    event_id: Id
    cursor: str = Field(description="Opaque, monotonic per project; equals SSE id.")
    schema_version: str = SCHEMA_VERSION
    project_id: Id
    entity_id: Id
    entity_version: int = Field(ge=0)
    type: str = Field(description="Always equals payload.type (validated).")
    timestamp: Timestamp
    payload: EventPayload

    @model_validator(mode="after")
    def _type_matches_payload(self) -> EventEnvelope:
        if self.type != self.payload.type:
            raise ValueError(f"envelope type {self.type!r} != payload type {self.payload.type!r}")
        return self
