"""`python -m bridge_mcp [serve]`: the MCP server Claude Code (or Codex) launches."""

from __future__ import annotations

import io
import logging
import os
import secrets
import sys
from collections.abc import Mapping
from typing import TextIO

import httpx

from . import config, mcp
from .client import BridgeClient
from .tools import ToolSet

USAGE = ("usage: python -m bridge_mcp [serve [--config PATH] | join <invite-url> --name NAME [--label LABEL] "
         "[--config PATH] | add-agent LABEL [--from PATH] [--config PATH] | hook [--config PATH]]")


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
        from . import hook
        try:
            env = _with_config(env, _config_arg(args[1:])[0])
        except Exception:  # noqa: BLE001 - a hook never fails a prompt
            pass
        return hook.run(env, transport, stdout or sys.stdout, stdin)
    if args[:1] == ["join"]:
        return _join(args[1:], env, transport, stdout or sys.stdout)
    if args[:1] == ["add-agent"]:
        return _add_agent(args[1:], env, transport, stdout or sys.stdout)
    path, rest = _config_arg(args[1:] if args[:1] == ["serve"] else args)
    if rest or (path is not None and not path):
        print(USAGE, file=sys.stderr)
        return 2
    env = _with_config(env, path)
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
        real_stdout, sys.stdout = sys.stdout, sys.stderr  # a stray print must not corrupt the protocol
        previous = _exit_on_signals()
        try:
            mcp.serve(stdin, stdout, tools)
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
    ap.add_argument("--name", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--config")
    try:
        ns = ap.parse_args(args)
    except SystemExit as e:
        return int(e.code or 0)
    # Only an explicit --label is saved: without one, each Claude Code session is labelled by
    # the folder it runs in (config.load falls back to the bridge process's working folder).
    return join.run(ns.link, ns.name.strip(), ns.label.strip(), ns.config, env=env,
                    transport=transport, out=out, err=sys.stderr)


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
