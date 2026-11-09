"""Conversations, tasks, runs and decisions (plan sections 5-8)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from .common import (
    ActorRef,
    Contract,
    FencingToken,
    Id,
    LongText,
    Money,
    Revision,
    ShortText,
    Timestamp,
    Visibility,
)


class ConversationScope(StrEnum):
    private = "private"  # one person + their orchestrator; never auto-copied to project memory
    project = "project"  # explicit project channel


class Conversation(Contract):
    id: Id
    project_id: Id
    scope: ConversationScope
    owner_user_id: Id | None = Field(None, description="Required for private scope.")
    created_at: Timestamp

    @model_validator(mode="after")
    def _private_needs_owner(self) -> Conversation:
        if self.scope is ConversationScope.private and self.owner_user_id is None:
            raise ValueError("private conversations require owner_user_id")
        return self


class MessageRole(StrEnum):
    user = "user"
    agent = "agent"
    system = "system"


class Message(Contract):
    id: Id
    conversation_id: Id
    project_id: Id
    role: MessageRole
    author: ActorRef
    body: LongText
    artifact_refs: list[Id] = Field(default_factory=list)
    created_at: Timestamp


class TaskState(StrEnum):
    backlog = "backlog"
    ready = "ready"
    leased = "leased"
    running = "running"
    verifying = "verifying"
    review = "review"
    done = "done"
    blocked = "blocked"
    failed = "failed"
    cancelled = "cancelled"


class Task(Contract):
    """Authoritative task schema (section 7)."""

    id: Id
    project_id: Id
    title: ShortText
    description: LongText = ""
    acceptance_criteria: list[ShortText] = Field(min_length=1)
    dependencies: list[Id] = Field(default_factory=list)
    owner_agent_id: Id | None = None
    state: TaskState = TaskState.backlog
    blocked_reason: ShortText | None = None
    revision: Revision = 0
    lease_id: Id | None = None
    lease_token: FencingToken | None = Field(None, description="Current fencing token, if leased.")
    budget_limit: Money
    payer_user_id: Id | None = Field(None, description="Null = workspace-funded.")
    visibility: Visibility = Visibility.project
    artifact_refs: list[Id] = Field(default_factory=list)
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _no_self_dependency(self) -> Task:
        if self.id in self.dependencies:
            raise ValueError("task cannot depend on itself")
        return self


class TaskDependency(Contract):
    task_id: Id
    depends_on_task_id: Id


class RunStatus(StrEnum):
    """User-facing honest status (section 2)."""

    queued = "queued"
    working = "working"
    blocked = "blocked"
    needs_review = "needs_review"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class Run(Contract):
    id: Id
    project_id: Id
    conversation_id: Id
    requested_by_user_id: Id
    orchestrator_agent_id: Id
    status: RunStatus = RunStatus.queued
    goal: LongText
    task_ids: list[Id] = Field(default_factory=list)
    budget_limit: Money
    idempotency_key: str = Field(min_length=8, max_length=128)
    revision: Revision = 0
    created_at: Timestamp
    finished_at: Timestamp | None = None


class AttemptOutcome(StrEnum):
    in_progress = "in_progress"
    succeeded = "succeeded"
    failed_retryable = "failed_retryable"
    failed_permanent = "failed_permanent"
    cancelled = "cancelled"
    fenced = "fenced"  # superseded by a newer attempt; any late result is discarded


class RunAttempt(Contract):
    id: Id
    run_id: Id
    attempt_number: int = Field(ge=1)
    checkpoint_ref: str | None = Field(None, max_length=200)
    outcome: AttemptOutcome = AttemptOutcome.in_progress
    error_code: str | None = None
    started_at: Timestamp
    ended_at: Timestamp | None = None


class DecisionStatus(StrEnum):
    proposed = "proposed"
    accepted = "accepted"
    superseded = "superseded"
    conflicted = "conflicted"  # competing accepted proposals; visible, never last-write-wins
    rejected = "rejected"


class Decision(Contract):
    id: Id
    project_id: Id
    key: ShortText = Field(description="Topic key; two accepted decisions on one key = conflict.")
    body: LongText
    status: DecisionStatus = DecisionStatus.proposed
    author: ActorRef
    source_refs: list[Id] = Field(default_factory=list, description="Authoritative records cited.")
    supersedes_id: Id | None = None
    version: int = Field(1, ge=1)
    revision: Revision = 0
    created_at: Timestamp
