"""`python -m bridge_mcp hook`: a Claude Code UserPromptSubmit hook. Prints what teammates did
since the last prompt, under "Bridge updates:". Any problem means no output; always exit 0.

Each Claude Code session keeps its own place: the hook reads Claude Code's JSON from stdin
and saves its cursor in hook-<project>-<agent>-<session_id>.cursor. It also saves the latest
place in hook-<project>-<agent>.cursor, which a new session (or a run without a session id)
starts from. Keying by agent keeps two agents on one computer (a person's Claude and Codex)
apart; a config without a saved agent id uses hook-<project>[-<session_id>].cursor.
A session file that can't be read falls back to the project file; when neither can be read
the hook starts from now and says nothing, as on its very first run."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import httpx

from . import config
from .client import BridgeClient, BridgeError, BridgeOffline
from .tools import board_names, person_agents

ABANDON = 2.0  # seconds from start; the hook has 3 in all
CLIENT_TIMEOUT = 2.0
STDIN_WAIT = 0.3  # Claude Code writes its JSON at once; never wait longer for it
NOTE_RESERVE = 0.3  # seconds the note lookup leaves for printing what was found
REPLACE_RETRY = 0.25  # seconds to retry replacing a state file another hook holds open
MAX_LINES = 15
FINISHED = ("review", "done")
SESSION_FILE_DAYS = 30  # session cursor files older than this are removed
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def session_id(stdin) -> str:  # noqa: ANN001
    """Claude Code's session id from the hook input on stdin, made safe for a file name;
    "" when there is no input, it isn't JSON or it doesn't arrive within STDIN_WAIT."""
    if stdin is None:
        return ""
    try:
        if stdin.isatty():
            return ""
    except Exception:  # noqa: BLE001 - a stand-in stdin that can't say
        return ""
    box: list = []

    def read() -> None:
        try:
            try:
                fd = stdin.fileno()
            except Exception:  # noqa: BLE001 - not a real file: read it whole
                data = stdin.read()
                box.append(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
                return
            raw = b""
            while True:  # stop as soon as a whole JSON object arrived, even if stdin stays open
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
                try:
                    json.loads(raw.decode("utf-8", "replace"))
                    break
                except ValueError:
                    continue
            box.append(raw.decode("utf-8", "replace"))
        except BaseException:  # noqa: BLE001
            pass

    th = threading.Thread(target=read, daemon=True)
    th.start()
    th.join(STDIN_WAIT)
    if not box:
        return ""
    try:
        sid = json.loads(box[0]).get("session_id")
    except (ValueError, AttributeError):
        return ""
    return _UNSAFE.sub("", sid)[:80] if isinstance(sid, str) else ""


def _state_paths(cfg: config.BridgeConfig, env: Mapping[str, str] | None, sid: str) -> list[Path]:
    """[this session's file, the project file], or just the project file without a session.
    Both are kept per project and agent."""
    base, project = config.default_path(env).parent, _UNSAFE.sub("", cfg.project_id)
    agent = _UNSAFE.sub("", cfg.agent_id)
    if agent:
        project = f"{project}-{agent}"
    own = [base / f"hook-{project}-{sid}.cursor"] if sid else []
    return [*own, base / f"hook-{project}.cursor"]


def _legacy_path(cfg: config.BridgeConfig, env: Mapping[str, str] | None) -> Path | None:
    """The project file from before cursors were kept per agent, read (never written) when
    this agent has no place of its own yet, so the first run after an upgrade loses nothing."""
    if not _UNSAFE.sub("", cfg.agent_id):
        return None
    return config.default_path(env).parent / f"hook-{_UNSAFE.sub('', cfg.project_id)}.cursor"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text("utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("cursor"), str) else None
    except (OSError, ValueError):
        return None


def _load(paths: list[Path]) -> dict | None:
    """The first readable state: this session's, else the project's."""
    for p in paths:
        state = _read(p)
        if state is not None:
            return state
    return None


def _atomic_write(path: Path, text: str) -> None:
    """Readers see the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        give_up = time.monotonic() + REPLACE_RETRY
        while True:
            try:
                os.replace(name, path)
                return
            except PermissionError:  # Windows: another session's hook has it open right now
                if time.monotonic() > give_up:
                    raise
                time.sleep(0.005)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _write(paths: list[Path], cursor: str | None, board: dict) -> None:  # cursor "" = no events yet
    text = json.dumps({"cursor": cursor or "", "feed": [m["id"] for m in board.get("feed") or []]})
    fresh = len(paths) > 1 and not paths[0].exists()
    for p in paths:
        try:
            _atomic_write(p, text)
        except OSError:
            if p is paths[0]:
                raise  # this session's own place must not be lost silently
    if fresh:
        _tidy(paths[0], paths[-1])


def _tidy(own: Path, project_file: Path) -> None:
    """Remove session files of this project not touched for SESSION_FILE_DAYS."""
    cutoff = time.time() - SESSION_FILE_DAYS * 86400
    try:
        for p in own.parent.glob(f"{project_file.stem}-*.cursor"):
            if p != own and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def _ts(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _lines(board: dict, events: list[dict], me: str, seen: set[str], notes: dict[str, str]) -> list[str]:
    # A main agent is named by its person ("Seda"), any other agent by its name ("Seda's Codex").
    # Only this agent's own events are skipped: the same person's other agents show.
    agents = board_names(board)
    users = {p["user_id"]: p["name"] for p in board.get("people") or []}
    my_user = next((p["user_id"] for p in board.get("people") or []
                    if any(str(a["agent_id"]) == me for a in person_agents(p))), "")
    tasks = {t["id"]: t for t in board.get("tasks") or []}
    claims = board.get("claims") or []
    out: list[tuple[datetime, str]] = []

    def who(agent_id: str | None) -> str:
        return agents.get(agent_id or "", "A teammate")

    for e in events:
        pl, ts = e["payload"], _ts(e["timestamp"])
        kind = pl.get("type")
        if kind in ("seat.updated", "seat.left", "claims.changed"):
            if pl.get("agent_id") == me:
                continue
            name = who(pl["agent_id"])
            if kind == "seat.left":
                out.append((ts, f"{name} left ({pl['seat_key'][:6]})"))
            elif kind == "seat.updated":
                text = f": {pl['status_text']}" if pl.get("status_text") else ""
                out.append((ts, f"{name} is on the board as {pl['label']} ({pl['status']}{text})"))
            else:
                paths = pl.get("paths") or []
                held = [c for c in claims if c["agent_id"] == pl["agent_id"] and c["path"] in paths]
                if held:
                    out.append((ts, f"{name} claimed {', '.join(c['path'] for c in held)}: {held[0]['intent']}"))
                else:
                    out.append((ts, f"{name} released {', '.join(paths)}"))
        elif kind == "agent.message":
            if pl.get("recipient_agent_id") == me:
                text = notes.get(pl["message_id"])
                out.append((ts, f"Note from {who(pl['sender_agent_id'])}: {text}" if text
                            else f"{who(pl['sender_agent_id'])} sent you a note"))
        elif kind == "task.updated":
            if pl.get("owner_agent_id") == me:
                continue
            t = tasks.get(e["entity_id"])
            title = t["title"] if t else "a task"
            state, prev = pl["state"], pl.get("previous_state")
            if prev is None:
                if "created_by_agent_id" in pl:  # who created it; my other agents' tasks show
                    if pl["created_by_agent_id"] == me:
                        continue
                elif t and my_user and t.get("payer_user_id") == my_user:
                    continue  # an older server: best effort, tasks my person created
                out.append((ts, f"New task: {title}"))
            elif state == "leased":
                out.append((ts, f"{who(pl.get('owner_agent_id'))} claimed task: {title}"))
            elif state in FINISHED:
                out.append((ts, f"{who(pl.get('owner_agent_id'))} finished task ({state}): {title}"))
    for m in board.get("feed") or []:
        ts = _ts(m["created_at"])
        a = m.get("author") or {}
        if m["id"] in seen or a.get("id") == me or a.get("kind") == "system":
            continue
        name = agents.get(a.get("id")) or users.get(a.get("id")) or "A teammate"
        out.append((ts, f"{name} posted: {' '.join(str(m['body']).split())[:200]}"))
    out.sort(key=lambda x: x[0])
    return [line for _, line in out]


def _emit(out: TextIO, text: str) -> None:
    """UTF-8 whatever the console's locale encoding is."""
    buf = getattr(out, "buffer", None)
    if buf is not None:
        out.flush()
        buf.write(text.encode("utf-8", "replace"))
        buf.flush()
    else:
        out.write(text)
        out.flush()


def run(env: Mapping[str, str] | None, transport: httpx.BaseTransport | None, out: TextIO,
        stdin: TextIO | None = None) -> int:
    """Always returns 0. The work runs in a daemon thread that is abandoned at ABANDON seconds."""
    import sys

    start = time.monotonic()
    box: list = []
    try:
        sid = session_id(sys.stdin if stdin is None else stdin)
    except Exception:  # noqa: BLE001
        sid = ""

    def work() -> None:
        try:
            box.append(_run(env, transport, start, sid))
        except BaseException:  # noqa: BLE001 - a hook must never get in the way of a prompt
            pass

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(max(0.0, ABANDON - (time.monotonic() - start)))
    if th.is_alive() or not box or box[0] is None:
        return 0
    text, commit = box[0]
    try:
        if text:
            _emit(out, text)
        commit()  # only after the text went out
    except Exception:  # noqa: BLE001
        pass
    return 0


def _run(env, transport, start: float, sid: str = ""):  # noqa: ANN001, ANN202
    """Returns (text, commit) or None."""
    deadline = start + ABANDON - 0.1

    def left(reserve: float = 0.0) -> float:
        r = deadline - reserve - time.monotonic()
        if r <= 0.05:
            raise TimeoutError
        return min(r, CLIENT_TIMEOUT)

    cfg = config.load(env)
    if cfg.missing():
        return None
    paths = _state_paths(cfg, env, sid)
    legacy = _legacy_path(cfg, env)
    state = _load(paths + ([legacy] if legacy else []))
    client = BridgeClient(cfg, transport=transport, timeout=CLIENT_TIMEOUT)
    try:
        board = client.board_timeout(left())
        if state is None:  # first run (or no readable state): start from now, say nothing
            return "", lambda: _write(paths, board.get("head_cursor"), board)
        try:
            page = client.events(state["cursor"] or None, timeout=left())
        except BridgeError as e:
            if e.status == 410 or e.code == "cursor_expired":
                return "", lambda: _write(paths, board.get("head_cursor"), board)
            raise
        events = page.get("events") or []
        me = cfg.agent_id or str(client.me(left()).get("agent_id") or "")
        if not me:
            return None
        wanted = {e["payload"].get("message_id") for e in events
                  if e["payload"].get("type") == "agent.message" and e["payload"].get("recipient_agent_id") == me}
        notes = _notes(client, wanted, lambda: left(NOTE_RESERVE)) if wanted else {}
        lines = _lines(board, events, me, set(state.get("feed") or []), notes)
        nxt = page.get("next_cursor") or state["cursor"]
        text = ""
        if lines:
            text = chr(10).join(["Bridge updates:", *lines[-MAX_LINES:]]) + chr(10)
        return text, lambda: _write(paths, nxt, board)
    finally:
        client.close()


def _notes(client: BridgeClient, wanted: set, budget) -> dict[str, str]:  # noqa: ANN001
    """Text of the wanted notes. The inbox is oldest first, so new notes are on its last
    page: page through while the budget lasts. Notes not found are reported as "X sent you a
    note"."""
    notes: dict[str, str] = {}
    try:
        for page, _more in client.inbox_pages(budget):
            for m in page:
                pl = m.get("payload") or {}
                if m.get("message_id") in wanted and pl.get("text"):
                    notes[m["message_id"]] = " ".join(str(pl["text"]).split())[:200]
            if len(notes) == len(wanted):
                break
    except (BridgeError, BridgeOffline, TimeoutError):
        pass
    return notes
