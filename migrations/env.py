"""Alembic environment. URL precedence: config attribute 'connection' (tests) >
`-x url=...` > BRIDGE_DATABASE_URL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "control-api" / "src"))

from alembic import context

from bridge_control.db.engine import make_engine
from bridge_control.db.tables import metadata

config = context.config


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    url = x.get("url") or os.environ.get("BRIDGE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("set BRIDGE_DATABASE_URL or pass -x url=...")
    return url


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        context.configure(connection=conn, target_metadata=metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url())
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # Batch mode rebuilds SQLite tables (copy, DROP, rename); with foreign keys on,
            # dropping a referenced table fails. The pragma only works outside a
            # transaction, so set it on the raw connection; integrity is checked below.
            connection.connection.driver_connection.execute("PRAGMA foreign_keys=OFF")
        # Alembic treats SQLite DDL as non-transactional (each migration commits alone).
        # SQLite DDL is transactional, so run the whole upgrade in ONE transaction: a
        # failed integrity check below then rolls back every migration in this run,
        # including the alembic_version bump.
        context.configure(connection=connection, target_metadata=metadata, render_as_batch=True,
                          **({"transactional_ddl": True} if sqlite else {}))
        try:
            with context.begin_transaction():
                context.run_migrations()
                if sqlite:
                    bad = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    if bad:
                        raise RuntimeError(f"migration left foreign key violations: {bad[:5]}")
        finally:
            if sqlite:
                connection.connection.driver_connection.execute("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    run_offline()
else:
    run_online()
