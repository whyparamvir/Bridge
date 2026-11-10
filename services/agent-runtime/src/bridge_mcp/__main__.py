"""`python -m bridge_mcp [serve]`: the MCP server Claude Code (or Codex) launches."""

from __future__ import annotations

import io
import logging
import os
import secrets
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, TextIO

from . import config

if TYPE_CHECKING:
    import httpx

# httpx, the client and the MCP server load only when needed: a hook that has nothing to do
# (outside its repositories, or PostToolUse within its minute) exits before importing them.

USAGE = ("usage: python -m bridge_mcp [serve [--config PATH] [--channel] | join <invite-url> --name NAME [--label LABEL] "
         "[--config PATH] | add-agent LABEL [--from PATH] [--config PATH] | hook [--event NAME] [--config PATH] "
         "[--repo PATH]... | autopilot [--config PATH] [--repo PATH]... [--write] [--settings PATH] [--codex] | "
         "responder [--config PATH] [--codex PATH] [--cd DIR] [--sandbox read-only|workspace-write]]")


def _config_arg(args: list[str]) -> tuple[str | None, list[str]]:
    """(the --config PATH value, the other arguments)."""
    rest, path, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a == "--config" and i + 1 < len(args):
            path, i = args[i + 1], i + 2
            continue
        if a.startswith("--config="):
            path = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return path, rest


def _hook_args(args: list[str]) -> tuple[str, list[str]]:
    """(the --event NAME value, "" for the plain UserPromptSubmit hook; every --repo PATH)."""
    event, repos, i = "", [], 0
    while i < len(args):
        a = args[i]
        for flag in ("--event", "--repo"):
            if a == flag and i + 1 < len(args):
                value, i = args[i + 1], i + 1
            elif a.startswith(flag + "="):
                value = a.split("=", 1)[1]
            else:
                continue
            if flag == "--event":
                event = value
            else:
                repos.append(value)
            break
        i += 1
    return event, repos


def _with_config(env: Mapping[str, str] | None, path: str | None) -> Mapping[str, str] | None:
    """--config PATH stands in for BRIDGE_CONFIG (and the default ~/.bridge/bridge.json),
    and that file wins over BRIDGE_URL, BRIDGE_TOKEN and BRIDGE_PROJECT: a setting meant for one
    agent never makes another agent's bridge use its key."""
    if not path:
        return env
    return config.explicit_env(env, path)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         env: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hook"]:
        event, repos = "", []
        try:
            path, rest = _config_arg(args[1:])
            env = _with_config(env, path)
            event, repos = _hook_args(rest)
        except Exception:  # noqa: BLE001 - a hook never fails a prompt
            pass
        if event in ("", "user-prompt-submit") and not repos:
            from . import hook
            return hook.run(env, transport, stdout or sys.stdout, stdin)
        from . import autopilot
        return autopilot.run_hook(event, env, transport, stdout or sys.stdout, stdin, repos)
    if args[:1] == ["autopilot"]:
        from . import autopilot
        return autopilot.cli(args[1:], stdout or sys.stdout, sys.stderr)
    if args[:1] == ["responder"]:
        from . import responder
        return responder.cli(args[1:], env, stdout or sys.stdout, sys.stderr)
    if args[:1] == ["join"]:
        return _join(args[1:], env, transport, stdout or sys.stdout)
    if args[:1] == ["add-agent"]:
        return _add_agent(args[1:], env, transport, stdout or sys.stdout)
    path, rest = _config_arg(args[1:] if args[:1] == ["serve"] else args)
    use_channel = "--channel" in rest
    rest = [a for a in rest if a != "--channel"]
    if rest or (path is not None and not path):
        print(USAGE, file=sys.stderr)
        return 2
    env = _with_config(env, path)
    from . import mcp
    from .client import BridgeClient
    from .tools import ToolSet

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("Bridge MCP: %(levelname)s %(message)s"))
    log = logging.getLogger("bridge_mcp")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        cfg = config.load(env)
        missing = cfg.missing()
        if missing:
            log.error("not set up: missing %s (in %s or BRIDGE_* variables)",
                      ", ".join(missing), config.default_path(env))
            return 1
        if stdin is None:
            stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
        if stdout is None:
            stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True)
        client = BridgeClient(cfg, transport=transport)
        tools = ToolSet(client, seat_key=secrets.token_urlsafe(12), label=cfg.seat_label)
        channel = None
        if use_channel:
            from .channel import NoteChannel

            channel = NoteChannel(client, notify=lambda method, params: None)  # serve() supplies the real one
        real_stdout, sys.stdout = sys.stdout, sys.stderr  # a stray print must not corrupt the protocol
        previous = _exit_on_signals()
        try:
            mcp.serve(stdin, stdout, tools, channel=channel)
        finally:
            _restore_signals(previous)
            sys.stdout = real_stdout
            client.close()
        return 0
    finally:
        log.removeHandler(handler)


def _stop(signum, frame) -> None:  # noqa: ANN001
    raise SystemExit(0)


def _exit_on_signals() -> dict:
    """Best effort: SIGTERM (and SIGBREAK on Windows) end serve() through its `finally`, so
    the seat is left and its claims released. Returns the handlers to put back."""
    import signal

    previous: dict = {}
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, _stop)
        except (ValueError, OSError):  # not the main thread, or not supported here
            pass
    return previous


def _restore_signals(previous: dict) -> None:
    import signal

    for sig, h in previous.items():
        try:
            signal.signal(sig, h)
        except (ValueError, OSError, TypeError):
            pass


def _join(args: list[str], env, transport, out) -> int:  # noqa: ANN001
    import argparse

    from . import join

    ap = argparse.ArgumentParser(prog="python -m bridge_mcp join")
    ap.add_argument("link")
    ap.add_argument("--name", help="your name")
    ap.add_argument("--agent", help="agent or model name, e.g. Opus 5.5")
    ap.add_argument("--label", default="")
    ap.add_argument("--config")
    try:
        ns = ap.parse_args(args)
        if ns.name is None and sys.stdin.isatty():
            print("Your name: ", file=sys.stderr, end="", flush=True)
            ns.name = input()
            if ns.agent is None:
                print("Agent or model name (optional): ", file=sys.stderr, end="", flush=True)
                ns.agent = input().strip() or None
        if not ns.name or not ns.name.strip():
            ap.error("--name is required when no name is entered")
        if ns.agent is not None and not join.LABEL.fullmatch(ns.agent.strip()):
            ap.error("--agent must be a short name of up to 40 characters")
    except SystemExit as e:
        return int(e.code or 0)
    except (EOFError, KeyboardInterrupt):
        return 1
    # Only an explicit --label is saved: without one, each Claude Code session is labelled by
    # the folder it runs in (config.load falls back to the bridge process's working folder).
    return join.run(ns.link, ns.name.strip(), ns.label.strip(), ns.config, env=env,
                    transport=transport, out=out, err=sys.stderr,
                    agent_label=ns.agent.strip() if ns.agent else None)


def _add_agent(args: list[str], env, transport, out) -> int:  # noqa: ANN001
    import argparse

    from . import join

    ap = argparse.ArgumentParser(
        prog="python -m bridge_mcp add-agent",
        description="Add another named agent for you (e.g. your Codex next to your Claude) and print its setup.")
    ap.add_argument("label", help='What sets the agent apart, e.g. "Codex".')
    ap.add_argument("--from", dest="source", help="Your existing bridge config (default: the usual one).")
    ap.add_argument("--config", help="Where to save the new agent's config (default: bridge-<label>.json next to it).")
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0)
    return join.add_agent(ns.label, ns.source, ns.config, env=env, transport=transport, out=out, err=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
