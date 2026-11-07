"""JSON-producing LLM providers (Groq, Gemini) with failover on rate limits."""

from __future__ import annotations

from typing import Protocol

from .config import ConfigError, Settings
from .logs import console, log
from .ratelimit import StopStage, call_with_backoff

TIMEOUT_S = 120


class LLM(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> str:
        """Return the model's raw JSON text for `schema`."""


class GroqLLM:
    def __init__(self, settings: Settings) -> None:
        from groq import Groq

        self.model = settings.llm.groq_model
        self.name = f"groq:{self.model}"
        self.client = Groq(api_key=settings.secret("GROQ_API_KEY"), max_retries=1, timeout=TIMEOUT_S)
        self.cfg = settings.llm

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> str:
        def call() -> str:
            r = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=self.cfg.temperature,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            )
            return r.choices[0].message.content or ""

        return call_with_backoff(call, provider="groq", max_wait=self.cfg.max_wait_seconds)


class GeminiLLM:
    def __init__(self, settings: Settings) -> None:
        from google import genai
        from google.genai import types

        self.model = settings.llm.gemini_model
        self.name = f"gemini:{self.model}"
        self.client = genai.Client(api_key=settings.secret("GEMINI_API_KEY"),
                                   http_options=types.HttpOptions(timeout=TIMEOUT_S * 1000))
        self.cfg = settings.llm

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> str:
        def call() -> str:
            it = self.client.interactions.create(
                model=self.model,
                input=f"{system}\n\n{user}",
                response_format={"type": "text", "mime_type": "application/json", "schema": schema},
                timeout=TIMEOUT_S,
            )
            return it.output_text or ""

        return call_with_backoff(call, provider="gemini", max_wait=self.cfg.max_wait_seconds)


_PROVIDERS = {"groq": GroqLLM, "gemini": GeminiLLM}


class ProviderChain:
    """Try providers in preference order; drop one for the rest of the run once it's capped."""

    def __init__(self, providers: list[LLM]) -> None:
        if not providers:
            raise ConfigError("No LLM providers available (check [llm].providers and API keys in .env).")
        self.active = list(providers)
        self.last_used: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderChain:
        providers: list[LLM] = []
        for key in settings.llm.providers:
            if key not in _PROVIDERS:
                raise ConfigError(f"Unknown LLM provider {key!r}; choose from {sorted(_PROVIDERS)}")
            try:
                providers.append(_PROVIDERS[key](settings))
            except ConfigError as e:  # e.g. missing key for a fallback provider
                console.print(f"[yellow]skipping LLM provider {key}:[/] {e}")
        return cls(providers)

    @property
    def name(self) -> str:
        return self.last_used or (self.active[0].name if self.active else "none")

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> str:
        while self.active:
            p = self.active[0]
            try:
                out = p.complete_json(system, user, schema, schema_name)
                self.last_used = p.name
                return out
            except StopStage as e:
                self.active.pop(0)
                log.warning("provider capped, failing over", extra={"fields": {"provider": p.name, "why": str(e)}})
                if self.active:
                    console.print(f"[yellow]{e}; switching to {self.active[0].name}[/]")
        raise StopStage("all LLM providers are rate-limited")
