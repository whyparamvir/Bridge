"""`python -m bridge_mcp join <invite-url> --name NAME`: turn an invite link into saved bridge
settings and print the two things to paste. It never edits Claude Code's settings."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, urlsplit

import httpx

from . import config
from .client import BridgeClient, BridgeError, BridgeOffline


def parse_invite(link: str) -> tuple[str, str]:
    """(server url, token) from f"{public_base_url}/#join={token}"."""
    parts = urlsplit(link.strip())
    token = (parse_qs(parts.fragment).get("join") or [""])[0]
    if not parts.scheme or not parts.netloc or not token:
        raise ValueError("That doesn't look like a Bridge invite link. Ask for a new link.")
    return f"{parts.scheme}://{parts.netloc}", token


def quoted(path: str | os.PathLike) -> str:
    """A path for a printed command: always double-quoted, and with forward slashes on
    Windows, where Claude Code runs hook commands through Git Bash (which would eat
    unquoted backslashes). Windows programs accept forward slashes too."""
    s = Path(path).as_posix() if os.name == "nt" else str(path)
    return f'"{s}"'


def launcher() -> Path | None:
    """services/agent-runtime/bin/bridge_mcp.py when the bridge runs from the source tree;
    None when it is installed as a package."""
    p = Path(__file__).resolve().parents[2] / "bin" / "bridge_mcp.py"
    return p if p.is_file() else None


def bridge_command(sub: str) -> str:
    """The command that runs `bridge_mcp <sub>` on this computer exactly as typed, from any
    folder and without PYTHONPATH."""
    lp = launcher()
    target = quoted(lp) if lp else "-m bridge_mcp"
    return f"{quoted(sys.executable)} {target} {sub}"


def mcp_add_line() -> str:
    """User scope: run it once and the bridge is there in every project folder."""
    return f"claude mcp add --scope user bridge -- {bridge_command('serve')}"


def codex_add_line() -> str:
    return f"codex mcp add bridge -- {bridge_command('serve')}"


def join_line(invite_url: str) -> str:
    """What a teammate runs: from the Bridge folder before Bridge is installed."""
    how = "services/agent-runtime/bin/bridge_mcp.py" if launcher() else "-m bridge_mcp"
    return f'python {how} join "{invite_url}" --name "Your Name"'


def hook_snippet(cfg_path: str | os.PathLike | None = None) -> dict:
    """The Claude Code settings snippet that runs the Bridge hook on every prompt; with
    `cfg_path`, for the agent whose bridge config is there."""
    sub = f"hook --config {quoted(cfg_path)}" if cfg_path else "hook"
    return {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": bridge_command(sub)}]}]}}


def autopilot_advice() -> str:
    """Pointer to the repo-scoped hooks; the plain snippet is for setups without autopilot."""
    return (f"Recommended for Claude Code: in your team's repository run {bridge_command('autopilot --write')} "
            "to install all Bridge hooks for that repository. The snippet above is the simpler option "
            "(updates on every prompt, in every folder).")


LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


def label_slug(label: str) -> str:
    """"My Codex 2" -> "my-codex-2", for file and MCP server names."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "agent"


def add_agent(label: str, source: str | None, target: str | None, *, env: Mapping[str, str] | None,
              transport: httpx.BaseTransport | None, out: TextIO, err: TextIO) -> int:
    """`add-agent LABEL`: from this person's bridge config, ask Bridge for another named agent
    ("Seda's Codex"), save its own config and print how to plug it into Claude Code or Codex."""
    label = label.strip()
    if not LABEL.match(label):
        print("Use a short name: letters, digits, spaces, '.', '_' or '-', up to 40 characters.", file=err)
        return 1
    src = Path(source).expanduser().absolute() if source else config.default_path(env)
    cfg = config.load(config.explicit_env(env, src) if source else env)
    if not src.is_file() or cfg.missing():
        print(f"Bridge isn't set up in {src} yet. Join first, or pass --from with your bridge config.", file=err)
        return 1
    slug = label_slug(label)
    path = Path(target).expanduser().absolute() if target else src.parent / f"bridge-{slug}.json"
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(src)):
        print("That is your existing bridge config. Pass another --config path.", file=err)
        return 1
    client = BridgeClient(cfg, transport=transport)
    try:
        s = client.create_sibling(label)
    except BridgeOffline as e:
        print(f"Can't reach Bridge at {e.host}. Check that Bridge is running and your connection.", file=err)
        return 1
    except BridgeError as e:
        detail = e.detail
        if e.status == 401 and not detail.startswith("Your sign-in expired"):
            detail = "Bridge doesn't accept this bridge's key. Ask the host for a new invite link and join again."
        print(" ".join(str(detail).split()), file=err)
        return 1
    finally:
        client.close()
    config.save(config.BridgeConfig(url=cfg.url, token=s["agent_token"], project_id=cfg.project_id,
                                    person_name=cfg.person_name, agent_id=str(s["agent_id"])), path)
    serve = bridge_command(f"serve --config {quoted(path)}")
    print(f"Added {s['name']}.", file=out)
    print(f"Claude Code: claude mcp add --scope user bridge-{slug} -- {serve}", file=out)
    print(f"Codex: codex mcp add bridge -- {serve}", file=out)
    print(json.dumps(hook_snippet(path), indent=2), file=out)
    return 0


def run(link: str, name: str, label: str, path: str | None, *, env: Mapping[str, str] | None,
        transport: httpx.BaseTransport | None, out: TextIO, err: TextIO,
        agent_label: str | None = None) -> int:
    try:
        url, token = parse_invite(link)
    except ValueError as e:
        print(str(e), file=err)
        return 1
    client = BridgeClient(config.BridgeConfig(url=url, token="", project_id=""), transport=transport)
    try:
        s = client.redeem_invite(token, name, agent_label)
    except BridgeOffline as e:
        print(f"Can't reach Bridge at {e.host}. Check the link and your connection.", file=err)
        return 1
    except BridgeError as e:
        print(e.detail, file=err)
        return 1
    finally:
        client.close()
    if not s.get("agent_token") or not s.get("project_id"):
        print("This invite doesn't give access to a project. Ask for a new link.", file=err)
        return 1
    cfg = config.BridgeConfig(url=url, token=s["agent_token"], project_id=s["project_id"], person_name=name,
                              seat_label=label, agent_id=str(s.get("agent_id") or ""))
    config.save(cfg, path or config.default_path(env))
    print(f"Joined {s['workspace']['name']} as {name}.", file=out)
    print(mcp_add_line(), file=out)
    print(codex_add_line(), file=out)
    print(json.dumps(hook_snippet(), indent=2), file=out)
    print(autopilot_advice(), file=out)
    return 0
