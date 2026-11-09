"""FastAPI control API. Every handler authenticates, opens ONE transaction and calls an
application service; authorization lives in the services (deny by default, non-members
see not_found). OpenAPI is generated from these routes (stable operation_ids)."""


import base64
import json
import logging
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..contracts import api as A
from ..contracts.artifacts import Artifact
from ..contracts.common import PAGE_LIMIT_DEFAULT, PAGE_LIMIT_MAX, SCHEMA_VERSION
from ..contracts.compute import (
    AgentDelegationConsent, AgentDelegationConsentUpdate, AttemptFence, DelegationAccept, DelegationAssignment,
    DelegationDecline, DelegationHeartbeat, DelegationHeartbeatResponse, DelegationRequest, DelegationResult,
    DelegationVerification, HeartbeatResponse, InferenceAssignment, InferenceCompletion, InferenceRequestView, Job,
    Lease, ModelEndpoint, SharingPolicy, SharingPolicyUpdate,
)
from ..contracts.errors import DomainError, ErrorCode, Problem
from ..contracts.identity import AgentProfile, Membership, PayerGrant, Project, ProviderConnection, Workspace, WorkspaceAccess, WorkspaceInvite
from ..contracts.artifacts import RepositoryBinding
from ..contracts import repository as R
from ..contracts.messaging import AgentMessage
from ..contracts.team import (
    Board,
    ClaimRequest, ClaimResult, CreateInvite, CreateSiblingAgent, InviteSession, LocalSession, RedeemInvite,
    ReleaseRequest, ReleaseResult, Seat, SeatUpdate, SiblingAgent, UpdateMe,
)
from ..contracts.work import Conversation, Decision, Message, Run, Task, TaskState
from ..core import Clock, Principal
from ..db.engine import Database, make_engine
from ..reliability import idempotent, read_events
from ..services import (
    Ctx, artifacts, capabilities, delegation, inference, local, models, repository, runs, scheduling, team, work, workspace,
)
from ..services.artifacts import BlobStore, LocalBlobStore
from ..settings import Settings

log = logging.getLogger("bridge.api")
M = TypeVar("M", bound=BaseModel)

_ERRORS = {s: {"model": Problem, "description": "Problem details"} for s in (400, 401, 402, 403, 404, 409, 410, 422, 429, 503)}


def _problem(err: DomainError) -> JSONResponse:
    prob = err.to_problem()
    return JSONResponse(prob.model_dump(mode="json", exclude_none=True), status_code=prob.status,
                        media_type="application/problem+json")


# ------------------------------------------------------------------- pagination


def _enc_offset(n: int) -> str:
    return base64.urlsafe_b64encode(f"o:{n}".encode()).decode().rstrip("=")


def _dec_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        tag, n = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().split(":")
        assert tag == "o" and int(n) >= 0
        return int(n)
    except Exception as e:  # noqa: BLE001
        raise DomainError(ErrorCode.validation_error, "malformed cursor") from e


def page(model: type[M], fetch: Callable[[int], list], cursor: str | None, limit: int) -> M:
    """`fetch(n)` returns the first n items in a stable order. Offset cursors are opaque."""
    offset = _dec_offset(cursor)
    items = fetch(offset + limit + 1)[offset:]
    more = len(items) > limit
    return model(items=items[:limit], next_cursor=_enc_offset(offset + limit) if more else None)


Cursor = Annotated[str | None, Query(description="Opaque cursor from a previous page.")]
Limit = Annotated[int, Query(ge=1, le=PAGE_LIMIT_MAX)]
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def create_app(settings: Settings | None = None, *, clock: Clock | None = None, db: Database | None = None,
               blob_store: BlobStore | None = None) -> FastAPI:
    settings = (settings or Settings.from_env()).validate()
    db = db or Database(make_engine(settings.database_url))
    ctx = Ctx(db=db, clock=clock or Clock(), settings=settings)
    store: BlobStore = blob_store or LocalBlobStore(settings.artifact_dir)

    app = FastAPI(
        title="Bridge control API", version=SCHEMA_VERSION,
        description="Proposed contract (not frozen). Errors are RFC 9457 problem details. "
                    "Lists return typed pages {items, next_cursor}; next_cursor is null at the end.",
    )
    app.state.ctx = ctx
    app.state.blob_store = store
    if settings.dev_auth:
        log.warning("DEVELOPMENT AUTH ENABLED (BRIDGE_ENV=%s). X-Bridge-Dev-User is trusted.", settings.env)

    @app.exception_handler(DomainError)
    def _domain(_: Request, err: DomainError) -> JSONResponse:
        return _problem(err)

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, err: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in err.errors()[:5])
        return _problem(DomainError(ErrorCode.validation_error, detail))

    @app.middleware("http")
    async def _dev_auth_header(request: Request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        if settings.dev_auth:
            response.headers["X-Bridge-Dev-Auth"] = "enabled"
        return response

    # ------------------------------------------------------------------ auth

    def principal(authorization: Annotated[str | None, Header()] = None,
                  x_bridge_dev_user: Annotated[str | None, Header()] = None) -> Principal:
        with ctx.db.tx() as conn:
            if authorization and authorization.lower().startswith("bearer "):
                p = workspace.authenticate(conn, ctx.now(), authorization[7:].strip(),
                                           local_env=settings.is_local)
                if p is not None:
                    return p
                raise DomainError(ErrorCode.unauthenticated, "invalid or expired token")
            if x_bridge_dev_user and settings.dev_auth and settings.is_dev:
                return workspace.dev_principal(conn, ctx.now(), x_bridge_dev_user)
        raise DomainError(ErrorCode.unauthenticated, "authentication required")

    P = Annotated[Principal, Depends(principal)]

    def optional_principal(authorization: Annotated[str | None, Header()] = None,
                           x_bridge_dev_user: Annotated[str | None, Header()] = None) -> Principal | None:
        """No credentials, or (self-run server) an expired sign-in: None, so a returning
        person whose browser still sends the old token can redeem a new link. Unknown,
        revoked and malformed credentials are refused (401), never ignored."""
        if authorization or (x_bridge_dev_user and settings.dev_auth and settings.is_dev):
            try:
                return principal(authorization, x_bridge_dev_user)
            except workspace.SignInExpired:
                return None
        return None

    OptP = Annotated[Principal | None, Depends(optional_principal)]

    def tx():  # noqa: ANN202
        return ctx.db.tx()

    def idem(p: Principal, route: str, key: str | None, body: Any, model: type[M], fn: Callable[[Any], M],
             status: int = 200) -> M:
        with tx() as conn:
            def run() -> tuple[int, Any]:
                return status, fn(conn).model_dump(mode="json")
            _, out = idempotent(conn, ctx.now(), p, route, key,
                                body.model_dump(mode="json") if isinstance(body, BaseModel) else body, run)
        return model.model_validate(out)

    common = {"responses": _ERRORS}

    # --------------------------------------------------------------- health

    @app.get("/v1/health", operation_id="getHealth", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "schema_version": SCHEMA_VERSION, "env": settings.env,
                "database": "sqlite (development fallback)" if settings.is_sqlite else "postgresql"}

    # ------------------------------------------------------------ accounts

    @app.post("/v1/auth/register", operation_id="registerAccount", status_code=201, tags=["auth"], **common)
    def register(body: A.RegisterAccount) -> A.Me:
        with tx() as conn:
            u = workspace.register(conn, ctx.now(), body)
        return A.Me(user=u, principal_kind="user")

    @app.post("/v1/auth/login", operation_id="login", tags=["auth"], **common)
    def login(body: A.Login) -> A.SessionToken:
        with tx() as conn:
            return workspace.login(conn, ctx.now(), body, timedelta(hours=settings.session_ttl_hours))

    @app.post("/v1/auth/logout", operation_id="logout", status_code=204, tags=["auth"], **common)
    def logout(p: P, authorization: Annotated[str | None, Header()] = None) -> Response:
        if authorization and authorization.lower().startswith("bearer "):
            with tx() as conn:
                workspace.logout(conn, ctx.now(), authorization[7:].strip())
        return Response(status_code=204)

    @app.get("/v1/me", operation_id="getMe", tags=["auth"], **common)
    def me(p: P) -> A.Me:
        with tx() as conn:
            return workspace.me(conn, p)

    @app.patch("/v1/me", operation_id="updateMe", tags=["auth"], **common)
    def update_me(p: P, body: UpdateMe) -> A.Me:
        with tx() as conn:
            return workspace.update_me(conn, p, body)

    @app.post("/v1/local/session", operation_id="startLocalSession", tags=["auth"], **common)
    def start_local_session(request: Request, response: Response) -> LocalSession:
        """Self-run team server only: the owner's session, for programs on this computer."""
        if not settings.is_local:
            raise DomainError(ErrorCode.not_found, "not found")
        local.require_loopback(request, settings)  # before anything is written
        response.headers["Cache-Control"] = "no-store"
        with tx() as conn:
            inst = local.bootstrap(conn, ctx.now(), settings)
            owner = workspace.user_row(conn, inst.owner_user_id)
            if owner.disabled_at is not None:
                raise DomainError(ErrorCode.permission_denied, local.REFUSAL)
            session = workspace.issue_session(conn, ctx.now(), owner, timedelta(hours=settings.session_ttl_hours))
        return LocalSession(session=session, workspace_id=inst.workspace_id, project_id=inst.project_id,
                            feed_conversation_id=inst.feed_conversation_id)

    # ----------------------------------------------------------- workspaces

    @app.post("/v1/workspaces", operation_id="createWorkspace", status_code=201, tags=["workspaces"], **common)
    def create_workspace(p: P, body: A.CreateWorkspace, key: IdemKey = None) -> Workspace:
        return idem(p, "createWorkspace", key, body, Workspace, lambda c: workspace.create_workspace(c, ctx.now(), p, body))

    @app.get("/v1/workspaces", operation_id="listWorkspaces", tags=["workspaces"], **common)
    def list_workspaces(p: P, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.WorkspacePage:
        with tx() as conn:
            return page(A.WorkspacePage, lambda n: workspace.list_workspaces(conn, p, n), cursor, limit)

    @app.get("/v1/workspaces/{workspace_id}/members", operation_id="listWorkspaceMembers",
             tags=["workspaces"], **common)
    def list_workspace_members(workspace_id: UUID, p: P, cursor: Cursor = None,
                               limit: Limit = PAGE_LIMIT_DEFAULT) -> A.WorkspaceMemberPage:
        with tx() as conn:
            return page(A.WorkspaceMemberPage, lambda n: workspace.list_workspace_members(conn, p, workspace_id, n), cursor, limit)

    @app.get("/v1/workspaces/{workspace_id}/agents", operation_id="listOwnedWorkspaceAgents",
             tags=["workspaces"], **common)
    def list_owned_workspace_agents(workspace_id: UUID, p: P, cursor: Cursor = None,
                                    limit: Limit = PAGE_LIMIT_DEFAULT) -> A.WorkspaceAgentPage:
        with tx() as conn:
            return page(A.WorkspaceAgentPage, lambda n: workspace.list_owned_agents(conn, p, workspace_id, n), cursor, limit)

    @app.post("/v1/workspaces/{workspace_id}/invites", operation_id="createWorkspaceInvite", status_code=201,
              tags=["workspaces"], **common)
    def create_workspace_invite(workspace_id: UUID, p: P, response: Response,
                                body: CreateInvite | None = None) -> WorkspaceInvite:
        """The body is optional; without one the link allows 10 uses and no project."""
        response.headers["Cache-Control"] = "no-store"
        with tx() as conn:
            return workspace.create_workspace_invite(conn, ctx.now(), p, workspace_id, body or CreateInvite(),
                                                     settings)

    @app.post("/v1/invites/redeem", operation_id="redeemInvite", tags=["workspaces"], **common)
    def redeem_invite(body: RedeemInvite, response: Response, p: OptP) -> InviteSession:
        """Join through an invite link, signed in or not (a name is enough). Optionally
        returns an agent credential for the person's Claude Code bridge."""
        response.headers["Cache-Control"] = "no-store"
        with tx() as conn:
            return workspace.redeem_invite(conn, ctx.now(), settings, body.token, body.display_name, p,
                                           body.with_agent_credential)

    @app.post("/v1/workspaces/join", operation_id="joinWorkspace", tags=["workspaces"], **common)
    def join_workspace(p: P, body: A.JoinWorkspace) -> WorkspaceAccess:
        with tx() as conn:
            return workspace.join_workspace(conn, ctx.now(), p, body)

    @app.post("/v1/workspaces/{workspace_id}/members", operation_id="addWorkspaceMember", status_code=204,
              tags=["workspaces"], **common)
    def add_ws_member(p: P, workspace_id: UUID, body: A.AddWorkspaceMember) -> Response:
        with tx() as conn:
            workspace.add_workspace_member(conn, ctx.now(), p, workspace_id, body)
        return Response(status_code=204)

    @app.post("/v1/workspaces/{workspace_id}/enrollment-grants", operation_id="createEnrollmentGrant",
              status_code=201, tags=["workers"], **common)
    def enrollment(p: P, workspace_id: UUID) -> A.EnrollmentGrant:
        with tx() as conn:
            return scheduling.create_enrollment_grant(conn, ctx.now(), p, workspace_id)

    @app.post("/v1/workspaces/{workspace_id}/provider-connections", operation_id="createProviderConnection",
              status_code=201, tags=["models"], **common)
    def create_connection(p: P, workspace_id: UUID, body: A.CreateProviderConnection) -> ProviderConnection:
        if body.workspace_id != workspace_id:
            raise DomainError(ErrorCode.validation_error, "workspace mismatch")
        with tx() as conn:
            return models.create_connection(conn, ctx.now(), p, body)

    @app.get("/v1/workspaces/{workspace_id}/provider-connections", operation_id="listProviderConnections",
             tags=["models"], **common)
    def list_connections(p: P, workspace_id: UUID, cursor: Cursor = None,
                         limit: Limit = PAGE_LIMIT_DEFAULT) -> A.ProviderConnectionPage:
        with tx() as conn:
            return page(A.ProviderConnectionPage, lambda n: models.list_connections(conn, p, workspace_id)[:n], cursor, limit)

    # --------------------------------------------------------------- agents

    @app.post("/v1/agents", operation_id="createAgent", status_code=201, tags=["agents"], **common)
    def create_agent(p: P, body: A.CreateAgent, key: IdemKey = None) -> AgentProfile:
        return idem(p, "createAgent", key, body, AgentProfile, lambda c: workspace.create_agent(c, ctx.now(), p, body))

    @app.post("/v1/agents/{agent_id}/credentials", operation_id="issueAgentCredential", status_code=201,
              tags=["agents"], **common)
    def agent_cred(p: P, agent_id: UUID) -> A.AgentCredential:
        with tx() as conn:
            return workspace.issue_agent_credential(conn, ctx.now(), p, agent_id,
                                                    timedelta(hours=settings.session_ttl_hours))

    @app.post("/v1/agents/me/siblings", operation_id="createSiblingAgent", status_code=201, tags=["team"], **common)
    def create_sibling_agent(p: P, body: CreateSiblingAgent, response: Response) -> SiblingAgent:
        """Self-run team server only. The calling agent adds another named agent for its
        person, e.g. "Alex's Codex" next to "Alex's agents", with its own key and inbox. Asking
        again for the same label returns that agent with a new key; at most 8 per person."""
        if not settings.is_local:
            raise DomainError(ErrorCode.not_found, "not found")
        response.headers["Cache-Control"] = "no-store"
        with tx() as conn:
            return workspace.create_sibling_agent(conn, ctx.now(), p, body.label,
                                                  timedelta(hours=settings.session_ttl_hours))

    @app.put("/v1/agents/{agent_id}/lead-model", operation_id="setAgentLeadModel", tags=["agents"], **common)
    def lead_model(p: P, agent_id: UUID, body: A.SetLeadModel) -> AgentProfile:
        with tx() as conn:
            return workspace.set_lead_model(conn, p, agent_id, body.model_endpoint_id)

    @app.get("/v1/agents/{agent_id}/projects/{project_id}/consent", operation_id="getAgentConsent",
             tags=["agents"], **common)
    def get_consent(p: P, agent_id: UUID, project_id: UUID) -> AgentDelegationConsent:
        with tx() as conn:
            return workspace.get_consent(conn, ctx.now(), p, agent_id, project_id)

    @app.put("/v1/agents/{agent_id}/projects/{project_id}/consent", operation_id="setAgentConsent",
             tags=["agents"], **common)
    def set_consent(p: P, agent_id: UUID, project_id: UUID, body: AgentDelegationConsentUpdate) -> AgentDelegationConsent:
        with tx() as conn:
            return workspace.set_consent(conn, ctx.now(), p, agent_id, project_id, body)

    # ------------------------------------------------------------- projects

    @app.post("/v1/projects", operation_id="createProject", status_code=201, tags=["projects"], **common)
    def create_project(p: P, body: A.CreateProject, key: IdemKey = None) -> Project:
        return idem(p, "createProject", key, body, Project, lambda c: workspace.create_project(c, ctx.now(), p, body))

    @app.get("/v1/projects", operation_id="listProjects", tags=["projects"], **common)
    def list_projects(p: P, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.ProjectPage:
        with tx() as conn:
            return page(A.ProjectPage, lambda n: workspace.list_projects(conn, p)[:n], cursor, limit)

    @app.get("/v1/projects/{project_id}", operation_id="getProject", tags=["projects"], **common)
    def get_project(p: P, project_id: UUID) -> Project:
        with tx() as conn:
            return workspace.get_project(conn, p, project_id)

    @app.get("/v1/projects/{project_id}/budget", operation_id="getBudget", tags=["budget"], **common)
    def get_budget(p: P, project_id: UUID) -> A.BudgetStatus:
        with tx() as conn:
            return workspace.budget_status(conn, p, project_id)

    @app.put("/v1/projects/{project_id}/budget", operation_id="setBudget", tags=["budget"], **common)
    def set_budget(p: P, project_id: UUID, body: A.SetBudget) -> A.BudgetStatus:
        with tx() as conn:
            return workspace.set_budget(conn, p, project_id, body)

    @app.get("/v1/projects/{project_id}/members", operation_id="listMembers", tags=["projects"], **common)
    def list_members(p: P, project_id: UUID, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.MembershipPage:
        with tx() as conn:
            return page(A.MembershipPage, lambda n: workspace.list_members(conn, p, project_id)[:n], cursor, limit)

    @app.post("/v1/projects/{project_id}/members", operation_id="addMember", status_code=201, tags=["projects"], **common)
    def add_member(p: P, project_id: UUID, body: A.AddMember) -> Membership:
        with tx() as conn:
            return workspace.add_member(conn, ctx.now(), p, project_id, body)

    @app.post("/v1/projects/{project_id}/members/{user_id}/revoke", operation_id="revokeMember", tags=["projects"], **common)
    def revoke_member(p: P, project_id: UUID, user_id: UUID, body: A.RevokeMember) -> Membership:
        with tx() as conn:
            return workspace.revoke_member(conn, ctx.now(), p, project_id, user_id, body.expected_revision)

    # -------------------------------------------------------- conversations

    @app.post("/v1/projects/{project_id}/conversations", operation_id="createConversation", status_code=201,
              tags=["conversations"], **common)
    def create_conversation(p: P, project_id: UUID, body: A.CreateConversation) -> Conversation:
        with tx() as conn:
            return work.create_conversation(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/conversations", operation_id="listConversations", tags=["conversations"], **common)
    def list_conversations(p: P, project_id: UUID, cursor: Cursor = None,
                           limit: Limit = PAGE_LIMIT_DEFAULT) -> A.ConversationPage:
        with tx() as conn:
            return page(A.ConversationPage, lambda n: work.list_conversations(conn, p, project_id)[:n], cursor, limit)

    @app.post("/v1/conversations/{conversation_id}/messages", operation_id="postMessage", status_code=201,
              tags=["conversations"], **common)
    def post_message(p: P, conversation_id: UUID, body: A.PostMessage, key: IdemKey = None) -> Message:
        return idem(p, f"postMessage:{conversation_id}", key or body.idempotency_key, body, Message,
                    lambda c: work.post_message(c, ctx.now(), p, conversation_id, body))

    @app.get("/v1/conversations/{conversation_id}/messages", operation_id="listMessages", tags=["conversations"], **common)
    def list_messages(p: P, conversation_id: UUID, cursor: Cursor = None,
                      limit: Limit = PAGE_LIMIT_DEFAULT) -> A.MessagePage:
        with tx() as conn:
            return page(A.MessagePage, lambda n: work.list_messages(conn, p, conversation_id, None, n), cursor, limit)

    # ----------------------------------------------------------------- runs

    @app.post("/v1/projects/{project_id}/runs", operation_id="createRun", status_code=201, tags=["runs"], **common)
    def create_run(p: P, project_id: UUID, body: A.CreateRun) -> Run:
        return idem(p, f"createRun:{project_id}", body.idempotency_key, body, Run,
                    lambda c: runs.create(c, ctx.now(), p, project_id, body))

    @app.get("/v1/projects/{project_id}/runs", operation_id="listRuns", tags=["runs"], **common)
    def list_runs(p: P, project_id: UUID, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.RunPage:
        with tx() as conn:
            return page(A.RunPage, lambda n: runs.list_for_project(conn, p, project_id, n), cursor, limit)

    @app.get("/v1/runs/{run_id}", operation_id="getRun", tags=["runs"], **common)
    def get_run(p: P, run_id: UUID) -> Run:
        with tx() as conn:
            return runs.get(conn, p, run_id)

    @app.post("/v1/runs/{run_id}/cancel", operation_id="cancelRun", tags=["runs"], **common)
    def cancel_run(p: P, run_id: UUID, body: A.CancelRun = Body(default_factory=A.CancelRun)) -> Run:
        with tx() as conn:
            return runs.cancel(conn, ctx.now(), p, run_id)

    @app.post("/v1/runs/{run_id}/accept", operation_id="acceptRun", tags=["runs"], **common)
    def accept_run(p: P, run_id: UUID) -> Run:
        with tx() as conn:
            return runs.accept(conn, ctx.now(), p, run_id)

    # ---------------------------------------------------------------- tasks

    @app.post("/v1/projects/{project_id}/tasks", operation_id="createTask", status_code=201, tags=["tasks"], **common)
    def create_task(p: P, project_id: UUID, body: A.CreateTask, key: IdemKey = None) -> Task:
        return idem(p, f"createTask:{project_id}", key or body.idempotency_key, body, Task,
                    lambda c: work.create_task(c, ctx.now(), p, project_id, body))

    @app.get("/v1/projects/{project_id}/tasks", operation_id="listTasks", tags=["tasks"], **common)
    def list_tasks(p: P, project_id: UUID, state: TaskState | None = None, cursor: Cursor = None,
                   limit: Limit = PAGE_LIMIT_DEFAULT) -> A.TaskPage:
        with tx() as conn:
            return page(A.TaskPage, lambda n: work.list_tasks(conn, p, project_id, state, None, n), cursor, limit)

    @app.get("/v1/tasks/{task_id}", operation_id="getTask", tags=["tasks"], **common)
    def get_task(p: P, task_id: UUID) -> Task:
        with tx() as conn:
            return work.task_contract(conn, work.readable_task(conn, p, task_id))

    @app.post("/v1/tasks/{task_id}/claim", operation_id="claimTask", tags=["tasks"], **common)
    def claim_task(p: P, task_id: UUID, body: A.ClaimTask) -> A.ClaimResponse:
        with tx() as conn:
            return work.claim_task(conn, ctx.now(), p, task_id, body)

    @app.post("/v1/tasks/{task_id}/renew", operation_id="renewTaskLease", tags=["tasks"], **common)
    def renew_task(p: P, task_id: UUID, body: A.RenewLease) -> Lease:
        with tx() as conn:
            return work.renew_task_lease(conn, ctx.now(), p, task_id, body)

    @app.post("/v1/tasks/{task_id}/transition", operation_id="transitionTask", tags=["tasks"], **common)
    def transition(p: P, task_id: UUID, body: A.TransitionTask) -> Task:
        with tx() as conn:
            return work.transition_task(conn, ctx.now(), p, task_id, body)

    # -------------------------------------------------------- team seats

    @app.put("/v1/projects/{project_id}/seats/{seat_key}", operation_id="upsertSeat", tags=["team"], **common)
    def upsert_seat(p: P, project_id: UUID, seat_key: str, body: SeatUpdate) -> Seat:
        """An agent's Claude Code session announces itself and what it is doing; call it
        again as a heartbeat."""
        with tx() as conn:
            return team.upsert_seat(conn, ctx.now(), p, project_id, seat_key, body)

    @app.delete("/v1/projects/{project_id}/seats/{seat_key}", operation_id="leaveSeat", status_code=204,
                tags=["team"], **common)
    def leave_seat(p: P, project_id: UUID, seat_key: str) -> Response:
        with tx() as conn:
            team.leave_seat(conn, ctx.now(), p, project_id, seat_key)
        return Response(status_code=204)

    @app.post("/v1/projects/{project_id}/claims", operation_id="claimFiles", tags=["team"], **common)
    def claim_files(p: P, project_id: UUID, body: ClaimRequest) -> ClaimResult:
        """A seat claims files or folders (trailing '/') before editing them. All or nothing:
        when another seat holds an overlapping path, nothing is granted and `conflicts` says who."""
        with tx() as conn:
            return team.claim(conn, ctx.now(), p, project_id, body)

    @app.post("/v1/projects/{project_id}/claims/release", operation_id="releaseFiles", tags=["team"], **common)
    def release_files(p: P, project_id: UUID, body: ReleaseRequest) -> ReleaseResult:
        with tx() as conn:
            return ReleaseResult(released=team.release(conn, ctx.now(), p, project_id, body))

    @app.get("/v1/projects/{project_id}/board", operation_id="getBoard", tags=["team"], **common)
    def get_board(p: P, project_id: UUID) -> Board:
        """People and their seats, active claims, open tasks, the recent feed, the caller's
        inbox count and the current event cursor."""
        with tx() as conn:
            return team.board(conn, ctx.now(), p, project_id)

    # -------------------------------------------------------- agent messages

    @app.post("/v1/projects/{project_id}/agent-messages", operation_id="postAgentMessage", status_code=201,
              tags=["agents"], **common)
    def post_agent_message(p: P, project_id: UUID, body: A.PostAgentMessage) -> AgentMessage:
        with tx() as conn:
            return work.post_agent_message(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/agent-messages/inbox", operation_id="agentInbox", tags=["agents"], **common)
    def inbox(p: P, project_id: UUID, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.AgentMessagePage:
        with tx() as conn:
            return page(A.AgentMessagePage, lambda n: work.agent_inbox(conn, ctx.now(), p, project_id, n), cursor, limit)

    # ------------------------------------------------------------- decisions

    @app.post("/v1/projects/{project_id}/decisions", operation_id="proposeDecision", status_code=201,
              tags=["decisions"], **common)
    def propose(p: P, project_id: UUID, body: A.ProposeDecision) -> Decision:
        with tx() as conn:
            return work.propose_decision(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/decisions", operation_id="listDecisions", tags=["decisions"], **common)
    def list_decisions(p: P, project_id: UUID, key: str | None = None, cursor: Cursor = None,
                       limit: Limit = PAGE_LIMIT_DEFAULT) -> A.DecisionPage:
        with tx() as conn:
            return page(A.DecisionPage, lambda n: work.list_decisions(conn, p, project_id, key, n, 0), cursor, limit)

    @app.post("/v1/decisions/{decision_id}/accept", operation_id="acceptDecision", tags=["decisions"], **common)
    def accept_decision(p: P, decision_id: UUID, body: A.AcceptDecision) -> Decision:
        with tx() as conn:
            return work.accept_decision(conn, ctx.now(), p, decision_id, body)

    # ------------------------------------------------------------- artifacts

    @app.post("/v1/artifacts/upload-intents", operation_id="createUploadIntent", status_code=201,
              tags=["artifacts"], **common)
    def upload_intent(p: P, body: A.UploadIntent) -> A.UploadIntentResponse:
        with tx() as conn:
            return artifacts.create_upload_intent(conn, ctx.now(), p, body)

    @app.put("/v1/artifacts/{artifact_id}/content", operation_id="uploadArtifactContent", status_code=204,
             tags=["artifacts"], **common,
             openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {
                 "schema": {"type": "string", "format": "binary"}}}}})
    async def upload_content(p: P, artifact_id: UUID, request: Request) -> Response:
        data = await request.body()
        with tx() as conn:
            artifacts.upload_content(conn, ctx.now(), p, store, artifact_id, data)
        return Response(status_code=204)

    @app.post("/v1/artifacts/{artifact_id}/finalize", operation_id="finalizeArtifact", tags=["artifacts"], **common)
    def finalize(p: P, artifact_id: UUID, body: A.FinalizeArtifact) -> Artifact:
        try:
            with tx() as conn:
                return artifacts.finalize(conn, ctx.now(), p, store, artifact_id, body)
        except DomainError as e:
            if e.code is ErrorCode.validation_error and "rejected" in e.detail:
                # Persist the rejected status even though we report an error.
                with tx() as conn:
                    import sqlalchemy as sa

                    from ..db import tables as t
                    conn.execute(sa.update(t.artifacts).where(t.artifacts.c.id == artifact_id,
                                                              t.artifacts.c.status == "pending_upload")
                                 .values(status="rejected"))
            raise

    @app.get("/v1/artifacts/{artifact_id}", operation_id="getArtifact", tags=["artifacts"], **common)
    def get_artifact(p: P, artifact_id: UUID) -> Artifact:
        with tx() as conn:
            return artifacts.contract(artifacts.readable(conn, p, artifact_id))

    @app.get("/v1/artifacts/{artifact_id}/content", operation_id="downloadArtifact", tags=["artifacts"],
             response_class=Response, **common)
    def download(p: P, artifact_id: UUID) -> Response:
        with tx() as conn:
            art, data = artifacts.download(conn, p, store, artifact_id)
        return Response(content=data, media_type=art.media_type,
                        headers={"ETag": f'"{art.sha256}"', "X-Content-SHA256": art.sha256})

    @app.get("/v1/projects/{project_id}/artifacts", operation_id="listArtifacts", tags=["artifacts"], **common)
    def list_artifacts(p: P, project_id: UUID, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.ArtifactPage:
        with tx() as conn:
            return page(A.ArtifactPage, lambda n: artifacts.list_for_project(conn, p, project_id, n), cursor, limit)

    # ------------------------------------------------- capabilities / models

    @app.get("/v1/projects/{project_id}/capabilities", operation_id="getCapabilities", tags=["capabilities"], **common)
    def get_capabilities(p: P, project_id: UUID) -> A.CapabilityView:
        with tx() as conn:
            return capabilities.summary(conn, ctx.now(), p, project_id)

    @app.post("/v1/projects/{project_id}/presence", operation_id="reportPresence", status_code=204,
              tags=["capabilities"], **common)
    def presence(p: P, project_id: UUID, body: A.ReportPresence) -> Response:
        with tx() as conn:
            delegation.report_presence(conn, ctx.now(), p, project_id, body)
        return Response(status_code=204)

    @app.post("/v1/projects/{project_id}/model-endpoints", operation_id="createModelEndpoint", status_code=201,
              tags=["models"], **common)
    def create_endpoint(p: P, project_id: UUID, body: A.CreateModelEndpoint) -> ModelEndpoint:
        with tx() as conn:
            return models.create_endpoint(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/model-endpoints", operation_id="listModelEndpoints", tags=["models"], **common)
    def list_endpoints(p: P, project_id: UUID, cursor: Cursor = None,
                       limit: Limit = PAGE_LIMIT_DEFAULT) -> A.ModelEndpointPage:
        with tx() as conn:
            return page(A.ModelEndpointPage, lambda n: models.list_endpoints(conn, p, project_id)[:n], cursor, limit)

    @app.post("/v1/model-endpoints/{endpoint_id}/probe", operation_id="requestModelProbe", status_code=202,
              tags=["models"], **common)
    def probe(p: P, endpoint_id: UUID) -> A.Accepted:
        with tx() as conn:
            models.request_probe(conn, ctx.now(), p, endpoint_id)
        return A.Accepted(detail="probe queued; results appear on the endpoint's probes")

    @app.post("/v1/projects/{project_id}/payer-grants", operation_id="createPayerGrant", status_code=201,
              tags=["budget"], **common)
    def create_grant(p: P, project_id: UUID, body: A.CreatePayerGrant) -> PayerGrant:
        with tx() as conn:
            return models.create_grant(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/payer-grants", operation_id="listPayerGrants", tags=["budget"], **common)
    def list_grants(p: P, project_id: UUID, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.PayerGrantPage:
        with tx() as conn:
            return page(A.PayerGrantPage, lambda n: models.list_grants(conn, p, project_id)[:n], cursor, limit)

    @app.post("/v1/payer-grants/{grant_id}/revoke", operation_id="revokePayerGrant", tags=["budget"], **common)
    def revoke_grant(p: P, grant_id: UUID) -> PayerGrant:
        with tx() as conn:
            return models.revoke_grant(conn, ctx.now(), p, grant_id)

    # --------------------------------------------------------------- workers

    @app.post("/v1/workers/register", operation_id="registerWorker", status_code=201, tags=["workers"], **common)
    def register_worker(body: A.RegisterWorker) -> A.RegisterWorkerResponse:
        with tx() as conn:
            return scheduling.register_worker(conn, ctx.now(), body)

    @app.get("/v1/workers", operation_id="listMyWorkers", tags=["workers"], **common)
    def my_workers(p: P, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.WorkerStatusPage:
        with tx() as conn:
            return page(A.WorkerStatusPage, lambda n: scheduling.list_my_workers(conn, ctx.now(), p)[:n], cursor, limit)

    @app.post("/v1/workers/{worker_id}/heartbeat", operation_id="workerHeartbeat", tags=["workers"], **common)
    def worker_heartbeat(p: P, worker_id: UUID, body: A.WorkerHeartbeat) -> HeartbeatResponse:
        with tx() as conn:
            return scheduling.heartbeat(conn, ctx.now(), p, worker_id, body)

    @app.get("/v1/workers/{worker_id}/sharing-policy", operation_id="getSharingPolicy", tags=["workers"], **common)
    def get_policy(p: P, worker_id: UUID) -> SharingPolicy:
        with tx() as conn:
            return scheduling.get_policy(conn, p, worker_id)

    @app.put("/v1/workers/{worker_id}/sharing-policy", operation_id="setSharingPolicy", tags=["workers"], **common)
    def set_policy(p: P, worker_id: UUID, body: SharingPolicyUpdate) -> SharingPolicy:
        with tx() as conn:
            return scheduling.update_policy(conn, ctx.now(), p, worker_id, body)

    @app.post("/v1/workers/{worker_id}/revoke", operation_id="revokeWorker", status_code=204, tags=["workers"], **common)
    def revoke_worker(p: P, worker_id: UUID) -> Response:
        with tx() as conn:
            scheduling.revoke_worker(conn, ctx.now(), p, worker_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------ jobs

    @app.post("/v1/projects/{project_id}/jobs", operation_id="createJob", status_code=201, tags=["jobs"], **common)
    def create_job(p: P, project_id: UUID, body: A.CreateJob, key: IdemKey = None) -> Job:
        return idem(p, f"createJob:{project_id}", key or body.idempotency_key, body, Job,
                    lambda c: scheduling.create_job(c, ctx.now(), p, project_id, body))

    @app.get("/v1/jobs/{job_id}", operation_id="getJob", tags=["jobs"], **common)
    def get_job(p: P, job_id: UUID) -> Job:
        with tx() as conn:
            return scheduling.get_job(conn, p, job_id)

    @app.get("/v1/jobs/{job_id}/inputs/{sha256}", operation_id="downloadJobInput", tags=["jobs"],
             response_class=Response,
             responses={**_ERRORS, 200: {"description": "Input bytes, verified against sha256 before sending",
                                         "content": {"application/octet-stream": {
                                             "schema": {"type": "string", "format": "binary"}}}}})
    def job_input(p: P, job_id: UUID, sha256: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
                  attempt_id: Annotated[UUID, Query(description="The worker's current attempt (from the grant URL).")]
                  ) -> Response:
        """Worker-only. Requires: this worker holds the job's CURRENT attempt under a live
        lease, the job is not cancelled, the device owner is still a project member, and
        `sha256` is one of the job's input_hashes. Stale attempts get 409 stale_attempt."""
        with tx() as conn:
            data = scheduling.job_input(conn, ctx.now(), p, store, job_id, sha256, attempt_id)
        return Response(content=data, media_type="application/octet-stream",
                        headers={"ETag": f'"{sha256}"', "X-Content-SHA256": sha256, "Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/jobs/claim", operation_id="claimJob", tags=["jobs"], **common)
    def claim_job(p: P, body: A.ClaimJob) -> A.ClaimJobResponse:
        with tx() as conn:
            return A.ClaimJobResponse(assignment=scheduling.claim_job(conn, ctx.now(), p, body))

    @app.post("/v1/jobs/{job_id}/start", operation_id="startJob", tags=["jobs"], **common)
    def start_job(p: P, job_id: UUID, body: AttemptFence) -> Job:
        with tx() as conn:
            return scheduling.start_job(conn, ctx.now(), p, job_id, body)

    @app.post("/v1/jobs/{job_id}/complete", operation_id="completeJob", tags=["jobs"], **common)
    def complete_job(p: P, job_id: UUID, body: A.CompleteJob) -> Job:
        with tx() as conn:
            return scheduling.complete_job(conn, ctx.now(), p, job_id, body)

    # ------------------------------------------------------------ repositories

    @app.post("/v1/projects/{project_id}/repositories", operation_id="createRepositoryBinding", status_code=201,
              tags=["repositories"], **common)
    def create_binding(p: P, project_id: UUID, body: R.CreateRepositoryBinding) -> RepositoryBinding:
        """Owner-only. Stores the GitHub App installation id and numeric repository id;
        no credential is ever sent to or stored by the server."""
        with tx() as conn:
            return repository.create_binding(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/repositories", operation_id="listRepositoryBindings", tags=["repositories"],
             **common)
    def list_bindings(p: P, project_id: UUID, cursor: Cursor = None,
                      limit: Limit = PAGE_LIMIT_DEFAULT) -> R.RepositoryBindingPage:
        with tx() as conn:
            return page(R.RepositoryBindingPage, lambda n: repository.list_bindings(conn, p, project_id)[:n], cursor, limit)

    @app.post("/v1/repositories/{binding_id}/revoke", operation_id="revokeRepositoryBinding", tags=["repositories"],
              **common)
    def revoke_binding(p: P, binding_id: UUID, body: R.RevokeRepositoryBinding) -> RepositoryBinding:
        with tx() as conn:
            return repository.revoke_binding(conn, ctx.now(), p, binding_id, body.expected_revision)

    @app.post("/v1/repositories/{binding_id}/executor-credentials", operation_id="issueRepositoryExecutorCredential",
              status_code=201, tags=["repositories"], **common)
    def executor_credential(p: P, binding_id: UUID) -> R.ExecutorCredential:
        with tx() as conn:
            return repository.issue_executor_credential(conn, ctx.now(), p, binding_id,
                                                        timedelta(hours=settings.session_ttl_hours))

    @app.post("/v1/tasks/{task_id}/repository-operations", operation_id="createRepositoryOperation", status_code=201,
              tags=["repositories"], **common)
    def create_repo_op(p: P, task_id: UUID, body: R.CreateRepositoryOperation) -> R.RepositoryOperation:
        """Idempotent on (binding, operation_key): a retry after a lost response returns
        the original operation instead of creating a second one."""
        with tx() as conn:
            op, _ = repository.create_operation(conn, ctx.now(), p, task_id, body)
            return op

    @app.get("/v1/tasks/{task_id}/repository-operations", operation_id="listTaskRepositoryOperations",
             tags=["repositories"], **common)
    def list_repo_ops(p: P, task_id: UUID, cursor: Cursor = None,
                      limit: Limit = PAGE_LIMIT_DEFAULT) -> R.RepositoryOperationPage:
        with tx() as conn:
            return page(R.RepositoryOperationPage, lambda n: repository.list_for_task(conn, p, task_id)[:n], cursor, limit)

    @app.get("/v1/repository-operations/{operation_id}", operation_id="getRepositoryOperation", tags=["repositories"],
             **common)
    def get_repo_op(p: P, operation_id: UUID) -> R.RepositoryOperation:
        with tx() as conn:
            return repository.get_operation(conn, p, operation_id)

    @app.post("/v1/repository-operations/{operation_id}/cancel", operation_id="cancelRepositoryOperation",
              tags=["repositories"], **common)
    def cancel_repo_op(p: P, operation_id: UUID) -> R.RepositoryOperation:
        with tx() as conn:
            return repository.cancel_operation(conn, ctx.now(), p, operation_id)

    @app.post("/v1/repositories/{binding_id}/operations/claim", operation_id="claimRepositoryOperation",
              tags=["repositories"], **common)
    def claim_repo_op(p: P, binding_id: UUID) -> R.ClaimRepositoryOperationResponse:
        """Executor credential for this binding only."""
        with tx() as conn:
            return R.ClaimRepositoryOperationResponse(assignment=repository.claim(conn, ctx.now(), p, binding_id))

    @app.post("/v1/repository-operations/{operation_id}/heartbeat", operation_id="repositoryOperationHeartbeat",
              tags=["repositories"], **common)
    def repo_op_heartbeat(p: P, operation_id: UUID, body: R.RepositoryOperationProof) -> R.RepositoryOperationHeartbeatResponse:
        with tx() as conn:
            return repository.heartbeat(conn, ctx.now(), p, operation_id, body)

    @app.post("/v1/repository-operations/{operation_id}/complete", operation_id="completeRepositoryOperation",
              tags=["repositories"], **common)
    def complete_repo_op(p: P, operation_id: UUID, body: R.CompleteRepositoryOperation) -> R.RepositoryOperation:
        with tx() as conn:
            return repository.complete(conn, ctx.now(), p, operation_id, body)

    # ------------------------------------------------------- inference bridge

    @app.post("/v1/projects/{project_id}/inference-requests", operation_id="createInferenceRequest", status_code=201,
              tags=["inference"], **common)
    def create_inference(p: P, project_id: UUID, body: A.CreateInferenceRequest) -> InferenceRequestView:
        """Bounded text task on a teammate's device-local model (no cloud call)."""
        with tx() as conn:
            return inference.submit(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/inference-requests/{request_id}", operation_id="getInferenceRequest", tags=["inference"], **common)
    def get_inference(p: P, request_id: UUID) -> InferenceRequestView:
        with tx() as conn:
            return inference.get(conn, p, request_id)

    @app.post("/v1/workers/{worker_id}/inference/claim", operation_id="claimInference", tags=["inference"], **common)
    def claim_inference(p: P, worker_id: UUID) -> A.ClaimInferenceResponse:
        with tx() as conn:
            return A.ClaimInferenceResponse(
                assignment=inference.claim(conn, ctx.now(), p, A.ClaimInference(worker_id=worker_id)))

    @app.post("/v1/inference-requests/{request_id}/complete", operation_id="completeInference", tags=["inference"],
              **common)
    def complete_inference(p: P, request_id: UUID, body: InferenceCompletion) -> InferenceRequestView:
        with tx() as conn:
            return inference.complete(conn, ctx.now(), p, request_id, body)

    # ------------------------------------------------------------ delegations

    @app.post("/v1/projects/{project_id}/delegations", operation_id="createDelegation", status_code=201,
              tags=["delegations"], **common)
    def create_delegation(p: P, project_id: UUID, body: A.CreateDelegation) -> DelegationRequest:
        with tx() as conn:
            return delegation.create(conn, ctx.now(), p, project_id, body)

    @app.get("/v1/projects/{project_id}/delegations", operation_id="listDelegations", tags=["delegations"], **common)
    def list_delegations(p: P, project_id: UUID, cursor: Cursor = None,
                         limit: Limit = PAGE_LIMIT_DEFAULT) -> A.DelegationPage:
        with tx() as conn:
            return page(A.DelegationPage, lambda n: delegation.list_for_project(conn, p, project_id, n), cursor, limit)

    @app.get("/v1/delegations/{delegation_id}", operation_id="getDelegation", tags=["delegations"], **common)
    def get_delegation(p: P, delegation_id: UUID) -> A.DelegationView:
        with tx() as conn:
            return delegation.get(conn, p, delegation_id)

    @app.get("/v1/agents/me/delegation-inbox", operation_id="delegationInbox", tags=["delegations"], **common)
    def delegation_inbox(p: P, cursor: Cursor = None, limit: Limit = PAGE_LIMIT_DEFAULT) -> A.DelegationPage:
        with tx() as conn:
            return page(A.DelegationPage, lambda n: delegation.inbox(conn, ctx.now(), p)[:n], cursor, limit)

    @app.post("/v1/delegations/{delegation_id}/assignment", operation_id="fetchDelegationAssignment",
              tags=["delegations"], **common)
    def fetch_assignment(p: P, delegation_id: UUID) -> DelegationAssignment:
        with tx() as conn:
            return delegation.fetch_assignment(conn, ctx.now(), p, delegation_id)

    @app.post("/v1/delegations/{delegation_id}/accept", operation_id="acceptDelegation", tags=["delegations"], **common)
    def accept_delegation(p: P, delegation_id: UUID, body: DelegationAccept) -> DelegationRequest:
        with tx() as conn:
            return delegation.accept(conn, ctx.now(), p, delegation_id, body)

    @app.post("/v1/delegations/{delegation_id}/decline", operation_id="declineDelegation", tags=["delegations"], **common)
    def decline_delegation(p: P, delegation_id: UUID, body: DelegationDecline) -> DelegationRequest:
        with tx() as conn:
            return delegation.decline(conn, ctx.now(), p, delegation_id, body)

    @app.post("/v1/delegations/{delegation_id}/heartbeat", operation_id="delegationHeartbeat", tags=["delegations"],
              **common)
    def delegation_heartbeat(p: P, delegation_id: UUID, body: DelegationHeartbeat) -> DelegationHeartbeatResponse:
        with tx() as conn:
            return delegation.heartbeat(conn, ctx.now(), p, delegation_id, body)

    @app.post("/v1/delegations/{delegation_id}/complete", operation_id="completeDelegation", tags=["delegations"],
              **common)
    def complete_delegation(p: P, delegation_id: UUID, body: DelegationResult) -> DelegationRequest:
        with tx() as conn:
            return delegation.complete(conn, ctx.now(), p, delegation_id, body)

    @app.post("/v1/delegations/{delegation_id}/verify", operation_id="verifyDelegation", tags=["delegations"], **common)
    def verify_delegation(p: P, delegation_id: UUID, body: DelegationVerification) -> DelegationRequest:
        with tx() as conn:
            return delegation.verify(conn, ctx.now(), p, delegation_id, body)

    @app.post("/v1/delegations/{delegation_id}/cancel", operation_id="cancelDelegation", tags=["delegations"], **common)
    def cancel_delegation(p: P, delegation_id: UUID) -> DelegationRequest:
        with tx() as conn:
            return delegation.cancel(conn, ctx.now(), p, delegation_id)

    # ---------------------------------------------------------------- events

    retention = timedelta(hours=settings.event_retention_hours)

    _sse_doc = {**_ERRORS, 200: {"description": "JSON EventPage, or SSE when Accept: text/event-stream "
                                                 "(id = cursor; Last-Event-ID beats ?after=)",
                                  "content": {"application/json": {"schema": {"$ref": "#/components/schemas/EventPage"}},
                                              "text/event-stream": {}}}}

    @app.get("/v1/projects/{project_id}/events", operation_id="listEvents", tags=["events"], responses=_sse_doc,
             response_model=A.EventPage)
    def list_events(p: P, project_id: UUID, after: str | None = None, limit: Limit = 100,
                    accept: Annotated[str | None, Header()] = None,
                    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
                    max_seconds: Annotated[float, Query(gt=0, le=60)] = 25.0) -> Any:
        """JSON page by default. With `Accept: text/event-stream` the same URL streams SSE,
        so one URL serves both polling and live clients."""
        from ..core import require_role

        if accept and "text/event-stream" in accept:
            return _sse(p, project_id, last_event_id or after, max_seconds)
        with tx() as conn:
            require_role(conn, p, project_id, "viewer")
            evs, nxt, head = read_events(conn, p, project_id, after, limit, ctx.now(), retention)
        return A.EventPage(events=evs, next_cursor=nxt, head_cursor=head)

    @app.get("/v1/projects/{project_id}/events/stream", operation_id="streamEvents", tags=["events"],
             response_class=StreamingResponse,
             responses={**_ERRORS, 200: {"content": {"text/event-stream": {}}, "description": "SSE; id = cursor"}})
    def stream_events(p: P, project_id: UUID, after: str | None = None,
                      last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
                      max_seconds: Annotated[float, Query(gt=0, le=60)] = 25.0) -> StreamingResponse:
        """Last-Event-ID (reconnect) takes precedence over ?after=. The stream closes after
        max_seconds; clients reconnect with Last-Event-ID. Membership is rechecked on every
        poll, so revocation ends the stream."""
        return _sse(p, project_id, last_event_id or after, max_seconds)

    def _sse(p: Principal, project_id: UUID, cursor: str | None, max_seconds: float) -> StreamingResponse:
        from ..core import require_role

        with tx() as conn:  # fail fast with a proper problem response
            require_role(conn, p, project_id, "viewer")
            read_events(conn, p, project_id, cursor, 1, ctx.now(), retention)

        def gen() -> Iterator[bytes]:
            nonlocal cursor
            deadline = time.monotonic() + max_seconds
            yield b"retry: 2000\n\n"
            while True:
                try:
                    with tx() as conn:
                        require_role(conn, p, project_id, "viewer")
                        evs, nxt, _ = read_events(conn, p, project_id, cursor, 100, ctx.now(), retention)
                except DomainError as e:
                    yield f"event: error\ndata: {json.dumps(e.to_problem().model_dump(mode='json'))}\n\n".encode()
                    return
                for ev in evs:
                    yield f"id: {ev.cursor}\nevent: {ev.type}\ndata: {ev.model_dump_json()}\n\n".encode()
                cursor = nxt
                if time.monotonic() >= deadline:
                    return
                if not evs:
                    time.sleep(0.5)
                    yield b": keep-alive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
