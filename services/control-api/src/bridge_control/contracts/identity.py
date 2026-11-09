"""Identity, tenancy, agents and provider credentials (plan sections 7, 11)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Contract, Id, Money, Revision, ShortText, Slug, Timestamp


Email = str  # validated loosely; uniqueness and normalization are server-side


class User(Contract):
    id: Id
    display_name: ShortText
    email: Email | None = Field(None, max_length=320,
                                description="Null for the owner of a self-run (local) team server.")
    created_at: Timestamp


class Workspace(Contract):
    id: Id
    name: ShortText
    created_at: Timestamp


class WorkspaceRole(StrEnum):
    owner = "owner"
    member = "member"


class WorkspaceAccess(Workspace):
    role: WorkspaceRole


class WorkspaceMemberView(Contract):
    user_id: Id
    display_name: ShortText
    role: WorkspaceRole


class WorkspaceInvite(Contract):
    token: str
    expires_at: Timestamp
    url: str = Field(description="The join link to share: {public base URL}/#join={token}.")
    max_uses: int = Field(ge=1, le=50, description="How many people can join through this link.")


class Role(StrEnum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class Membership(Contract):
    """Deleting (revoking) a membership revokes future project access, worker
    assignments, artifact downloads and credential grants."""

    id: Id
    workspace_id: Id
    project_id: Id
    user_id: Id
    role: Role
    revoked_at: Timestamp | None = None
    revision: Revision = 0


class Project(Contract):
    id: Id
    workspace_id: Id
    slug: Slug
    name: ShortText
    objective: str = Field("", max_length=4000)
    created_at: Timestamp
    revision: Revision = 0


class AgentKind(StrEnum):
    personal_orchestrator = "personal_orchestrator"
    specialist = "specialist"
    reviewer = "reviewer"


class AgentProfile(Contract):
    """An agent identity. Distinct from device identity (section 20): one agent may use
    several authorized devices; one device may host several agents."""

    id: Id
    workspace_id: Id
    owner_user_id: Id
    kind: AgentKind
    name: ShortText
    lead_model_endpoint_id: Id | None = Field(
        None, description="Must reference a ModelEndpoint qualified as orchestrator."
    )
    max_delegation_depth: int = Field(2, ge=0, le=5)
    created_at: Timestamp


class BillingMode(StrEnum):
    personal = "personal"
    workspace = "workspace"


class ProviderConnection(Contract):
    """Server-side credential reference. The secret itself is never part of any contract;
    `secret_ref` names an entry in managed secret storage."""

    id: Id
    workspace_id: Id
    owner_user_id: Id | None = Field(None, description="Null for workspace-funded connections.")
    billing_mode: BillingMode
    provider: Slug = Field(description="Adapter key, e.g. 'anthropic', 'openai', 'ollama'.")
    display_name: ShortText
    secret_ref: str | None = Field(None, max_length=200, description="Opaque; never the key.")
    created_at: Timestamp
    disabled_at: Timestamp | None = None


class PayerGrant(Contract):
    """The fourth, separate opt-in: permission for *someone else* to spend this
    connection's credits. Absence of a live grant means no spending (default off)."""

    id: Id
    connection_id: Id
    project_id: Id
    grantee_user_id: Id
    allowed_model_endpoint_ids: list[Id] = Field(default_factory=list, description="Empty = none.")
    cap: Money
    expires_at: Timestamp
    revoked_at: Timestamp | None = None
    revision: Revision = 0
