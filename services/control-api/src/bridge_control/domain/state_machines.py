"""Server-owned state machines. Models propose; this code decides (plan section 4).

Each rule names who may perform the transition and whether the caller must present
the task's current lease fencing token.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from ..contracts.common import ActorKind
from ..contracts.compute import DelegationState as D
from ..contracts.compute import JobAttemptState as A
from ..contracts.compute import JobState as J
from ..contracts.errors import DomainError, ErrorCode
from ..contracts.work import RunStatus as R
from ..contracts.work import TaskState as T

S = TypeVar("S", bound=StrEnum)

H, AG, W, SYS = ActorKind.human, ActorKind.agent, ActorKind.worker, ActorKind.system


@dataclass(frozen=True)
class Rule(Generic[S]):
    src: S
    dst: S
    actors: frozenset[ActorKind]
    requires_lease: bool = False


class StateMachine(Generic[S]):
    def __init__(self, name: str, rules: list[Rule[S]], terminal: set[S]) -> None:
        self.name = name
        self.terminal = frozenset(terminal)
        self._rules = {(r.src, r.dst): r for r in rules}
        for r in rules:
            if r.src in self.terminal:
                raise ValueError(f"{name}: terminal state {r.src} has outgoing rule")

    def rule(self, src: S, dst: S) -> Rule[S] | None:
        return self._rules.get((src, dst))

    def targets(self, src: S) -> set[S]:
        return {d for (s, d) in self._rules if s == src}

    def check(self, src: S, dst: S, actor: ActorKind, *, lease_ok: bool = False) -> Rule[S]:
        """Raise DomainError(invalid_transition / stale_attempt) unless permitted.

        `lease_ok` is computed by the caller from a fencing-token comparison
        (see domain.leases.verify_fence); this function only enforces that one was required.
        """
        r = self.rule(src, dst)
        if r is None:
            raise DomainError(ErrorCode.invalid_transition, f"{self.name}: {src} -> {dst}")
        if actor not in r.actors:
            raise DomainError(
                ErrorCode.invalid_transition, f"{self.name}: {actor} may not {src} -> {dst}"
            )
        if r.requires_lease and not lease_ok:
            raise DomainError(ErrorCode.stale_attempt, f"{self.name}: {src} -> {dst} needs lease")
        return r


def _r(src, dst, actors, lease=False):  # noqa: ANN001, ANN202 - compact table helper
    return Rule(src, dst, frozenset(actors), lease)


# Task (section 6): backlog -> ready -> leased -> running -> verifying -> review -> done
TASK = StateMachine[T](
    "task",
    [
        _r(T.backlog, T.ready, {H, AG, SYS}),  # SYS: dependencies satisfied
        _r(T.ready, T.backlog, {H}),
        _r(T.ready, T.leased, {SYS}),  # only via atomic claim
        _r(T.leased, T.running, {AG, W}, lease=True),
        _r(T.leased, T.ready, {SYS}),  # lease expired / released
        _r(T.running, T.verifying, {AG, W}, lease=True),
        _r(T.running, T.ready, {SYS}),  # attempt failed (retryable) or lease expired
        _r(T.verifying, T.review, {AG, SYS}, lease=True),
        _r(T.verifying, T.ready, {AG, SYS}),  # verification failed -> rework
        _r(T.review, T.done, {H}),  # pilot: humans accept
        _r(T.review, T.ready, {H}),  # changes requested
        # exceptional
        *[_r(s, T.blocked, {AG, W, SYS}) for s in (T.ready, T.leased, T.running, T.verifying)],
        _r(T.blocked, T.ready, {H, SYS}),
        *[_r(s, T.failed, {SYS}) for s in (T.running, T.verifying, T.blocked)],  # retries spent
        _r(T.failed, T.ready, {H}),  # explicit human reopen
        *[
            _r(s, T.cancelled, {H, SYS})
            for s in (T.backlog, T.ready, T.leased, T.running, T.verifying, T.review, T.blocked)
        ],
    ],
    terminal={T.done, T.cancelled},
)

RUN = StateMachine[R](
    "run",
    [
        _r(R.queued, R.working, {SYS}),
        _r(R.working, R.blocked, {SYS}),
        _r(R.blocked, R.working, {H, SYS}),
        _r(R.working, R.needs_review, {SYS}),
        _r(R.needs_review, R.complete, {H}),
        _r(R.needs_review, R.working, {H}),
        _r(R.working, R.failed, {SYS}),
        _r(R.blocked, R.failed, {SYS}),
        *[_r(s, R.cancelled, {H, SYS}) for s in (R.queued, R.working, R.blocked, R.needs_review)],
    ],
    terminal={R.complete, R.failed, R.cancelled},
)

JOB = StateMachine[J](
    "job",
    [
        _r(J.queued, J.reserved, {SYS}),
        _r(J.reserved, J.running, {W}, lease=True),
        _r(J.reserved, J.queued, {SYS}),  # reservation/lease expired
        _r(J.running, J.succeeded, {W}, lease=True),
        _r(J.running, J.failed, {W, SYS}),
        _r(J.running, J.queued, {SYS}),  # retry of idempotent job after fence
        *[_r(s, J.cancelled, {H, SYS}) for s in (J.queued, J.reserved, J.running)],
    ],
    terminal={J.succeeded, J.failed, J.cancelled},
)

JOB_ATTEMPT = StateMachine[A](
    "job_attempt",
    [
        _r(A.assigned, A.running, {W}, lease=True),
        _r(A.running, A.succeeded, {W}, lease=True),
        _r(A.running, A.failed, {W, SYS}),
        *[_r(s, A.fenced, {SYS}) for s in (A.assigned, A.running)],
        *[_r(s, A.cancelled, {H, SYS}) for s in (A.assigned, A.running)],
    ],
    terminal={A.succeeded, A.failed, A.fenced, A.cancelled},
)

DELEGATION = StateMachine[D](
    "delegation",
    [
        _r(D.requested, D.assigned, {SYS}),  # coordinator reserved a device atomically
        _r(D.assigned, D.accepted, {AG}, lease=True),  # recipient-bound token
        _r(D.assigned, D.declined, {SYS}),  # recipient declined and no alternates remain
        _r(D.assigned, D.requested, {SYS}),  # alternate scheduling after decline/expiry
        _r(D.accepted, D.running, {AG, W}, lease=True),
        _r(D.accepted, D.requested, {SYS}),  # lease expired before start; reassign
        _r(D.running, D.completed, {AG, W}, lease=True),
        _r(D.running, D.failed, {AG, W, SYS}),
        _r(D.running, D.requested, {SYS}),  # lease expired; idempotent retry
        _r(D.completed, D.verified, {AG, SYS}),  # requester's orchestrator verified result
        _r(D.completed, D.rejected_result, {AG, SYS}),
        *[_r(s, D.expired, {SYS}) for s in (D.requested, D.assigned)],
        *[
            _r(s, D.cancelled, {H, AG, SYS})
            for s in (D.requested, D.assigned, D.accepted, D.running)
        ],
    ],
    terminal={D.verified, D.rejected_result, D.declined, D.expired, D.cancelled, D.failed},
)

MACHINES = {m.name: m for m in (TASK, RUN, JOB, JOB_ATTEMPT, DELEGATION)}
