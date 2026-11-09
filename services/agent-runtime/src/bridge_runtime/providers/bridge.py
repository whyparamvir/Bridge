"""Adapter for device-local endpoints: submits a bounded text request to the worker
inference bridge and waits for the owning worker to complete it. Text only. The bridge
offers no tool calling, so device models never qualify as orchestrators."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import UUID

from bridge_control.contracts import api as A
from bridge_control.core import Principal
from bridge_control.services import Ctx, inference

from .base import (
    AdapterCapabilities, CancelToken, ErrorKind, GenerateRequest, GenerateResponse, ProviderError, Usage,
)


class WorkerBridgeAdapter:
    runtime = "worker_bridge"

    def __init__(self, ctx: Ctx, principal: Principal, project_id: UUID, endpoint_id: UUID, *,
                 poll_s: float = 0.5, wait_s: float = 300.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ctx, self.p, self.project_id, self.endpoint_id = ctx, principal, project_id, endpoint_id
        self.poll_s, self.wait_s, self.sleep = poll_s, wait_s, sleep

    def describe(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(tools=False, json_schema=False, vision=False, streaming=False,
                                   cancellation="between_calls")

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse:
        if req.tools or req.tool_choice:
            raise ProviderError(ErrorKind.invalid_request, "worker bridge is text-only; tool calling is not offered")
        prompt = "\n\n".join(f"{m.role}: {m.content}" if m.role != "user" else m.content for m in req.messages)
        with self.ctx.db.tx() as conn:
            r = inference.submit(conn, self.ctx.now(), self.p, self.project_id, A.CreateInferenceRequest(
                model_endpoint_id=self.endpoint_id, prompt=prompt[:16_000], max_tokens=min(req.max_tokens, 2048),
                deadline_seconds=int(min(3600, max(10, self.wait_s)))))
        waited = 0.0
        while True:
            if cancel is not None and cancel.cancelled:
                raise ProviderError(ErrorKind.cancelled, "cancelled while waiting for device")
            with self.ctx.db.tx() as conn:
                v = inference.get(conn, self.p, r.id)
            if v.state.value == "succeeded":
                return GenerateResponse(text=v.text or "", tool_calls=[],
                                        usage=Usage(v.input_tokens or 0, v.output_tokens or 0),
                                        provider_model_id=v.provider_model_id or req.model, finish_reason="stop",
                                        raw_id=str(v.id))
            if v.state.value in ("failed", "expired", "cancelled"):
                raise ProviderError(ErrorKind.unavailable, f"device inference {v.state.value}: {v.error or ''}")
            if waited >= self.wait_s:
                raise ProviderError(ErrorKind.timeout, "device did not answer in time")
            self.sleep(self.poll_s)
            waited += self.poll_s
