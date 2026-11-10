"""Self-run team server (BRIDGE_ENV=local): the owner's loopback session and profile, and
limited-use invite links without passwords."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from .api import SessionToken
from .common import Contract, Id, ShortText, Timestamp
from .identity import WorkspaceAccess
from .work import Message, Task


class LocalSession(Contract):
    """Returned only to a request from the computer the server runs on."""

    session: SessionToken
    workspace_id: Id
    project_id: Id
    feed_conversation_id: Id


class UpdateMe(Contract):
    display_name: ShortText


class CreateInvite(Contract):
    """Body of createWorkspaceInvite; an omitted body uses these defaults."""

    project_id: Id | None = Field(None, description="Also grant editor on this project of the workspace.")
    max_uses: int = Field(10, ge=1, le=50)


SiblingLabel = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")]


class RedeemInvite(Contract):
    token: str = Field(min_length=1, max_length=200)
    display_name: ShortText = Field(description="The new member's name. Ignored when already signed in.")
    with_agent_credential: bool = Field(False, description="Also return an agent bearer token for the bridge.")
    agent_label: SiblingLabel | None = Field(None, description="Name of the new main agent, e.g. Opus 5.5.")


class InviteSession(Contract):
    """What redeeming an invite link returns. Tokens are shown once."""

    session: SessionToken | None = Field(
        None, description="A new person's session; null when the caller was already signed in.")
    agent_id: Id = Field(description="The person's agent in this workspace.")
    agent_token: str | None = Field(None, description="Agent bearer token (ha_) when requested.")
    workspace: WorkspaceAccess
    project_id: Id | None = Field(
        None, description="The project the person can now edit: the invite's project, or the team "
                          "project of a self-run server. Null when the invite grants none.")


SeatStatus = Literal["working", "idle", "away"]


class SeatUpdate(Contract):
    """Body of upsertSeat; also the session's heartbeat."""

    label: ShortText = Field(description="Who this session is, e.g. 'Seda - Opus - backend'.")
    status: SeatStatus
    status_text: ShortText | None = None
    task_id: Id | None = None


class Seat(Contract):
    """One Claude Code session on the team board."""

    seat_key: str
    agent_id: Id
    person_name: ShortText
    label: ShortText
    status: SeatStatus
    status_text: ShortText | None = None
    task_id: Id | None = None
    last_seen_at: Timestamp
    online: bool = Field(description="Seen within the last 15 minutes.")
    agent_name: ShortText | None = Field(None, description="The agent holding this seat, e.g. \"Seda's Codex\".")


ClaimPath = Annotated[str, StringConstraints(max_length=400)]


class ClaimRequest(Contract):
    """Body of claimFiles. All or nothing: a path that overlaps another seat's active claim
    grants none of them."""

    seat_key: str
    paths: list[ClaimPath] = Field(min_length=1, max_length=50, description=(
        "Repository-relative paths; a trailing '/' claims a whole folder. Compared case-insensitively."))
    intent: ShortText = Field(description="What the seat is doing with these files.")
    minutes: int = Field(60, ge=5, le=240, description="The claim lapses after this long unless renewed.")


class Claim(Contract):
    """An active claim on a file or folder (trailing '/')."""

    path: str
    intent: ShortText
    agent_id: Id
    person_name: ShortText
    seat_label: ShortText
    expires_at: Timestamp


class ClaimResult(Contract):
    granted: list[Claim] = Field(description="Empty when any path was refused.")
    conflicts: list[Claim] = Field(description="Other seats' active claims that blocked the request.")


class ReleaseRequest(Contract):
    seat_key: str
    paths: list[ClaimPath] | None = Field(None, max_length=50, description="Null releases all of the seat's claims.")


class ReleaseResult(Contract):
    released: int = Field(ge=0, description="How many claims were released.")


class BoardAgent(Contract):
    """One of a person's agents and its seats (online first)."""

    agent_id: Id
    number: int = Field(gt=0, description="Stable agent number for this Bridge server.")
    name: ShortText = Field(description="e.g. \"Seda's agents\" (the main agent) or \"Seda's Codex\".")
    label: ShortText = Field(description="The name without the person's \"Seda's \": \"agents\", \"Codex\".")
    seats: list[Seat] = Field(default_factory=list)


class BoardPerson(Contract):
    """A project member with their agents: the main agent first, then the others by age."""

    user_id: Id
    name: ShortText
    agent_id: Id | None = Field(None, description="The person's main agent.")
    seats: list[Seat] = Field(default_factory=list, description="Every seat of all the person's agents, online first.")
    agents: list[BoardAgent] = Field(default_factory=list)


class CreateSiblingAgent(Contract):
    """Body of createSiblingAgent."""

    label: SiblingLabel = Field(description=(
        "What sets the new agent apart, e.g. \"Codex\". Not \"agents\" or \"main\", which name the main agent."))


class SiblingAgent(Contract):
    """Another named agent of the caller's person. The token is shown once."""

    agent_id: Id
    name: ShortText = Field(description="\"{person}'s {label}\".")
    label: ShortText
    agent_token: str = Field(description="Agent bearer token (ha_) for this agent's bridge.")


class Board(Contract):
    """Everything a session needs to see the team at a glance."""

    project_id: Id
    feed_conversation_id: Id | None = None
    people: list[BoardPerson]
    claims: list[Claim] = Field(description="Active claims only.")
    tasks: list[Task] = Field(description="Up to 20 open tasks, newest first.")
    feed: list[Message] = Field(description="The last 20 feed messages, oldest first.")
    inbox_count: int = Field(ge=0, description="Unexpired notes and messages for the caller's agent; 0 for people.")
    head_cursor: str | None = Field(None, description="The project's current event cursor; null before any event.")
