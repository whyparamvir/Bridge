"""Engine factory. SQLite gets WAL, foreign keys, a busy timeout and BEGIN IMMEDIATE so
concurrent writers serialize instead of failing on lock upgrade."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine


def make_engine(url: str, *, echo: bool = False) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = sa.create_engine(
            url, echo=echo, connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_conn, _rec):  # noqa: ANN001, ANN202
            dbapi_conn.isolation_level = None  # let SQLAlchemy emit BEGIN itself
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # noqa: ANN001, ANN202
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return sa.create_engine(url, echo=echo, pool_pre_ping=True, future=True)


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """One transaction. Commits on success, rolls back on any exception."""
        with self.engine.begin() as conn:
            yield conn
