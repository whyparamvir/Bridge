"""Provider adapter contract (plan section 5): describe capabilities, generate normalized
responses + usage, normalize tool calls and errors, honour deadlines/cancellation, and
record the exact provider/model identifier returned."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


class ErrorKind(StrEnum):
    timeout = "timeout"
    rate_limited = "rate_limited"
    unavailable = "unavailable"
    auth = "auth"
    invalid_request = "invalid_request"
    invalid_tool_arguments = "invalid_tool_arguments"
    disabled = "disabled"  # live calls not authorized in this deployment
    cancelled = "cancelled"
    content_filtered = "content_filtered"


RETRYABLE = {ErrorKind.timeout, ErrorKind.rate_limited, ErrorKind.unavailable, ErrorKind.invalid_tool_arguments}


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


class CancelToken:
    def __init__(self) -> None:
        self._e = threading.Event()

    def cancel(self) -> None:
        self._e.set()

    @property
    def cancelled(self) -> bool:
        return self._e.is_set()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: str | None = None  # force a named tool
    max_tokens: int = 1024
    temperature: float = 0.0
    timeout_s: float = 60.0


@dataclass(frozen=True)
class GenerateResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    provider_model_id: str  # exact identifier the provider reported
    finish_reason: str
    raw_id: str | None = None


@dataclass(frozen=True)
class AdapterCapabilities:
    tools: bool
    json_schema: bool
    vision: bool
    streaming: bool
    cancellation: Literal["none", "between_calls", "in_flight"]
    context_limit_tokens: int | None = None


class ProviderAdapter(Protocol):
    runtime: str

    def describe(self, model: str) -> AdapterCapabilities: ...

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse: ...
