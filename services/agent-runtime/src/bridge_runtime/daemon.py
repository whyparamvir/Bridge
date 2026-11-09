"""Runtime process: maintenance tick + outbox dispatch (run.start, model.probe) + pickup
of runs whose runner lease lapsed (restart recovery).

    python -m bridge_runtime            # loop
    python -m bridge_runtime --once     # one pass (useful in scripts/tests)
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field

from bridge_control.core import Clock
from bridge_control.db.engine import Database, make_engine
from bridge_control.reliability import claim_outbox, complete_outbox, fail_outbox
from bridge_control.services import Ctx
from bridge_control.services.artifacts import BlobStore, LocalBlobStore
from bridge_control.services.maintenance import tick
from bridge_control.settings import Settings

from .executor import RunExecutor
from .probe import run_probe
from .providers.registry import AdapterRegistry

log = logging.getLogger("bridge.runtime")


@dataclass
class Runtime:
    ctx: Ctx
    registry: AdapterRegistry
    store: BlobStore
    name: str = "runtime"
    executor: RunExecutor = field(init=False)

    def __post_init__(self) -> None:
        self.executor = RunExecutor(self.ctx, self.registry, self.store, runner_id=self.name)

    def once(self) -> dict[str, int]:
        stats = {"runs": 0, "probes": 0}
        tick(self.ctx, self.name)
        with self.ctx.db.tx() as conn:
            items = claim_outbox(conn, self.ctx.now(), self.name, ["run.start", "model.probe"], limit=10)
        for item in items:
            try:
                if item.topic == "run.start":
                    self.executor.execute(item.subject_id)
                    stats["runs"] += 1
                else:
                    run_probe(self.ctx, self.registry, item.subject_id)
                    stats["probes"] += 1
                with self.ctx.db.tx() as conn:
                    complete_outbox(conn, self.ctx.now(), item.seq)
            except Exception as e:  # noqa: BLE001 - recorded on the outbox row, retried with backoff
                log.exception("outbox item %s failed", item.seq)
                with self.ctx.db.tx() as conn:
                    fail_outbox(conn, self.ctx.now(), item.seq, repr(e), item.attempts + 1)
        stats["runs"] += len(self.executor.run_pending())  # lapsed runner leases
        return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge_runtime")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    ctx = Ctx(db=Database(make_engine(settings.database_url)), clock=Clock(), settings=settings)
    rt = Runtime(ctx, AdapterRegistry.from_env(), LocalBlobStore(settings.artifact_dir))
    if settings.is_dev and not settings.live_provider_calls:
        log.warning("Live provider calls DISABLED; agents without a lead model use the deterministic fake provider.")
    while True:
        rt.once()
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
