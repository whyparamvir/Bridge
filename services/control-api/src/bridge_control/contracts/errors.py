"""Error contract: RFC 9457 problem details with stable machine codes.

Non-members receive `not_found` (never `permission_denied`) for project-scoped
objects, so existence is not leaked across tenants.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Contract, Id


class ErrorCode(StrEnum):
    validation_error = "validation_error"  # 422
    unauthenticated = "unauthenticated"  # 401
    not_found = "not_found"  # 404 — also returned to non-members
    permission_denied = "permission_denied"  # 403 — member lacking role
    policy_denied = "policy_denied"  # 403 — sharing/payer/project policy forbids
    revision_conflict = "revision_conflict"  # 409 — expected_revision mismatch
    invalid_transition = "invalid_transition"  # 409 — state machine rejects
    lease_conflict = "lease_conflict"  # 409 — already leased by someone else
    stale_attempt = "stale_attempt"  # 409 — fencing token superseded; result discarded
    idempotency_conflict = "idempotency_conflict"  # 409 — same key, different body
    capacity_unavailable = "capacity_unavailable"  # 409 — reservation would over-commit
    budget_exhausted = "budget_exhausted"  # 402
    delegation_bounds_exceeded = "delegation_bounds_exceeded"  # 422 — widening attempt
    assignment_token_invalid = "assignment_token_invalid"  # 403
    provider_unavailable = "provider_unavailable"  # 503
    rate_limited = "rate_limited"  # 429
    cursor_expired = "cursor_expired"  # 410 — event retention passed; resync from snapshot


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.validation_error: 422,
    ErrorCode.unauthenticated: 401,
    ErrorCode.not_found: 404,
    ErrorCode.permission_denied: 403,
    ErrorCode.policy_denied: 403,
    ErrorCode.revision_conflict: 409,
    ErrorCode.invalid_transition: 409,
    ErrorCode.lease_conflict: 409,
    ErrorCode.stale_attempt: 409,
    ErrorCode.idempotency_conflict: 409,
    ErrorCode.capacity_unavailable: 409,
    ErrorCode.budget_exhausted: 402,
    ErrorCode.delegation_bounds_exceeded: 422,
    ErrorCode.assignment_token_invalid: 403,
    ErrorCode.provider_unavailable: 503,
    ErrorCode.rate_limited: 429,
    ErrorCode.cursor_expired: 410,
}


class Problem(Contract):
    type: str = Field(description="URI: https://bridge.invalid/errors/{code}")
    title: str
    status: int
    code: ErrorCode
    detail: str | None = None
    instance: str | None = None
    entity_id: Id | None = None
    current_revision: int | None = Field(None, description="Set on revision_conflict.")
    retryable: bool = False

    @classmethod
    def of(cls, code: ErrorCode, detail: str | None = None, **kw: object) -> Problem:
        return cls(
            type=f"https://bridge.invalid/errors/{code.value}",
            title=code.value.replace("_", " ").capitalize(),
            status=HTTP_STATUS[code],
            code=code,
            detail=detail,
            **kw,  # type: ignore[arg-type]
        )


class DomainError(Exception):
    """Raised by deterministic domain code; the API layer maps it to a Problem."""

    def __init__(self, code: ErrorCode, detail: str = "", **extra: object) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.extra = extra

    def to_problem(self) -> Problem:
        return Problem.of(self.code, self.detail or None, **self.extra)
