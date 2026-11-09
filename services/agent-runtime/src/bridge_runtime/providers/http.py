"""HTTP provider adapters. Keys are resolved server-side at call time and never logged.

SAFETY: every adapter refuses to send a request unless `allow_live=True`, which the
registry sets only from BRIDGE_LIVE_PROVIDER_CALLS=1. Tests inject an httpx MockTransport,
so the adapters' request/response mapping is verified without any network or spend.
These adapters have NOT been exercised against the real services from this repository.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import (
    AdapterCapabilities, CancelToken, ErrorKind, GenerateRequest, GenerateResponse, ProviderError, ToolCall, Usage,
)


def _classify(resp: httpx.Response) -> ProviderError:
    s = resp.status_code
    retry_after = None
    if "retry-after" in resp.headers:
        try:
            retry_after = float(resp.headers["retry-after"])
        except ValueError:
            retry_after = None
    detail = f"HTTP {s}"
    if s == 429:
        return ProviderError(ErrorKind.rate_limited, detail, retry_after=retry_after)
    if s in (401, 403):
        return ProviderError(ErrorKind.auth, detail)
    if s in (408, 504):
        return ProviderError(ErrorKind.timeout, detail)
    if s >= 500 or s == 529:
        return ProviderError(ErrorKind.unavailable, detail, retry_after=retry_after)
    return ProviderError(ErrorKind.invalid_request, detail)


class _HTTPAdapter:
    runtime = "http"

    def __init__(self, *, base_url: str, api_key: str | None, allow_live: bool,
                 client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self.allow_live = allow_live
        self.client = client or httpx.Client()

    def __repr__(self) -> str:  # never print the key
        return f"{type(self).__name__}(base_url={self.base_url!r}, key={'set' if self._key else 'none'})"

    def _post(self, path: str, body: dict, headers: dict, req: GenerateRequest,
              cancel: CancelToken | None) -> dict:
        if not self.allow_live:
            raise ProviderError(ErrorKind.disabled, "live provider calls are disabled (BRIDGE_LIVE_PROVIDER_CALLS)")
        if cancel is not None and cancel.cancelled:
            raise ProviderError(ErrorKind.cancelled, "cancelled before call")
        try:
            resp = self.client.post(self.base_url + path, json=body, headers=headers, timeout=req.timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderError(ErrorKind.timeout, type(e).__name__) from None
        except httpx.TransportError as e:
            raise ProviderError(ErrorKind.unavailable, type(e).__name__) from None
        if resp.status_code >= 400:
            raise _classify(resp)
        try:
            return resp.json()
        except ValueError:
            raise ProviderError(ErrorKind.unavailable, "non-JSON response") from None


# ---------------------------------------------------------------- Anthropic


class AnthropicAdapter(_HTTPAdapter):
    runtime = "anthropic"
    API_VERSION = "2023-06-01"

    def describe(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(tools=True, json_schema=True, vision=True, streaming=True, cancellation="between_calls")

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse:
        system = "\n".join(m.content for m in req.messages if m.role == "system")
        body: dict[str, Any] = {
            "model": req.model, "max_tokens": req.max_tokens, "temperature": req.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
        if req.tool_choice:
            body["tool_choice"] = {"type": "tool", "name": req.tool_choice}
        data = self._post("/v1/messages", body, {"x-api-key": self._key or "", "anthropic-version": self.API_VERSION},
                          req, cancel)
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                if not isinstance(block.get("input"), dict):
                    raise ProviderError(ErrorKind.invalid_tool_arguments, "tool input is not an object")
                calls.append(ToolCall(block["name"], block["input"], block.get("id")))
        u = data.get("usage", {})
        return GenerateResponse(text="".join(text), tool_calls=calls,
                                usage=Usage(u.get("input_tokens", 0), u.get("output_tokens", 0)),
                                provider_model_id=data.get("model", req.model), finish_reason=data.get("stop_reason", ""),
                                raw_id=data.get("id"))


# ---------------------------------------------------- OpenAI + compatible


class OpenAIChatAdapter(_HTTPAdapter):
    """OpenAI Chat Completions; also the OpenAI-compatible surface of Ollama, LM Studio,
    vLLM and custom servers. Compatible syntax is NOT proof of tool support: endpoints
    must pass probes before they are used with tools or as orchestrators."""

    runtime = "openai"

    def __init__(self, *, max_tokens_field: str = "max_completion_tokens", **kw: Any) -> None:
        super().__init__(**kw)
        self.max_tokens_field = max_tokens_field

    def describe(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(tools=True, json_schema=True, vision=False, streaming=True, cancellation="between_calls")

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse:
        body: dict[str, Any] = {
            "model": req.model, "temperature": req.temperature, self.max_tokens_field: req.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in req.tools]
        if req.tool_choice:
            body["tool_choice"] = {"type": "function", "function": {"name": req.tool_choice}}
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        data = self._post("/v1/chat/completions", body, headers, req, cancel)
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(ErrorKind.unavailable, "malformed completion") from None
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                raise ProviderError(ErrorKind.invalid_tool_arguments, "arguments are not valid JSON") from None
            if not isinstance(args, dict):
                raise ProviderError(ErrorKind.invalid_tool_arguments, "arguments are not an object")
            calls.append(ToolCall(fn.get("name", ""), args, tc.get("id")))
        u = data.get("usage") or {}
        return GenerateResponse(text=msg.get("content") or "", tool_calls=calls,
                                usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0)),
                                provider_model_id=data.get("model", req.model),
                                finish_reason=choice.get("finish_reason") or "", raw_id=data.get("id"))


class OpenAICompatibleAdapter(OpenAIChatAdapter):
    def __init__(self, *, runtime: str, **kw: Any) -> None:
        super().__init__(max_tokens_field="max_tokens", **kw)
        self.runtime = runtime

    def describe(self, model: str) -> AdapterCapabilities:
        # Unknown until probed.
        return AdapterCapabilities(tools=False, json_schema=False, vision=False, streaming=True, cancellation="between_calls")


# ------------------------------------------------------------------- Google


def _gemini_schema(s: Any) -> Any:
    """Gemini accepts an OpenAPI subset; strip keywords it rejects."""
    if isinstance(s, dict):
        return {k: _gemini_schema(v) for k, v in s.items() if k not in ("additionalProperties", "title", "$defs", "$schema")}
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    return s


class GeminiAdapter(_HTTPAdapter):
    runtime = "google"

    def describe(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(tools=True, json_schema=True, vision=True, streaming=True, cancellation="between_calls")

    def generate(self, req: GenerateRequest, cancel: CancelToken | None = None) -> GenerateResponse:
        system = "\n".join(m.content for m in req.messages if m.role == "system")
        body: dict[str, Any] = {
            "contents": [{"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
                         for m in req.messages if m.role != "system"],
            "generationConfig": {"maxOutputTokens": req.max_tokens, "temperature": req.temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if req.tools:
            body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description,
                                                        "parameters": _gemini_schema(t.parameters)} for t in req.tools]}]
        if req.tool_choice:
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [req.tool_choice]}}
        data = self._post(f"/v1beta/models/{req.model}:generateContent", body, {"x-goog-api-key": self._key or ""},
                          req, cancel)
        try:
            cand = data["candidates"][0]
        except (KeyError, IndexError, TypeError):
            if data.get("promptFeedback", {}).get("blockReason"):
                raise ProviderError(ErrorKind.content_filtered, data["promptFeedback"]["blockReason"]) from None
            raise ProviderError(ErrorKind.unavailable, "no candidates") from None
        text, calls = [], []
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                text.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                if not isinstance(fc.get("args", {}), dict):
                    raise ProviderError(ErrorKind.invalid_tool_arguments, "args not an object")
                calls.append(ToolCall(fc.get("name", ""), fc.get("args", {})))
        u = data.get("usageMetadata", {})
        return GenerateResponse(text="".join(text), tool_calls=calls,
                                usage=Usage(u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)),
                                provider_model_id=data.get("modelVersion", req.model),
                                finish_reason=cand.get("finishReason", ""), raw_id=data.get("responseId"))
