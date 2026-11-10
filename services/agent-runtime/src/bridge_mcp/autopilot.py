"""Autopilot: every agent on Bridge knows what the team is doing, claims before editing, hears
teammates mid-task and keeps the board accurate, without being asked.

- `instructions()`: the standing rules the MCP server returns at `initialize` (any client).
- `run_hook()`: `python -m bridge_mcp hook --event <name> [--repo ROOT]...` for Claude Code's
  SessionStart, PreToolUse (claim-before-edit), PostToolUse and (with --repo)
  UserPromptSubmit hooks. With --repo, a hook whose working folder (or, for pre-edit, whose
  target file) is outside every ROOT does nothing. Every event fails open: no config, Bridge
  offline, a slow server or any bug means exit 0 and no output, within ABANDON seconds.
- `cli()`: `python -m bridge_mcp autopilot`, which prints or merges the Claude Code settings
  and prints the AGENTS.md block for Codex.

This module imports only the standard library and `config` at load time: the scope check
and PostToolUse's once-a-minute check run before httpx and the client are imported, so a
hook call that has nothing to do exits fast.

Claude Code's hook output formats live in one place, `render()`:
- SessionStart: plain stdout, which Claude Code adds to the session's context.
- PreToolUse: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision":
  "deny", "permissionDecisionReason": ...}} on stdout, exit 0. Allowing prints nothing, so
  Claude Code's normal permission flow is unchanged.
- PostToolUse: {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ...}}.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from . import config

if TYPE_CHECKING:
    import httpx

    from .client import BridgeClient

ABANDON = 2.0  # seconds from start for any hook event
POST_TOOL_EVERY = 60.0  # seconds between PostToolUse attempts per session
IDENTITY_TIMEOUT = 2.0
CLIENT_TIMEOUT = 2.0
STDIN_WAIT = 0.3
REPLACE_RETRY = 0.25
MAX_START_LINES = 25
EDIT_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
CLAIMS_SWITCH = "BRIDGE_AUTOPILOT_CLAIMS"
LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")  # as in hook.py: the same state file names

CLAIM_RULE = (
    "When working in this Bridge workspace, inspect files freely. Before creating, editing, moving, or deleting "
    "repository files, call bridge_claim yourself with the specific repository-relative paths and a short "
    "description of the planned change. Wait for the claim to succeed before writing. Claim new paths as the "
    "scope changes. If a claim conflicts, message the holder with bridge_message and wait or work on unrelated "
    "files. If the claim fails for another reason, report it before editing. Keep claims while actively editing "
    "and call bridge_release for the paths once your edits are done.")

RULES = (
    "Call bridge_board when you start a task to see who is working on what.",
    "Set your status with bridge_update when you start a task and when you finish it.",
    "Call bridge_inbox between the steps of a longer task and before you finish.",
    "Answer notes addressed to you with bridge_message.",
    "Post decisions other people need to the team feed (bridge_update with post_to_feed).",
    "Never paste credentials, tokens or keys into Bridge.",
)

_now = time.time  # tests move the clock


# ------------------------------------------------------------------ standing instructions


def instructions(identity: str | None) -> str:
    """The rules an agent gets when it connects; names the agent when `identity` is known."""
    lines = [f"You are on Bridge as {identity}."] if identity else []
    lines += [CLAIM_RULE, "", *(f"- {r}" for r in RULES)]
    return "\n".join(lines)


def codex_block() -> str:
    """The same rules for a repository's AGENTS.md, for Codex."""
    return "\n".join(["## Bridge", "", CLAIM_RULE, "", *(f"- {r}" for r in RULES)])


def identity(client: BridgeClient, timeout: float = IDENTITY_TIMEOUT) -> str | None:
    """"Seda's Codex" (or "Seda's main agent"), or None when Bridge can't say in time. The team's
    name is not included: Bridge does not let an agent read its workspace's name."""
    return _bounded(lambda: _identity(client, time.monotonic() + timeout), timeout)


def _identity(client: BridgeClient, deadline: float) -> str | None:
    from .tools import person_agents

    def left() -> float:
        r = deadline - time.monotonic()
        if r <= 0.05:
            raise TimeoutError
        return r

    board = client.board_timeout(left())
    me = client.cfg.agent_id or str(client.me(left()).get("agent_id") or "")
    name = None
    for p in board.get("people") or []:
        for a in person_agents(p):
            if me and str(a["agent_id"]) == me:
                main = str(a["agent_id"]) == str(p.get("agent_id") or "")
                name = f"{p['name']}'s main agent" if main and a["label"] == "agents" else a["name"]
    return name


def connect_instructions(tools: Any) -> str:
    """For the MCP `initialize` result: never raises, never waits more than IDENTITY_TIMEOUT."""
    who = None
    try:
        from .client import BridgeClient

        client = getattr(tools, "client", None)
        if isinstance(client, BridgeClient):
            who = identity(client)
    except Exception:  # noqa: BLE001 - initialize must never fail
        who = None
    return instructions(who)


# ------------------------------------------------------------------ hook plumbing


def _bounded(fn: Callable[[], Any], seconds: float) -> Any:
    """fn() in a daemon thread, abandoned after `seconds`; None on timeout or any error."""
    box: list = []

    def work() -> None:
        try:
            box.append(fn())
        except BaseException:  # noqa: BLE001
            pass

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(max(0.0, seconds))
    return box[0] if box and not th.is_alive() else None


def read_input(stdin: TextIO | None) -> dict:
    """Claude Code's hook JSON from stdin; {} when there is none within STDIN_WAIT."""
    if stdin is None:
        return {}
    try:
        if stdin.isatty():
            return {}
    except Exception:  # noqa: BLE001
        return {}
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
            while True:  # stop at a whole JSON object even if stdin stays open
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
    try:
        data = json.loads(box[0]) if box else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _sid(data: dict) -> str:
    sid = data.get("session_id")
    return _UNSAFE.sub("", sid)[:80] if isinstance(sid, str) else ""


def render(event: str, text: str) -> str:
    """What the hook prints for Claude Code. The one place that knows its output formats."""
    if not text:
        return ""
    if event == "pre-edit":
        out = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": text}
    elif event == "post-tool":
        out = {"hookEventName": "PostToolUse", "additionalContext": text}
    else:  # session-start: plain stdout becomes session context
        return text if text.endswith("\n") else text + "\n"
    return json.dumps({"hookSpecificOutput": out}, ensure_ascii=False) + "\n"


def _emit(out: TextIO, text: str) -> None:
    """UTF-8 whatever the console's locale encoding is (as hook._emit)."""
    buf = getattr(out, "buffer", None)
    if buf is not None:
        out.flush()
        buf.write(text.encode("utf-8", "replace"))
        buf.flush()
    else:
        out.write(text)
        out.flush()


def _atomic_write(path: Path, text: str) -> None:
    """Readers see the old file or the new one; retries a file another process holds open."""
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
            except PermissionError:  # Windows: open in another process this instant
                if time.monotonic() > give_up:
                    raise
                time.sleep(0.005)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ repository scope


def _from_git_bash(raw: str) -> str:
    """"/c/Users/x" -> "C:/Users/x" on Windows; anything else unchanged."""
    m = re.match(r"^/([A-Za-z])(/.*)?$", raw)
    if os.name == "nt" and m:
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return raw


def _abs(raw: str, cwd: str | None = None) -> Path:
    p = Path(_from_git_bash(raw))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    return Path(os.path.abspath(p))


def git_root(path: str | os.PathLike) -> Path | None:
    """The nearest folder at or above `path` holding `.git` (a folder, or a worktree's file)."""
    p = _abs(str(path))
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def within(path: str | os.PathLike, roots: list[str]) -> bool:
    """Whether `path` is one of `roots` or inside one (case-insensitive on Windows)."""
    p = os.path.normcase(str(_abs(str(path))))
    for r in roots:
        root = os.path.normcase(str(_abs(r))).rstrip("\\/")
        if p == root or p.startswith(root + os.sep):
            return True
    return False


def repo_path(raw: str, cwd: str | None) -> tuple[str | None, str | None]:
    """(git repository root, repo-relative POSIX path) for an edited file; (None, None)
    outside a git repository (no `.git` file or folder above it)."""
    full = _abs(raw, cwd)
    for parent in full.parents:
        if (parent / ".git").exists():
            return str(parent), full.relative_to(parent).as_posix()
    return None, None


def _target(data: dict) -> str | None:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _cwd(data: dict) -> str:
    cwd = data.get("cwd")
    return cwd if isinstance(cwd, str) and cwd else os.getcwd()


def in_scope(event: str, data: dict, repos: list[str]) -> bool:
    """No --repo: everywhere. Otherwise pre-edit looks at the edited file, the other events at
    the session's working folder."""
    if not repos:
        return True
    if event == "pre-edit":
        target = _target(data)
        return bool(target) and within(_abs(target, data.get("cwd") if isinstance(data.get("cwd"), str) else None),
                                       repos)
    return within(_cwd(data), repos)


def _norm(path: str) -> str:
    """As tools._norm (kept here so this module loads without httpx)."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if p.endswith("/."):
        p = p[:-1]
    return p.casefold()


def covers(claim: str, path: str) -> bool:
    """The server's overlap rule, one way: the claim is the path, or a folder ("x/" or bare
    "x") above it. Case-insensitive."""
    c, p = _norm(claim), _norm(path)
    return c == p or p.startswith(c.rstrip("/") + "/")


# ------------------------------------------------------------------ running a hook


def run_hook(event: str, env: Mapping[str, str] | None, transport: httpx.BaseTransport | None,
             out: TextIO, stdin: TextIO | None = None, repos: list[str] | None = None) -> int:
    """Always returns 0. Prints nothing unless the event has something to say."""
    import sys

    start = time.monotonic()
    repos = list(repos or [])
    if event not in HANDLERS and event not in ("", "user-prompt-submit"):
        return 0
    try:
        data = read_input(sys.stdin if stdin is None else stdin)
        if not in_scope(event, data, repos):
            return 0
        if event in ("", "user-prompt-submit"):
            from . import hook

            return hook.run(env, transport, out, io.StringIO(json.dumps(data) if data else ""))
        if event == "post-tool" and not _post_tool_due(data, env):
            return 0
    except Exception:  # noqa: BLE001 - a hook never gets in the way
        return 0
    handler = HANDLERS[event]
    result = _bounded(lambda: handler(data, env, transport, start), ABANDON - (time.monotonic() - start))
    if not result:
        return 0
    text, commit = result
    try:
        if text:
            _emit(out, render(event, text))
        if commit:
            commit()  # only after the text went out
    except Exception:  # noqa: BLE001
        pass
    return 0


def _deadline(start: float) -> Callable[[float], float]:
    end = start + ABANDON - 0.1

    def left(reserve: float = 0.0) -> float:
        r = end - reserve - time.monotonic()
        if r <= 0.05:
            raise TimeoutError
        return min(r, CLIENT_TIMEOUT)
    return left


def _cfg(env: Mapping[str, str] | None) -> config.BridgeConfig | None:
    cfg = config.load(env)
    return None if cfg.missing() else cfg


# ------------------------------------------------------------------ pre-edit (PreToolUse)


def _pre_edit(data: dict, env, transport, start: float):  # noqa: ANN001, ANN202
    base = os.environ if env is None else env
    if str(base.get(CLAIMS_SWITCH, "")).strip().lower() in ("off", "0", "false", "no"):
        return None
    raw = _target(data)
    if raw is None:
        return None
    _root, rel = repo_path(raw, data.get("cwd") if isinstance(data.get("cwd"), str) else None)
    if rel is None:
        return None
    cfg = _cfg(env)
    if cfg is None:
        return None
    from .client import BridgeClient
    from .tools import board_names, hhmm

    left = _deadline(start)
    client = BridgeClient(cfg, transport=transport, timeout=CLIENT_TIMEOUT)
    try:
        board = client.board_timeout(left())
        me = cfg.agent_id or str(client.me(left()).get("agent_id") or "")
    finally:
        client.close()
    if not me:
        return None
    held = [c for c in board.get("claims") or [] if covers(c["path"], rel)]
    if any(str(c["agent_id"]) == me for c in held):
        return None
    if held:
        c = held[0]
        # "Alex · alex-laptop" for a person's main agent, "Seda's Codex · codex" for another agent
        agent = board_names(board).get(str(c["agent_id"])) or c.get("person_name") or "a teammate"
        who = " · ".join(x for x in (agent, c.get("seat_label")) if x)
        return (f"{rel} is claimed by {who} until {hhmm(c['expires_at'])}: {c['intent']}. "
                "Message them with bridge_message or work on other files."), None
    return f"Claim {rel} first: call bridge_claim with this path and why, then retry the edit.", None


# ------------------------------------------------------------------ post-tool (PostToolUse)


def _stamp_path(cfg: config.BridgeConfig, env: Mapping[str, str] | None, sid: str) -> Path:
    """Next to this session's cursor (hook-<project>-<agent>[-<session>].posttool)."""
    key = _UNSAFE.sub("", cfg.project_id)
    agent = _UNSAFE.sub("", cfg.agent_id)
    if agent:
        key = f"{key}-{agent}"
    if sid:
        key = f"{key}-{sid}"
    return config.default_path(env).parent / f"hook-{key}.posttool"


def _post_tool_due(data: dict, env: Mapping[str, str] | None) -> bool:
    """At most one attempt per POST_TOOL_EVERY seconds per session, whether it reaches Bridge
    or not: the time is saved before the attempt, so a slow or unreachable Bridge costs one
    wait a minute, not one per tool call."""
    cfg = _cfg(env)
    if cfg is None:
        return False
    stamp = _stamp_path(cfg, env, _sid(data))
    try:
        last = float(stamp.read_text("utf-8").strip())
    except (OSError, ValueError):
        last = None
    now = _now()
    if last is not None and 0 <= now - last < POST_TOOL_EVERY:
        return False
    _atomic_write(stamp, repr(now))
    return True


def _post_tool(data: dict, env, transport, start: float):  # noqa: ANN001, ANN202
    from . import hook

    got = hook._run(env, transport, start, _sid(data))
    if got is None:
        return None
    text, commit = got
    return text.rstrip("\n"), commit


# ------------------------------------------------------------------ session-start (SessionStart)


def _seats(agent: dict, one_line: Callable) -> str:
    on = [s for s in agent.get("seats") or [] if s.get("online", True)]
    return ", ".join(f"{s['label']} ({s['status']}" + (f": {one_line(s['status_text'], 60)}" if s.get("status_text")
                                                         else "") + ")" for s in on[:3])


def board_summary(board: dict, me: str, notes: list[dict] | None) -> list[str]:
    from .tools import _payload_text, board_names, hhmm, one_line, person_agents

    names = board_names(board)
    lines = ["Bridge: team board (bridge_board shows more). Claim files before editing them."]
    people = board.get("people") or []
    active = []
    for p in people:
        parts = [f"{names.get(str(a['agent_id']), a['name'])}: {seats}" for a in person_agents(p)
                 if (seats := _seats(a, one_line))]
        if parts:
            active.append(f"- {one_line(' · '.join(parts), 160)}")
    idle = [p["name"] for p in people if not any(_seats(a, one_line) for a in person_agents(p))]
    lines.append("People:" if active else "People: nobody is working right now")
    lines += active[:5] + ([f"  and {len(active) - 5} more"] if len(active) > 5 else [])
    if idle:
        lines.append(one_line("Not in a session: " + ", ".join(idle), 160))
    claims = board.get("claims") or []
    lines.append(f"Claims ({len(claims)}):" if claims else "Claims: none")
    for c in claims[:5]:
        mine = " (yours)" if str(c["agent_id"]) == me else ""
        lines.append(f"- {c['path']}: {c['person_name']} · {c['seat_label']}{mine} until {hhmm(c['expires_at'])}: "
                     f"{one_line(c['intent'], 70)}")
    if len(claims) > 5:
        lines.append(f"  and {len(claims) - 5} more")
    tasks = board.get("tasks") or []
    lines.append(f"Open tasks ({len(tasks)}):" if tasks else "Open tasks: none")
    lines += [f"- [{t['state']}] {one_line(t['title'], 80)} · {t['id']}" for t in tasks[:4]]
    if len(tasks) > 4:
        lines.append(f"  and {len(tasks) - 4} more")
    n = int(board.get("inbox_count") or 0)
    lines.append(f"Unread notes for you: {n}" + (" (read them with bridge_inbox)" if n else ""))
    for m in (notes or [])[-3:][::-1]:
        sender = names.get(str(m.get("sender_agent_id")), "a teammate")
        lines.append(f"- {hhmm(m.get('created_at'))} {sender}: {one_line(_payload_text(m), 120)}")
    if len(lines) > MAX_START_LINES:
        lines = lines[: MAX_START_LINES - 1] + ["(cut short; see bridge_board)"]
    return lines


def _session_start(data: dict, env, transport, start: float):  # noqa: ANN001, ANN202
    cfg = _cfg(env)
    if cfg is None:
        return None
    from . import hook
    from .client import BridgeClient, BridgeError, BridgeOffline

    left = _deadline(start)
    sid = _sid(data)
    paths = hook._state_paths(cfg, env, sid)[:1]  # this session's place (or the project's without one)
    client = BridgeClient(cfg, transport=transport, timeout=CLIENT_TIMEOUT)
    try:
        board = client.board_timeout(left())
        me = cfg.agent_id or str(client.me(left()).get("agent_id") or "")
        notes: list[dict] | None = None
        if board.get("inbox_count"):
            seen: list[dict] = []
            try:
                for page, _more in client.inbox_pages(lambda: left(0.3)):
                    seen = (seen + page)[-3:]
                notes = seen
            except (BridgeError, BridgeOffline, TimeoutError):
                notes = None  # only part of the inbox: its newest notes are unknown
    finally:
        client.close()
    text = "\n".join(board_summary(board, me, notes))
    if data.get("source") not in (None, "startup"):
        return text, None  # resume, clear, compact: the session keeps its place
    return text, lambda: hook._write(paths, board.get("head_cursor"), board)


def _session_end(data: dict, env, transport, start: float):  # noqa: ANN001, ANN202
    return None  # the MCP server's seat releases its own claims when the session ends


HANDLERS: dict[str, Callable] = {"session-start": _session_start, "pre-edit": _pre_edit,
                                 "post-tool": _post_tool, "session-end": _session_end}


# ------------------------------------------------------------------ `autopilot` command


def _quoted(path: str | os.PathLike) -> str:
    from .join import quoted

    return quoted(path)


def settings_block(repos: list[str | os.PathLike], cfg_path: str | os.PathLike | None = None) -> dict:
    """Claude Code settings with every Bridge hook, bound to `repos`; `cfg_path` for an agent
    with its own config."""
    from .join import bridge_command

    suffix = (f" --config {_quoted(cfg_path)}" if cfg_path else "") + "".join(
        f" --repo {_quoted(r)}" for r in repos)

    def cmd(sub: str) -> list[dict]:
        return [{"type": "command", "command": bridge_command(sub + suffix)}]

    return {"hooks": {
        "SessionStart": [{"hooks": cmd("hook --event session-start")}],
        "UserPromptSubmit": [{"hooks": cmd("hook")}],
        "PreToolUse": [{"matcher": EDIT_TOOLS, "hooks": cmd("hook --event pre-edit")}],
        "PostToolUse": [{"matcher": "*", "hooks": cmd("hook --event post-tool")}],
    }}


_ARG = r'--{0}(?:=|\s+)(?:"([^"]*)"|(\S+))'


def _signature(command: str) -> tuple[str, frozenset[str]]:
    """(the --config value, the --repo values) of a Bridge hook command, normalised."""
    def norm(v: str) -> str:
        v = v.replace("\\", "/").rstrip("/")
        return v.casefold() if os.name == "nt" else v

    cfg = re.search(_ARG.format("config"), command)
    repos = re.findall(_ARG.format("repo"), command)
    return (norm(cfg.group(1) or cfg.group(2)) if cfg else "", frozenset(norm(a or b) for a, b in repos))


def is_bridge_hook(entry: Any) -> bool:
    command = entry.get("command") if isinstance(entry, dict) else None
    return isinstance(command, str) and "bridge_mcp" in command and " hook" in command


def _replaces(entry: Any, mine: tuple[str, frozenset[str]]) -> bool:
    """A Bridge hook for the same agent config whose repositories overlap these (or that has
    none: an older, unscoped install). Other agents' and other repositories' hooks are kept."""
    if not is_bridge_hook(entry):
        return False
    cfg, repos = _signature(entry["command"])
    return cfg == mine[0] and (not repos or bool(repos & mine[1]))


def _unscoped(entry: Any, cfg: str) -> bool:
    """A Bridge hook for agent config `cfg` with no --repo (join's snippet, older installs)."""
    if not is_bridge_hook(entry):
        return False
    c, repos = _signature(entry["command"])
    return c == cfg and not repos


def _strip(settings: dict, drop: Callable[[Any], bool]) -> tuple[dict, int]:
    """(settings without the hook entries `drop` picks, how many). Everything else is kept;
    a group left empty by the removal goes too."""
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" is not an object')
    new_hooks: dict = {}
    removed = 0
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept = []
        for g in groups:
            entries = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(entries, list):
                kept.append(g)
                continue
            rest = [h for h in entries if not drop(h)]
            removed += len(entries) - len(rest)
            if rest or not entries:
                kept.append({**g, "hooks": rest})
        new_hooks[event] = kept
    return {**settings, "hooks": new_hooks}, removed


def _block_signature(block: dict) -> tuple[str, frozenset[str]]:
    return _signature(next(iter(block["hooks"].values()))[0]["hooks"][0]["command"])


def merge(settings: dict, block: dict) -> tuple[dict, int, int]:
    """(new settings, Bridge hook entries removed, added). Keeps every other key and hook."""
    mine = _block_signature(block)
    stripped, removed = _strip(settings, lambda h: _replaces(h, mine))
    new_hooks = stripped["hooks"]
    added = 0
    for event, groups in block["hooks"].items():
        new_hooks[event] = [*new_hooks.get(event, []), *groups]
        added += sum(len(g["hooks"]) for g in groups)
    return stripped, removed, added


def _write_settings(path: Path, settings: dict) -> Path | None:
    """Atomic write; the old file (if any) is copied to <name>.bak-<timestamp> first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        backup.write_bytes(path.read_bytes())
    _atomic_write(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    return backup


def _read_settings(path: Path) -> dict:
    """The settings file as an object ({} when missing); ValueError/OSError when unusable."""
    current = json.loads(path.read_text("utf-8-sig") or "{}") if path.exists() else {}
    if not isinstance(current, dict):
        raise ValueError("not an object")
    return current


def _short(e: Exception) -> str:
    return " ".join(str(e).split())[:80]


def user_settings_path() -> Path:
    """Claude Code's user settings (tests replace this)."""
    return Path.home() / ".claude" / "settings.json"


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _drop_user_hooks(user: Path, target: Path, block: dict, roots: list[Path]) -> str | None:
    """Remove this agent's unscoped Bridge hooks from the user settings, which would run next
    to the repo-local ones and say everything twice. One line to print, or None."""
    if _same_file(user, target) or not user.exists():
        return None
    try:
        current = _read_settings(user)
        cleaned, removed = _strip(current, lambda h: _unscoped(h, _block_signature(block)[0]))
        if not removed:
            return None
        backup = _write_settings(user, cleaned)
    except (ValueError, OSError) as e:
        return f"Left {user} unchanged ({_short(e)}): remove its Bridge hook entries by hand."
    where = ", ".join(str(r) for r in roots)
    return (f"Removed {removed} Bridge hook{'' if removed == 1 else 's'} without --repo from {user}"
            + (f" (backup at {backup.name})" if backup else "") + f"; the hooks in {target} now cover {where}.")


def _gitignore_warning(root: Path, target: Path) -> str | None:
    """A reminder when the repo's .gitignore exists and doesn't mention the local settings."""
    gi = root / ".gitignore"
    try:
        if target.resolve().parent.parent != root.resolve() or not gi.is_file():
            return None
        if "settings.local.json" in gi.read_text("utf-8-sig"):
            return None
    except OSError:
        return None
    return f"Note: add {LOCAL_SETTINGS.as_posix()} to {gi} so it isn't committed."


RESTART = "Restart Claude Code to load the hooks."


def cli(args: list[str], out: TextIO, err: TextIO) -> int:
    import argparse

    from .join import bridge_command

    ap = argparse.ArgumentParser(prog="python -m bridge_mcp autopilot",
                                 description="Set up Bridge's Claude Code hooks, or print the Codex AGENTS.md block.")
    ap.add_argument("--config", help="The bridge config the hooks use (default: the usual one).")
    ap.add_argument("--repo", action="append", default=[],
                    help="A team repository the hooks act in (repeatable; default: the git root of this folder).")
    ap.add_argument("--write", action="store_true", help="Merge the hooks into a Claude Code settings file.")
    ap.add_argument("--settings", help="Settings file for --write (default: <first repo>/.claude/settings.local.json).")
    ap.add_argument("--user-settings", help="Claude Code's user settings, whose unscoped Bridge hooks --write "
                                            "removes (default: ~/.claude/settings.json).")
    ap.add_argument("--keep-user-hooks", action="store_true",
                    help="Leave Bridge hooks without --repo in the user settings alone.")
    ap.add_argument("--codex", action="store_true", help="Print the AGENTS.md block for Codex.")
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0)
    if ns.codex:
        print(codex_block(), file=out)
        return 0
    if ns.repo:
        roots = [_abs(r) for r in ns.repo]
    else:
        here = git_root(os.getcwd())
        if here is None:
            print("Not in a git repository: run this in your team's repository or pass --repo PATH.", file=out)
            return 1
        roots = [here]
    cfg_path = Path(ns.config).expanduser().absolute() if ns.config else None
    block = settings_block(roots, cfg_path)
    if not ns.write:
        print(json.dumps(block, indent=2), file=out)
        again = "autopilot --write" + (f" --config {_quoted(cfg_path)}" if cfg_path else "") + "".join(
            f" --repo {_quoted(r)}" for r in roots)
        print(f"Add this to {_quoted(roots[0] / LOCAL_SETTINGS)}, or run: {bridge_command(again)}", file=out)
        print(RESTART, file=out)
        return 0
    path = Path(ns.settings).expanduser() if ns.settings else roots[0] / LOCAL_SETTINGS
    try:
        merged, removed, added = merge(_read_settings(path), block)
    except (ValueError, OSError) as e:
        print(f"{path} is not valid JSON settings ({_short(e)}); nothing was changed.", file=out)
        return 1
    try:
        backup = _write_settings(path, merged)
    except OSError as e:
        print(f"Could not write {path} ({_short(e)}); nothing was changed.", file=out)
        return 1
    print(f"Updated {path}: removed {removed} old Bridge hook{'' if removed == 1 else 's'}, added {added}"
          + (f"; backup at {backup.name}." if backup else " (new file)."), file=out)
    if not ns.keep_user_hooks:
        user = Path(ns.user_settings).expanduser() if ns.user_settings else user_settings_path()
        line = _drop_user_hooks(user, path, block, roots)
        if line:
            print(line, file=out)
    warning = None if ns.settings else _gitignore_warning(roots[0], path)
    if warning:
        print(warning, file=out)
    print(RESTART, file=out)
    return 0
