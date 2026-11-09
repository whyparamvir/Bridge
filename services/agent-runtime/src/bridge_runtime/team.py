"""`python -m bridge_runtime.team start --name TEAM`: the one command the host runs. It sets up
the data folder and database, creates the team, writes the host's own bridge config, makes an
invite link, prints what to send teammates and serves the API."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

PRIVATE_WARNING = "Anyone on this network can read Bridge traffic. Use Tailscale or a network you trust."
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class StartError(RuntimeError):
    """A problem the host can fix; printed as one line, no traceback."""


@dataclass(frozen=True)
class HostSetup:
    team_name: str
    invite_url: str
    bridge_config_path: Path
    note: str | None = None


def prepare(settings) -> None:  # noqa: ANN001
    """Create the data folder and bring the database to the latest schema."""
    from bridge_control.__main__ import _migrate

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _migrate(settings)


def own_url(bind: str, port: int) -> str:
    """Where the host's own bridge reaches the server: 127.0.0.1 when the server listens on
    every interface or on localhost, otherwise the one address it was bound to (an IPv6
    literal in brackets)."""
    host = (bind or "").strip().strip("[]")
    if host.lower() in ("", "0.0.0.0", "::", "localhost", "127.0.0.1"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def host_setup(settings, team_name: str | None, host_name: str, *, bridge_path=None,  # noqa: ANN001
               port: int = 8787, bind: str = "127.0.0.1") -> HostSetup:
    """Create the team once, give the host's own computer an agent credential (kept on
    later starts while it still works) and make a fresh invite."""
    import sqlalchemy as sa
    from bridge_control.contracts.team import CreateInvite
    from bridge_control.core import Clock, Principal
    from bridge_control.db import tables as t
    from bridge_control.db.engine import Database, make_engine
    from bridge_control.services import local, workspace
    from bridge_mcp import config

    path = Path(bridge_path) if bridge_path else config.default_path()
    bridge_url = own_url(bind, port)
    clock = Clock()
    db = Database(make_engine(settings.database_url))
    note = None
    try:
        with db.tx() as conn:
            inst = local.installation(conn)
            if inst is None:
                if not team_name:
                    raise StartError("First start: give your team a name with --name.")
                inst = local.bootstrap(conn, clock.now(), settings, team_name, host_name)
                name = team_name
            else:
                name = conn.execute(sa.select(t.workspaces.c.name).where(
                    t.workspaces.c.id == inst.workspace_id)).scalar_one()
                if team_name and team_name != name:
                    note = f'This computer already hosts "{name}"; ignoring --name "{team_name}".'
            owner = Principal(kind="user", user_id=inst.owner_user_id)
            agent_id = local.ensure_person_agent(conn, clock.now(), inst.owner_user_id, inst.workspace_id)
            kept = _kept_config(conn, clock, path, bridge_url, inst, agent_id, config)
            if kept is None:
                cred = workspace.issue_agent_credential(conn, clock.now(), owner, agent_id,
                                                        timedelta(hours=settings.session_ttl_hours))
                # No seat_label: each of the host's sessions is labelled by its own folder.
                config.save(config.BridgeConfig(url=bridge_url, token=cred.token, project_id=str(inst.project_id),
                                                person_name=host_name, agent_id=str(agent_id)), path)
            elif _saved_label(path) == "Host":  # written by earlier versions; drop it
                config.save(config.BridgeConfig(url=kept.url, token=kept.token, project_id=kept.project_id,
                                                person_name=kept.person_name, agent_id=kept.agent_id), path)
            invite = workspace.create_workspace_invite(conn, clock.now(), owner, inst.workspace_id,
                                                       CreateInvite(), settings)
    finally:
        db.engine.dispose()
    return HostSetup(name, invite.url, path, note)


def _saved_label(path) -> str:  # noqa: ANN001
    import json

    try:
        return str(json.loads(Path(path).read_text("utf-8")).get("seat_label") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _kept_config(conn, clock, path, bridge_url, inst, agent_id, config):  # noqa: ANN001, ANN202
    """The host's saved bridge config when its token still authenticates as this agent."""
    from bridge_control.services import workspace

    if not Path(path).is_file():
        return None
    try:
        cfg = config.load({"BRIDGE_CONFIG": str(path)})
    except (OSError, ValueError):
        return None
    if cfg.missing() or cfg.url != bridge_url or cfg.project_id != str(inst.project_id):
        return None
    try:
        p = workspace.authenticate(conn, clock.now(), cfg.token)
    except Exception:  # noqa: BLE001 - expired or unknown: issue a new one
        return None
    if p is None or p.kind != "agent" or p.agent_id != agent_id:
        return None
    return cfg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="bridge_runtime.team")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="start the team server and print the invite")
    s.add_argument("--name", default=None, help="team name (required the first time)")
    s.add_argument("--host-name", default=None, help="your name (default: your computer user name)")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--private-network", action="store_true")
    s.add_argument("--public-url", default=None)
    return ap.parse_args(argv)


def check_env(environ) -> None:  # noqa: ANN001
    """`team start` is a local team server only: refuse any other BRIDGE_ENV."""
    value = (environ.get("BRIDGE_ENV") or "").strip()
    if value and value.lower() != "local":
        raise StartError(f"team start only runs a local team server, but BRIDGE_ENV is set to {value}. "
                         "Unset BRIDGE_ENV (or set it to local) and try again.")


def settings_for(args):  # noqa: ANN001, ANN201
    """Settings for `start`; raises ConfigError for an unsafe bind."""
    from bridge_control.settings import Settings

    env = dict(os.environ)
    check_env(env)
    env["BRIDGE_ENV"] = "local"
    if args.bind.strip().lower() not in LOOPBACK and not (args.public_url or env.get("BRIDGE_PUBLIC_BASE_URL")):
        from bridge_control.settings import ConfigError

        raise ConfigError("Add --public-url with the address teammates use to reach this computer, "
                          "for example http://100.101.1.2:8787.")
    env["BRIDGE_BIND_HOST"] = args.bind
    if args.private_network:
        env["BRIDGE_PRIVATE_NETWORK"] = "1"
    if args.public_url:
        env["BRIDGE_PUBLIC_BASE_URL"] = args.public_url.rstrip("/")
    elif not env.get("BRIDGE_PUBLIC_BASE_URL"):
        env["BRIDGE_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{args.port}"
    return Settings.from_env(env)


def render(settings, hs: HostSetup, *, loopback_only: bool = False) -> str:  # noqa: ANN001
    import json

    from bridge_mcp.join import codex_add_line, hook_snippet, join_line, launcher, mcp_add_line

    lines = [f"Bridge team: {hs.team_name}"]
    if hs.note:
        lines.append(hs.note)
    if settings.private_network:
        lines.append(PRIVATE_WARNING)
    if loopback_only:
        lines.append("Teammates on other computers can't reach this: restart with --private-network "
                     "--bind 0.0.0.0 --public-url http://<your address>:<port>.")
    lines += ["", "Invite link:", f"  {hs.invite_url}", "",
              "Send teammates this to run on their computer" + (", from the repository root:" if launcher() else ":"),
              f"  {join_line(hs.invite_url)}", "",
              "Connect your own Codex:",
              f"  {codex_add_line()}", "",
              "Connect your own Claude Code:",
              f"  {mcp_add_line()}", "",
              "Add this to your Claude Code settings so you see teammates' updates:",
              json.dumps(hook_snippet(), indent=2), "",
              "Press Ctrl+C to stop."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        check_env(os.environ)
    except StartError as e:
        print(str(e), file=sys.stderr)
        return 2
    os.environ["BRIDGE_ENV"] = "local"
    from bridge_control.settings import ConfigError

    try:
        settings = settings_for(args)
        prepare(settings)
        host_name = args.host_name or os.environ.get("USERNAME") or os.environ.get("USER") or "Host"
        hs = host_setup(settings, args.name, host_name, port=args.port, bind=args.bind)
    except (ConfigError, StartError) as e:
        print(str(e), file=sys.stderr)
        return 2
    loop = (args.bind.strip().lower() in LOOPBACK and not args.public_url
            and not os.environ.get("BRIDGE_PUBLIC_BASE_URL"))
    print(render(settings, hs, loopback_only=loop), flush=True)
    import uvicorn

    from bridge_control.api.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(settings), host=args.bind, port=args.port,
                                           log_level="warning"))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
