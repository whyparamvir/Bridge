"""Application services: the only code that changes durable state. API handlers and the
agent runtime call these; models never write rows directly (plan section 4)."""

from __future__ import annotations

from dataclasses import dataclass

from ..core import Clock
from ..db.engine import Database
from ..settings import Settings


@dataclass
class Ctx:
    db: Database
    clock: Clock
    settings: Settings

    def now(self):  # noqa: ANN201
        return self.clock.now()
