"""The seven Bridge tools a Claude Code session gets, and the seat it holds on the board."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .client import BridgeClient, BridgeError, BridgeOffline

log = logging.getLogger("bridge_mcp")

HEARTBEAT_SECONDS = 300
MAX_BOARD_LINES = 60
OPEN_STATES = ("backlog", "ready", "leased", "running", "verifying", "review", "blocked")

_obj = lambda props, required=(): {  # noqa: E731
    "type": "object", "properties": props, "required": list(required), "additionalProperties": False}
_paths = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 400},
          "minItems": 1, "maxItems": 50}

SPECS: list[dict] = [
    {"name": "bridge_board",
     "description": "Show the team board: who is working on what, claimed files, open tasks and the recent feed.",
     "inputSchema": _obj({})},
    {"name": "bridge_update",
     "description": "Set this session's status on the board and optionally post the text to the team feed.",
     "inputSchema": _obj({
         "status": {"type": "string", "enum": ["working", "idle", "away"]},
         "text": {"type": "string", "maxLength": 200, "description": "What this session is doing."},
         "post_to_feed": {"type": "boolean", "description": "Also post the text to the team feed."}},
         ["status"])},
    {"name": "bridge_claim",
     "description": ("Call automatically before creating, editing, moving or deleting repository files. "
                     "Use the smallest set of paths, wait for success before writing, and release them when done. "
                     "If there is a conflict, coordinate with the holder before touching those paths."),
     "inputSchema": _obj({
         "paths": {**_paths, "description": "Repository-relative paths; a trailing '/' claims a folder."},
         "why": {"type": "string", "minLength": 1, "maxLength": 200, "description": "What you are doing."},
         "minutes": {"type": "integer", "minimum": 5, "maximum": 240, "description": "Default 60."}},
         ["paths", "why"])},
    {"name": "bridge_release",
     "description": "Release this session's claims on the given paths, or on all of them when no paths are given.",
     "inputSchema": _obj({"paths": _paths})},
    {"name": "bridge_message",
     "description": ("Send a note by agent number (#12), by address (\"seda/main\", \"seda/codex\", as the "
                     "board shows them), a teammate by name (\"Seda\"), one of their agents "
                     "(\"Seda's Codex\"), one of your own other agents by its label (\"Codex\", or \"main\" "
                     "for your main agent), or an agent id."),
     "inputSchema": _obj({
         "to": {"type": "string", "minLength": 1, "description": (
             "An agent number (#12), an address (\"seda/codex\"), a person's name, an agent's name, one of your agents' labels, "
             "\"main\" or an agent id.")},
         "text": {"type": "string", "minLength": 1, "maxLength": 4000}},
         ["to", "text"])},
    {"name": "bridge_inbox",
     "description": "Read the notes and messages teammates' agents sent to your agent.",
     "inputSchema": _obj({})},
    {"name": "bridge_tasks",
     "description": "List, create, claim or finish shared team tasks.",
     "inputSchema": _obj({
         "action": {"type": "string", "enum": ["list", "create", "claim", "done"]},
         "title": {"type": "string", "minLength": 1, "maxLength": 200, "description": "For create."},
         "description": {"type": "string", "maxLength": 20000, "description": "For create."},
         "acceptance_criteria": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200},
                                 "description": "For create; defaults to the title."},
         "task_id": {"type": "string", "description": "For claim and done."},
         "summary": {"type": "string", "maxLength": 4000, "description": "For done: what was achieved."}},
         ["action"])},
]


def hhmm(iso: str | None) -> str:
    """A server timestamp as local HH:MM."""
    if not iso:
        return "?"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")


def one_line(text: str, limit: int = 120) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _norm(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.endswith("/."):
        p = p[:-1]
    return p.casefold()


def _overlaps(a: str, b: str) -> bool:
    """Mirrors the server: a bare "src" also covers "src/a.py"."""
    a, b = _norm(a), _norm(b)
    return a == b or b.startswith(a.rstrip("/") + "/") or a.startswith(b.rstrip("/") + "/")


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class ToolSet:
    """One process = one seat. Holds the seat's status and the task leases it claimed."""

    def __init__(self, client: BridgeClient, seat_key: str, label: str,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        self.client, self.seat_key, self.label = client, seat_key, label
        self._agent_id = getattr(client.cfg, "agent_id", "") or ""
        self.status, self.status_text, self.task_id = "working", None, None
        self.leases: dict[str, tuple[str, int]] = {}  # task id -> (attempt id, fencing token)
        self.heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, Callable[[dict], dict]] = {
            "bridge_board": self._board, "bridge_update": self._update, "bridge_claim": self._claim,
            "bridge_release": self._release, "bridge_message": self._message, "bridge_inbox": self._inbox,
            "bridge_tasks": self._tasks}

    # ------------------------------------------------------------------ seat lifecycle

    def specs(self) -> list[dict]:
        return SPECS

    def agent_id(self) -> str:
        """This bridge's agent: saved at join, else asked of the server once."""
        if not self._agent_id:
            self._agent_id = str(self.client.me().get("agent_id") or "")
        return self._agent_id

    def _refresh(self) -> None:
        self.client.upsert_seat(self.seat_key, self.label, self.status, self.status_text, self.task_id)

    def start(self) -> None:
        """On initialize: take the seat as working and keep it fresh in the background."""
        self.status = "working"
        try:
            self._refresh()
        except (BridgeOffline, BridgeError) as e:
            log.warning("could not take a seat on the board: %s", _describe(e))
        if self._thread is None:
            self._thread = threading.Thread(target=self._beat, name="bridge-heartbeat", daemon=True)
            self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self._refresh()
                for task_id, (attempt_id, token) in list(self.leases.items()):
                    self.client.renew_task(task_id, attempt_id, token)
            except (BridgeOffline, BridgeError) as e:
                log.warning("heartbeat failed: %s", _describe(e))
            except Exception:  # noqa: BLE001 - the heartbeat must never die
                log.exception("heartbeat failed")

    def leave(self) -> None:
        """At end of input: leave the seat, which releases its claims. Best effort."""
        self._stop.set()
        try:
            self.client.leave_seat(self.seat_key)
        except (BridgeOffline, BridgeError) as e:
            log.warning("could not leave the seat: %s", _describe(e))

    # ------------------------------------------------------------------ dispatch

    def call(self, name: str, args: dict | None) -> dict:
        handler = self._handlers.get(name)
        if handler is None:
            return _result(f"Unknown tool: {name}", True)
        args = args or {}
        problem = _check(name, args)
        if problem:
            return _result(problem, True)
        try:
            if name != "bridge_update":
                self._refresh()
            return handler(args)
        except BridgeOffline as e:
            log.warning("Bridge is offline: %s", e.host)
            return _result(f"Bridge is offline: {e.host}", True)
        except BridgeError as e:
            return _result(e.detail, True)

    # ------------------------------------------------------------------ tools

    def _board(self, args: dict) -> dict:
        b = self.client.board()
        names = _names(b)
        lines: list[str] = []
        if b.get("inbox_count"):
            n = b["inbox_count"]
            lines.append(f"Inbox: {n} message{'' if n == 1 else 's'} waiting. Read with bridge_inbox.")
        lines.append("People:")
        for p in b["people"]:
            agents = person_agents(p)
            if not agents:
                lines.append(f"- {p['name']} — no agent yet")
                continue
            lines.append(f"- {p['name']} — " + " · ".join(
                f"{address(p, a)} ({a['name']}){number_tag(a)}: {self._seats_text(a.get('seats') or [])}" for a in agents))
        claims = b["claims"]
        lines.append(f"Claims ({len(claims)}):" if claims else "Claims: none")
        for c in claims[:12]:
            lines.append(f"- {c['path']}: {c['person_name']} · {c['seat_label']} until {hhmm(c['expires_at'])}: "
                         f"{one_line(c['intent'], 80)}")
        if len(claims) > 12:
            lines.append(f"  and {len(claims) - 12} more")
        tasks = b["tasks"]
        lines.append(f"Open tasks ({len(tasks)}):" if tasks else "Open tasks: none")
        for t in tasks[:10]:
            lines.append(f"- [{t['state']}] {one_line(t['title'], 80)} · {t['id']}")
        if len(tasks) > 10:
            lines.append(f"  and {len(tasks) - 10} more; see bridge_tasks list")
        feed = b["feed"][-8:]
        if feed:
            lines.append("Feed:")
            for m in feed:
                lines.append(f"- {hhmm(m['created_at'])} {names.get(m['author']['id'], m['author']['kind'])}: "
                             f"{one_line(m['body'])}")
        if len(lines) > MAX_BOARD_LINES:
            lines = lines[: MAX_BOARD_LINES - 1] + ["(board cut short)"]
        return _result("\n".join(lines))

    def _seats_text(self, seats: list[dict]) -> str:
        """'frontend (working: building login, this session), backend (idle)'."""
        if not seats:
            return "no session"
        shown = []
        for s in seats[:4]:
            bits = s["status"] + (f": {one_line(s['status_text'], 80)}" if s.get("status_text") else "")
            if not s.get("online"):
                bits += f", offline since {hhmm(s['last_seen_at'])}"
            if s["seat_key"] == self.seat_key:
                bits += ", this session"
            shown.append(f"{s['label']} ({bits})")
        more = f" and {len(seats) - 4} more" if len(seats) > 4 else ""
        return ", ".join(shown) + more

    def _update(self, args: dict) -> dict:
        text = (args.get("text") or "").strip()
        self.status, self.status_text = args["status"], (one_line(text, 200) if text else None)
        self._refresh()
        out = f"Status: {self.status}" + (f": {self.status_text}." if self.status_text else ".")
        if args.get("post_to_feed"):
            if text:
                self.client.post_feed(text)
                out += " Posted to the feed."
            else:
                out += " Nothing posted to the feed: no text."
        return _result(out)

    def _claim(self, args: dict) -> dict:
        paths = list(args["paths"])
        r = self.client.claim(self.seat_key, paths, args["why"], args.get("minutes"))
        if r["granted"]:
            until = hhmm(min(c["expires_at"] for c in r["granted"]))
            return _result(f"Claimed: {', '.join(c['path'] for c in r['granted'])} until {until}.")
        lines, blocked = [], set()
        for c in r["conflicts"]:
            mine = [p for p in paths if _overlaps(p, c["path"])] or [c["path"]]
            blocked.update(mine)
            held = "" if [_norm(c["path"])] == [_norm(p) for p in mine] else f" (holds {c['path']})"
            lines.append(f"Not claimed: {', '.join(mine)} is claimed by {c['person_name']} · {c['seat_label']} "
                         f"until {hhmm(c['expires_at'])}: {c['intent']}{held}")
        if set(paths) - blocked:
            lines.append("Nothing was claimed: a claim is all or nothing.")
        return _result("\n".join(lines))

    def _release(self, args: dict) -> dict:
        n = self.client.release(self.seat_key, args.get("paths"))
        return _result(f"Released {n} claim{'' if n == 1 else 's'}.")

    def _message(self, args: dict) -> dict:
        to = args["to"].strip().lstrip("@").strip()
        people = self.client.board()["people"]
        matches = resolve(people, self.agent_id(), to)
        if not matches:
            addresses = ", ".join(address(p, a) for p in people for a in person_agents(p)) or "none"
            if "/" in to:
                return _result(f"No agent at {to}. Addresses: {addresses}.", True)
            names = ", ".join(p["name"] for p in people) or "nobody"
            return _result(f"No one named {to} on the board. People: {names}. Addresses: {addresses}.", True)
        if len(matches) > 1:
            listed = "; ".join(f"{address(p, a)} ({a['name']}{number_tag(a)}, agent {a['agent_id']})" if a
                               else f"{p['name']} (agent none)" for p, a in matches)
            return _result(f"{len(matches)} agents match {to}: {listed}. Send to an agent number or ID instead.", True)
        person, agent = matches[0]
        if agent is None:
            return _result(f"{person['name']} has no agent yet.", True)
        if str(agent["agent_id"]) == self.agent_id():
            return _result("That's this agent.", True)
        self.client.send_note(agent["agent_id"], args["text"])
        return _result(f"Sent to {agent_display(person, agent)}.")

    def _inbox(self, args: dict) -> dict:
        items, complete = self.client.inbox_all()
        if not items:
            return _result("Inbox is empty.")
        board = self.client.board()
        names = board_senders(board)
        numbers = {str(a["agent_id"]): number_tag(a) for p in board["people"] for a in person_agents(p)}
        shown = items[-20:]  # the inbox comes oldest first: these are the newest
        count = f"{len(items)}" if complete else f"More than {len(items)}"
        lines = [f"{count} message{'' if len(items) == 1 else 's'}"
                 + (f", showing the newest {len(shown)}:" if len(shown) < len(items) else ":")]
        for m in shown:
            sender = names.get(m["sender_agent_id"], f"agent {m['sender_agent_id']}")
            tag = numbers.get(str(m["sender_agent_id"]), "")
            lines.append(f"- {hhmm(m['created_at'])}{tag} {sender}: {_payload_text(m)}")
        return _result("\n".join(lines))

    def _tasks(self, args: dict) -> dict:
        action = args["action"]
        if action == "list":
            tasks, complete = self.client.tasks_all()
            open_ = [t for t in tasks if t["state"] in OPEN_STATES]
            if not open_:
                return _result("No open tasks." if complete else "No open tasks among the first "
                               f"{len(tasks)}.")
            shown = open_[-30:][::-1]  # newest first
            count = f"{len(open_)}" if complete else f"At least {len(open_)}"
            lines = [f"{count} open task{'' if len(open_) == 1 else 's'}"
                     + (f", newest {len(shown)} first:" if len(shown) < len(open_) else ":")]
            lines += [f"- [{t['state']}] {one_line(t['title'], 100)} · {t['id']}" for t in shown]
            return _result("\n".join(lines))
        if action == "create":
            title = (args.get("title") or "").strip()
            if not title:
                return _result("bridge_tasks create needs a title.", True)
            t = self.client.create_task(title, args.get("description") or "", args.get("acceptance_criteria"))
            return _result(f"Created task: {t['title']} · {t['id']} ({t['state']}).")
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _result(f"bridge_tasks {action} needs a task_id.", True)
        if action == "claim":
            r = self.client.claim_task(task_id)
            lease = r["lease"]
            self.leases[task_id] = (lease["attempt_id"], lease["fencing_token"])
            self.task_id = task_id
            self._refresh()
            return _result(f"Claimed task: {r['task']['title']} until {hhmm(lease['expires_at'])}. "
                           f"Finish it with bridge_tasks done.")
        lease = self.leases.get(task_id)
        if lease is None:
            return _result("Claim the task again first.", True)
        try:
            t = self.client.finish_task(task_id, lease[0], lease[1], args.get("summary"))
        except BridgeError as e:
            if e.code == "stale_attempt":
                self.leases.pop(task_id, None)
                return _result("Claim the task again first.", True)
            raise
        self.leases.pop(task_id, None)
        if self.task_id == task_id:
            self.task_id = None
            self._refresh()
        return _result(f"Sent for review: {t['title']}. A person accepts it as done.")


def person_agents(person: dict) -> list[dict]:
    """The person's agents, main first: the board's `agents`, or (from a server that predates
    them) just the main agent with all the person's seats."""
    if person.get("agents"):
        return person["agents"]
    if not person.get("agent_id"):
        return []
    return [{"agent_id": person["agent_id"], "name": f"{person['name']}'s agents", "label": "agents",
             "seats": person.get("seats") or []}]


def number_tag(agent: dict) -> str:
    number = agent.get("number")
    return f" [#{number}]" if number is not None else ""


def _is_main(person: dict, agent: dict) -> bool:
    return str(agent["agent_id"]) == str(person.get("agent_id") or "")


def agent_display(person: dict, agent: dict) -> str:
    """How an agent is named in replies, the inbox and the hook: a main agent by its person's
    name ("Seda"), any other agent by its own name ("Seda's Codex")."""
    return person["name"] if _is_main(person, agent) and agent["label"] == "agents" else agent["name"]


def _slug(text: str) -> str:
    """"Mo Li" -> "mo-li": lowercased, each run of spaces a "-"."""
    return "-".join(text.casefold().split())


def address(person: dict, agent: dict) -> str:
    """An agent's address, "<person>/<label>": "seda/main" for Seda's main agent, "seda/codex"
    for Seda's Codex, "mo-li/my-codex-2" for Mo Li's "My Codex 2"."""
    return f"{_slug(person['name'])}/{'main' if _is_main(person, agent) else _slug(agent['label'])}"


def resolve(people: list[dict], me: str, to: str) -> list[tuple[dict, dict | None]]:
    """The (person, agent) pairs `to` names, by the first rule that matches anything: an
    agent number (#N); address ("seda/codex", see address); an agent id; an agent's exact name; "<person> <label>" or "<person>'s <label>"; the label of
    one of my person's agents ("main" or "agents" = my main agent); a person's name (their
    main agent, None when they have none); the label of someone else's agent. All
    case-insensitive. More than one pair means `to` is ambiguous."""
    key = to.casefold()
    pairs = [(p, a) for p in people for a in person_agents(p)]
    if key.startswith("#"):
        return [(p, a) for p, a in pairs if str(a.get("number")) == key[1:]]
    rules = (
        lambda p, a: "/" in key and address(p, a) == key,
        lambda p, a: str(a["agent_id"]).casefold() == key,
        lambda p, a: a["name"].casefold() == key,
        lambda p, a: key in (f"{p['name']} {a['label']}".casefold(), f"{p['name']}'s {a['label']}".casefold()),
    )
    for rule in rules:
        found = [(p, a) for p, a in pairs if rule(p, a)]
        if found:
            return found
    mine = next((p for p, a in pairs if me and str(a["agent_id"]) == me), None)
    if mine is not None:
        found = [(mine, a) for a in person_agents(mine)
                 if (_is_main(mine, a) and key in ("main", "agents")) or a["label"].casefold() == key]
        if found:
            return found
    found = [(p, next((a for a in person_agents(p) if _is_main(p, a)), None))
             for p in people if p["name"].casefold() == key]
    if found:
        return found
    return [(p, a) for p, a in pairs if not _is_main(p, a) and a["label"].casefold() == key]


def board_names(board: dict) -> dict[str, str]:
    """User ids to people's names, and agent ids to how agents are named (see agent_display)."""
    out: dict[str, str] = {}
    for p in board.get("people") or []:
        out[p["user_id"]] = p["name"]
        for a in person_agents(p):
            out[str(a["agent_id"])] = agent_display(p, a)
    return out


_names = board_names


def board_senders(board: dict) -> dict[str, str]:
    """Agent ids to how the inbox shows a sender: its address and name, "seda/codex (Seda's
    Codex)", so a reply can go straight to the address."""
    return {str(a["agent_id"]): f"{address(p, a)} ({agent_display(p, a)})"
            for p in board.get("people") or [] for a in person_agents(p)}


def _payload_text(m: dict) -> str:
    pl = m.get("payload") or {}
    if pl.get("type") == "note":
        return one_line(pl.get("text", ""), 300)
    for key in ("summary", "outcome", "question", "note", "detail", "reason"):
        if pl.get(key):
            return f"[{m['type']}] {one_line(pl[key], 300)}"
    return f"[{m['type']}]"


def _check(name: str, args: Any) -> str | None:
    """Enough schema checking to give the agent a plain error instead of a server one."""
    if not isinstance(args, dict):
        return f"{name} takes an object of arguments."
    schema = next(s for s in SPECS if s["name"] == name)["inputSchema"]
    for key in schema["required"]:
        if args.get(key) in (None, "", []):
            return f"{name} needs {key}."
    for key, value in args.items():
        prop = schema["properties"].get(key)
        if prop is None:
            return f"{name} has no argument {key}."
        if "enum" in prop and value not in prop["enum"]:
            return f"{key} must be one of: {', '.join(prop['enum'])}."
        if prop["type"] == "array" and value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            return f"{key} must be a list of strings."
        if prop["type"] == "string" and value is not None and not isinstance(value, str):
            return f"{key} must be text."
        if prop["type"] == "integer" and value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return f"{key} must be a whole number."
    return None


def _describe(e: Exception) -> str:
    return f"Bridge is offline: {e.host}" if isinstance(e, BridgeOffline) else str(e)
