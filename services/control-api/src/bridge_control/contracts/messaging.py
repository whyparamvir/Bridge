"""Structured inter-agent messages (plan section 6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Contract, Id, IdempotencyKey, Money, ShortText, Timestamp


class AgentMessageType(StrEnum):
    request = "request"
    accept = "accept"
    clarification = "clarification"
    progress = "progress"
    result = "result"
    reject = "reject"
    cancel = "cancel"
    note = "note"


class RequestPayload(Contract):
    type: Literal["request"] = "request"
    outcome: ShortText
    acceptance_criteria: list[ShortText] = Field(min_length=1)
    deadline: Timestamp
    budget: Money


class AcceptPayload(Contract):
    type: Literal["accept"] = "accept"
    note: ShortText | None = None


class ClarificationPayload(Contract):
    type: Literal["clarification"] = "clarification"
    question: str = Field(min_length=1, max_length=4000)


class ProgressPayload(Contract):
    type: Literal["progress"] = "progress"
    summary: ShortText  # execution summary, never hidden chain-of-thought
    percent: int | None = Field(None, ge=0, le=100)


class ResultPayload(Contract):
    type: Literal["result"] = "result"
    summary: str = Field(max_length=4000)
    artifact_ids: list[Id] = Field(default_factory=list)


class RejectPayload(Contract):
    type: Literal["reject"] = "reject"
    reason: Literal["unavailable", "policy", "capacity", "out_of_scope", "other"]
    detail: ShortText | None = None


class CancelPayload(Contract):
    type: Literal["cancel"] = "cancel"
    reason: ShortText | None = None


class NotePayload(Contract):
    type: Literal["note"] = "note"
    text: str = Field(min_length=1, max_length=4000)


AgentMessagePayload = Annotated[
    RequestPayload
    | AcceptPayload
    | ClarificationPayload
    | ProgressPayload
    | ResultPayload
    | RejectPayload
    | CancelPayload
    | NotePayload,
    Field(discriminator="type"),
]


class AgentMessage(Contract):
    message_id: Id
    project_id: Id
    sender_agent_id: Id
    recipient_agent_id: Id
    task_id: Id | None = None
    correlation_id: Id
    type: AgentMessageType
    payload: AgentMessagePayload
    created_at: Timestamp
    expires_at: Timestamp
    deduplication_key: IdempotencyKey

    @model_validator(mode="after")
    def _consistent(self) -> AgentMessage:
        if self.payload.type != self.type.value:
            raise ValueError("payload.type must equal message type")
        if self.sender_agent_id == self.recipient_agent_id:
            raise ValueError("agent cannot message itself")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self
