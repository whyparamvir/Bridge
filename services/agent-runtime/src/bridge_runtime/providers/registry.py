"""Extensible adapter registry + server-side secret resolution + cost rates.

New runtimes register a factory; nothing else in the runtime changes. Keys are read
from the process environment only when an adapter is built (`env:NAME` secret refs), so
they never touch the database, events, logs or the browser.

Cost: budgets need a price per token. Rates come from BRIDGE_MODEL_RATES (JSON:
{"<provider_model_or_model_id>": [input_micros_per_mtok, output_micros_per_mtok]}). A
live call to a model without a configured rate is refused, so the budget can always be
enforced. No prices are hard-coded here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import ErrorKind, ProviderAdapter, ProviderError, Usage
from .fake import FakeProvider
from .http import AnthropicAdapter, GeminiAdapter, OpenAIChatAdapter, OpenAICompatibleAdapter

DEFAULT_BASE = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com",
    "ollama": "http://127.0.0.1:11434",
    "lmstudio": "http://127.0.0.1:1234",
    "vllm": "http://127.0.0.1:8000",
}


class SecretResolver:
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._env = environ if environ is not None else os.environ

    def resolve(self, ref: str | None) -> str | None:
        if ref is None:
            return None
        if not ref.startswith("env:"):
            raise ProviderError(ErrorKind.auth, "unsupported secret reference")
        value = self._env.get(ref[4:])
        if not value:
            raise ProviderError(ErrorKind.auth, f"secret {ref} is not set on the server")
        return value


@dataclass
class Rates:
    table: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Rates:
        raw = (environ if environ is not None else os.environ).get("BRIDGE_MODEL_RATES", "")
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls({k: (int(v[0]), int(v[1])) for k, v in data.items()})

    def known(self, model: str) -> bool:
        return model in self.table

    def cost_micros(self, model: str, usage: Usage) -> int:
        i, o = self.table[model]
        return (usage.input_tokens * i + usage.output_tokens * o + 999_999) // 1_000_000

    def estimate_micros(self, model: str, prompt_chars: int, max_tokens: int) -> int:
        return self.cost_micros(model, Usage(prompt_chars // 3 + 1, max_tokens))


Factory = Callable[..., ProviderAdapter]


@dataclass
class AdapterRegistry:
    allow_live: bool = False
    secrets: SecretResolver = field(default_factory=SecretResolver)
    rates: Rates = field(default_factory=Rates)
    client: httpx.Client | None = None  # tests inject a MockTransport client
    factories: dict[str, Factory] = field(default_factory=dict)
    fakes: dict[str, FakeProvider] = field(default_factory=dict)

    def __post_init__(self) -> None:
        def http(cls, **extra):  # noqa: ANN001, ANN202
            def make(base_url: str | None, secret_ref: str | None, runtime: str) -> ProviderAdapter:
                return cls(base_url=base_url or DEFAULT_BASE[runtime], api_key=self.secrets.resolve(secret_ref),
                           allow_live=self.allow_live, client=self.client, **extra)
            return make

        def compat(base_url: str | None, secret_ref: str | None, runtime: str) -> ProviderAdapter:
            if not base_url and runtime not in DEFAULT_BASE:
                raise ProviderError(ErrorKind.invalid_request, f"{runtime} endpoints need a base_url")
            return OpenAICompatibleAdapter(runtime=runtime, base_url=base_url or DEFAULT_BASE[runtime],
                                           api_key=self.secrets.resolve(secret_ref) if secret_ref else None,
                                           allow_live=self.allow_live, client=self.client)

        self.factories.setdefault("anthropic", http(AnthropicAdapter))
        self.factories.setdefault("openai", http(OpenAIChatAdapter))
        self.factories.setdefault("google", http(GeminiAdapter))
        for r in ("ollama", "lmstudio", "vllm", "custom_endpoint", "hosted_open_model"):
            self.factories.setdefault(r, compat)

    def register(self, runtime: str, factory: Factory) -> None:
        self.factories[runtime] = factory

    def build(self, runtime: str, *, base_url: str | None = None, secret_ref: str | None = None,
              fake_key: str | None = None) -> ProviderAdapter:
        if fake_key is not None:
            return self.fakes.setdefault(fake_key, FakeProvider())
        if runtime not in self.factories:
            raise ProviderError(ErrorKind.invalid_request, f"no adapter registered for runtime {runtime!r}")
        return self.factories[runtime](base_url, secret_ref, runtime)

    def cost(self, adapter: ProviderAdapter, model: str, usage: Usage) -> tuple[int, bool]:
        """(cost_micros, rate_known). Fakes are priced at 1 micro-unit per token so budget
        logic is exercised deterministically."""
        if isinstance(adapter, FakeProvider):
            return usage.input_tokens + usage.output_tokens, True
        if not self.rates.known(model):
            return 0, False
        return self.rates.cost_micros(model, usage), True

    def estimate(self, adapter: ProviderAdapter, model: str, prompt_chars: int, max_tokens: int) -> int:
        if isinstance(adapter, FakeProvider):
            return prompt_chars // 4 + max_tokens
        if not self.rates.known(model):
            raise ProviderError(ErrorKind.disabled, f"no rate configured for {model}; cannot enforce budget")
        return self.rates.estimate_micros(model, prompt_chars, max_tokens)

    @classmethod
    def from_env(cls, **kw: Any) -> AdapterRegistry:
        from bridge_control.settings import Settings

        s = Settings.from_env()
        return cls(allow_live=s.live_provider_calls, rates=Rates.from_env(), **kw)
