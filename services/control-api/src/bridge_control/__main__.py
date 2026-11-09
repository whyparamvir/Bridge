"""Control-plane CLI.

    python -m bridge_control migrate            # alembic upgrade head (BRIDGE_DATABASE_URL)
    python -m bridge_control serve [--port N]   # uvicorn; binds 127.0.0.1 unless --host given
    python -m bridge_control maintain [--once]  # reapers + delegation retries
    python -m bridge_control check-config       # validate environment, print a summary
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .settings import ConfigError, Settings

REPO = Path(__file__).resolve().parents[4]


def _migrate(settings: Settings) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO / "migrations" / "alembic.ini"))
    cfg.cmd_opts = type("o", (), {"x": [f"url={settings.database_url}"]})()
    command.upgrade(cfg, "head")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge_control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    m = sub.add_parser("maintain")
    m.add_argument("--once", action="store_true")
    sub.add_parser("check-config")
    args = ap.parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "check-config":
        print(f"env={settings.env} database={'sqlite (dev fallback)' if settings.is_sqlite else 'postgresql'} "
              f"dev_auth={settings.dev_auth} live_provider_calls={settings.live_provider_calls}")
        return 0
    if args.cmd == "migrate":
        _migrate(settings)
        return 0
    if args.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
        return 0
    from .core import Clock
    from .db.engine import Database, make_engine
    from .services import Ctx
    from .services.maintenance import tick

    ctx = Ctx(db=Database(make_engine(settings.database_url)), clock=Clock(), settings=settings)
    while True:
        print(tick(ctx))
        if args.once:
            return 0
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
