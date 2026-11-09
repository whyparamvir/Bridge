"""Repository bindings and per-task repository operations (plan section 9).

Git execution and GitHub credentials stay in Codex's repository adapter. The server
records WHAT may happen (bound repository, task, operation key, base commit, branch),
leases each operation to a binding-scoped executor credential with a fence, and stores
the evidence it returns. Humans merge; there is no merge operation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, model_validator

from .artifacts import RepositoryBinding
from .common import Contract, FencingToken, GitSha, Id, IdempotencyKey, Page, Revision, Sha256, ShortText, Timestamp
from .compute import CheckEvidence

SEGMENT = r"^[A-Za-z0-9_.-]{1,100}$"


def _branch(v: str) -> str:
    """git check-ref-format subset: no '..', '//', leading/trailing '/', '.lock' suffix,
    leading '-' or '.'."""
    if (".." in v or "//" in v or v.startswith(("/", "-", ".")) or v.endswith(("/", ".lock", "."))
            or "@{" in v):
        raise ValueError("invalid branch name")
    return v


Branch = Annotated[str, Field(pattern=r"^[A-Za-z0-9._/-]{1,200}$"), AfterValidator(_branch)]


class CreateRepositoryBinding(Contract):
    provider: Literal["github"] = "github"
    owner: str = Field(pattern=SEGMENT)
    repo: str = Field(pattern=SEGMENT)
    repository_id: int = Field(ge=1, description="GitHub numeric repository id (pins identity across renames).")
    installation_id: int = Field(ge=1)
    default_branch: Branch = "main"

    @model_validator(mode="after")
    def _not_dots(self) -> CreateRepositoryBinding:
        if self.owner in (".", "..") or self.repo in (".", ".."):
            raise ValueError("invalid repository identifier")
        return self


class RevokeRepositoryBinding(Contract):
    expected_revision: Revision


class ExecutorCredential(Contract):
    binding_id: Id
    token: str = Field(description="Bearer hx_..., shown once; scoped to this binding's operations only.")
    expires_at: Timestamp


class RepositoryOperationKind(StrEnum):
    prepare_branch = "prepare_branch"  # isolated worktree pinned to base_commit; push head_branch
    open_draft_pr = "open_draft_pr"  # draft PR head_branch -> default branch (humans merge)


class RepositoryOperationState(StrEnum):
    requested = "requested"
    claimed = "claimed"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class RepositoryOperationParams(Contract):
    base_commit: GitSha
    head_branch: Branch
    title: ShortText | None = Field(None, description="Required for open_draft_pr.")
    body: str | None = Field(None, max_length=20_000)


class RepositoryEvidence(Contract):
    """Returned by the executor; untrusted until a reviewer looks at it."""

    head_commit: GitSha | None = None
    diff_sha256: Sha256 | None = None
    pr_number: int | None = Field(None, ge=1)
    pr_url: str | None = Field(None, max_length=300)
    checks: list[CheckEvidence] = Field(default_factory=list)
    reconciled: bool = Field(False, description="True when found via the operation marker after a lost response.")


class RepositoryOperation(Contract):
    id: Id
    project_id: Id
    task_id: Id
    binding_id: Id
    kind: RepositoryOperationKind
    operation_key: str
    params: RepositoryOperationParams
    state: RepositoryOperationState
    attempt_id: Id | None = None
    fencing_token: FencingToken | None = None
    lease_expires_at: Timestamp | None = None
    attempts: int
    max_attempts: int
    reconcile_required: bool = Field(description="A previous attempt may have acted; look before acting.")
    evidence: RepositoryEvidence | None = None
    error: ShortText | None = None
    requested_by_user_id: Id
    revision: Revision
    created_at: Timestamp
    updated_at: Timestamp


class CreateRepositoryOperation(Contract):
    binding_id: Id
    kind: RepositoryOperationKind
    operation_key: IdempotencyKey = Field(description="Stable id; repeating it returns the same operation.")
    params: RepositoryOperationParams

    @model_validator(mode="after")
    def _title(self) -> CreateRepositoryOperation:
        if self.kind is RepositoryOperationKind.open_draft_pr and not self.params.title:
            raise ValueError("open_draft_pr needs a title")
        return self


class RepositoryOperationAssignment(Contract):
    """Delivered only to the binding's executor credential."""

    operation: RepositoryOperation
    attempt_id: Id
    fencing_token: FencingToken
    lease_expires_at: Timestamp
    reconcile: bool = Field(description="Reconcile via the operation marker BEFORE any side effect.")
    owner: str
    repo: str
    repository_id: int | None
    installation_id: int
    default_branch: str


class ClaimRepositoryOperationResponse(Contract):
    assignment: RepositoryOperationAssignment | None


class RepositoryOperationProof(Contract):
    attempt_id: Id
    fencing_token: FencingToken


class RepositoryOperationHeartbeatResponse(Contract):
    lease_expires_at: Timestamp
    cancel_requested: bool


class CompleteRepositoryOperation(RepositoryOperationProof):
    outcome: Literal["succeeded", "failed"]
    evidence: RepositoryEvidence = Field(default_factory=RepositoryEvidence)
    error: ShortText | None = None


class RepositoryBindingPage(Page[RepositoryBinding]): ...
class RepositoryOperationPage(Page[RepositoryOperation]): ...
