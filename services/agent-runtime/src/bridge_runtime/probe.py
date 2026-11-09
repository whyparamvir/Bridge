"""Capability probes: two bounded calls (<=64 output tokens each) that test real tool
calling and schema adherence. OpenAI-compatible syntax is not evidence; only a correct
tool call with exactly-shaped arguments passes. Device endpoints cannot be probed until
the worker inference bridge exists, and are recorded as such (not as passing)."""

from __future__ import annotations

from uuid import UUID

from bridge_control.contracts.compute import Capability, EndpointHealth, ExecutionLocation, ProbeResult
from bridge_control.services import Ctx, models

from .providers.base import ChatMessage, GenerateRequest, ProviderError, ToolSpec
from .providers.registry import AdapterRegistry

ECHO = ToolSpec("probe_echo", "Call with value=42.", {
    "type": "object", "additionalProperties": False, "required": ["value"],
    "properties": {"value": {"type": "integer"}}})
SCHEMA = ToolSpec("probe_schema", "Call with name='bridge', tags=['a','b'], nested.ok=true.", {
    "type": "object", "additionalProperties": False, "required": ["name", "tags", "nested"],
    "properties": {"name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                   "nested": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}}})


def run_probe(ctx: Ctx, registry: AdapterRegistry, endpoint_id: UUID) -> list[ProbeResult]:
    with ctx.db.tx() as conn:
        row = models.endpoint_row(conn, endpoint_id)
        ep = models.endpoint_contract(row)
        c = models.connection_row(conn, ep.connection_id) if ep.connection_id else None
    now = ctx.now()
    if ep.execution_location is ExecutionLocation.device:
        results = [ProbeResult(capability=cap, passed=False, probed_at=now,
                               detail="device models run over the text-only worker bridge; tool calling cannot be offered")
                   for cap in (Capability.tool_calling, Capability.json_schema_output)]
        with ctx.db.tx() as conn:
            models.record_probe(conn, now, endpoint_id, results, EndpointHealth.unknown)
        return results
    results: list[ProbeResult] = []
    health = EndpointHealth.healthy
    try:
        adapter = registry.build(ep.runtime.value, base_url=c.base_url if c else None, secret_ref=c.secret_ref if c else None)
        for cap, tool, expected in ((Capability.tool_calling, ECHO, {"value": 42}),
                                    (Capability.json_schema_output, SCHEMA,
                                     {"name": "bridge", "tags": ["a", "b"], "nested": {"ok": True}})):
            resp = adapter.generate(GenerateRequest(model=ep.model_id, messages=[
                ChatMessage("user", f"Call the tool {tool.name} exactly as its description says.")],
                tools=[tool], tool_choice=tool.name, max_tokens=64, timeout_s=30))
            got = [tc.arguments for tc in resp.tool_calls if tc.name == tool.name]
            ok = bool(got) and got[0] == expected
            results.append(ProbeResult(capability=cap, passed=ok, probed_at=ctx.now(),
                                       detail=f"model={resp.provider_model_id}"[:200]))
    except ProviderError as e:
        health = EndpointHealth.unavailable if e.kind.value in ("unavailable", "timeout", "auth") else EndpointHealth.degraded
        results = [ProbeResult(capability=cap, passed=False, probed_at=ctx.now(), detail=f"{e.kind.value}"[:200])
                   for cap in (Capability.tool_calling, Capability.json_schema_output)]
    if results and not all(r.passed for r in results) and health is EndpointHealth.healthy:
        health = EndpointHealth.degraded
    with ctx.db.tx() as conn:
        models.record_probe(conn, ctx.now(), endpoint_id, results, health)
    return results
