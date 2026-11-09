"""Deterministic fake provider for orchestration and failure tests. It never touches the
network. Output is a pure function of the request, so runs are reproducible; failures
are injected from an explicit script."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .base import (
    AdapterCapabilities, CancelToken, ErrorKind, GenerateRequest, GenerateResponse, ProviderError, ToolCall, Usage,
)

Hook = Callable[[GenerateRequest, int], None]


@dataclass
class FakeProvider:
    """Behaviour by forced tool:
      propose_plan -> splits the goal on ';' / ' and then ' into dependent tasks
      submit_work  -> markdown deliverable that lists every acceptance criterion
      verify_work  -> passes iff the deliverable mentions every criterion
      probe_*      -> answers capability probes correctly (or not, if tools=False)
    `script` maps call index -> ErrorKind (or callable) to inject failures."""

    runtime: str = "fake"
    tools: bool = True
    script: dict[int, ErrorKind | Hook] = field(default_factory=dict)
    calls: list[GenerateRequest] = field(default_factory=list)
    model_suffix: str = "fake-1"

    def describe(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(tools=self.tools, json_schema=self.tools, vision=False, streaming=False,
                                   cancellation="between_calls", context_limit_tokens=32_000)

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse:
        if cancel is not None and cancel.cancelled:
            raise ProviderError(ErrorKind.cancelled, "cancelled before call")
        n = len(self.calls)
        self.calls.append(req)
        action = self.script.get(n)
        if callable(action):
            action(req, n)
        elif action is not None:
            raise ProviderError(action, f"injected at call {n}", retry_after=0.0)
        prompt = "\n".join(m.content for m in req.messages)
        tin = max(1, len(prompt) // 4)
        tool = req.tool_choice
        calls: list[ToolCall] = []
        text = ""
        if tool == "propose_plan" and self.tools:
            calls = [ToolCall("propose_plan", {"tasks": _plan(prompt)})]
        elif tool == "submit_work" and self.tools:
            calls = [ToolCall("submit_work", _work(prompt))]
        elif tool == "verify_work" and self.tools:
            calls = [ToolCall("verify_work", _verify(prompt))]
        elif tool == "probe_echo" and self.tools:
            calls = [ToolCall("probe_echo", {"value": 42})]
        elif tool == "probe_schema" and self.tools:
            calls = [ToolCall("probe_schema", {"name": "bridge", "tags": ["a", "b"], "nested": {"ok": True}})]
        else:
            text = "I cannot call tools." if not self.tools else "ok"
        out = json.dumps([c.arguments for c in calls]) + text
        return GenerateResponse(text=text, tool_calls=calls, usage=Usage(tin, max(1, len(out) // 4)),
                                provider_model_id=f"{req.model}@{self.model_suffix}", finish_reason="tool_use" if calls else "stop",
                                raw_id=hashlib.sha256(f"{n}:{prompt}".encode()).hexdigest()[:16])


def _section(prompt: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", prompt, re.S)
    return m.group(1).strip() if m else ""


def _plan(prompt: str) -> list[dict]:
    goal = _section(prompt, "goal") or prompt
    limit = int(_section(prompt, "max_tasks") or 5)
    parts = [p.strip(" .") for p in re.split(r";| and then ", goal) if p.strip(" .")][:limit] or [goal[:120]]
    tasks = []
    for i, part in enumerate(parts):
        tasks.append({"key": f"t{i + 1}", "title": part[:120] or f"Step {i + 1}",
                      "description": f"Deliver: {part}",
                      "acceptance_criteria": [f"{part[:80]} is delivered"],
                      "depends_on": [f"t{i}"] if i else []})
    return tasks


def _work(prompt: str) -> dict:
    title = _section(prompt, "title")
    criteria = [c.strip("- ").strip() for c in _section(prompt, "criteria").splitlines() if c.strip()]
    body = [f"# {title}", "", "Deterministic fake-provider deliverable (not real model output).", ""]
    body += ["## Acceptance criteria addressed", *[f"- [x] {c}" for c in criteria]]
    return {"content": "\n".join(body) + "\n", "summary": f"Completed: {title}"[:200], "media_type": "text/markdown"}


def _verify(prompt: str) -> dict:
    content = _section(prompt, "deliverable")
    criteria = [c.strip("- ").strip() for c in _section(prompt, "criteria").splitlines() if c.strip()]
    missing = [c for c in criteria if c not in content]
    return {"passed": not missing, "notes": "all criteria present" if not missing else f"missing: {missing}"}
