"""Shared primitives for every canonical Bridge contract model.

Conventions (proposed for joint review):
- IDs are UUIDs, generated server-side unless stated otherwise.
- Money is integer micro-units (1 USD = 1_000_000) to avoid float drift.
- `revision` is an optimistic-concurrency counter; mutations send `expected_revision`.
- All timestamps are timezone-aware UTC.
- Unknown fields are rejected so producers cannot smuggle unreviewed data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Generic, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

SCHEMA_VERSION = "1.0.0-proposed"


class Contract(BaseModel):
    """Base for all wire/canonical models."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, ser_json_timedelta="iso8601")


Id = UUID
Timestamp = AwareDatetime
Revision = Annotated[int, Field(ge=0)]
FencingToken = Annotated[int, Field(ge=1, description="Monotonic per lease; higher wins.")]
Micros = Annotated[int, Field(ge=0, description="Integer micro-units of `currency`.")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[a-f0-9]{64}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
LongText = Annotated[str, StringConstraints(max_length=20_000)]
IdempotencyKey = Annotated[str, StringConstraints(min_length=8, max_length=128)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Visibility(StrEnum):
    """Memory scopes from plan section 6."""

    private = "private"
    project = "project"
    task = "task"
    run = "run"


class ActorKind(StrEnum):
    human = "human"
    agent = "agent"
    worker = "worker"
    system = "system"  # deterministic server code (scheduler, lease reaper)


class ActorRef(Contract):
    kind: ActorKind
    id: Id | None = Field(None, description="Null only for kind=system.")


class Money(Contract):
    amount_micros: Micros
    currency: Currency = "USD"


class ResourceVector(Contract):
    """Device resources. One vector type for both inference and execution so a single
    scheduler accounts for both (section 20: no independent RAM/VRAM double-booking)."""

    cpu_millicores: Annotated[int, Field(ge=0)] = 0
    ram_mib: Annotated[int, Field(ge=0)] = 0
    vram_mib: Annotated[int, Field(ge=0)] = 0
    disk_mib: Annotated[int, Field(ge=0)] = 0

    def __add__(self, other: ResourceVector) -> ResourceVector:
        return ResourceVector(
            cpu_millicores=self.cpu_millicores + other.cpu_millicores,
            ram_mib=self.ram_mib + other.ram_mib,
            vram_mib=self.vram_mib + other.vram_mib,
            disk_mib=self.disk_mib + other.disk_mib,
        )

    def fits_within(self, limit: ResourceVector) -> bool:
        return (
            self.cpu_millicores <= limit.cpu_millicores
            and self.ram_mib <= limit.ram_mib
            and self.vram_mib <= limit.vram_mib
            and self.disk_mib <= limit.disk_mib
        )


T = TypeVar("T")

PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MAX = 200


class Page(Contract, Generic[T]):
    """Typed page. Query: ?cursor=<opaque>&limit=1..200 (default 50).
    `next_cursor` is null at the end. An empty `items` with a null cursor = no results.
    Cursors are opaque and server-ordered; clients must not parse or compare them."""

    items: list[T]
    next_cursor: str | None = None
