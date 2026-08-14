"""Deterministic provider selection for Arena.

Routing is policy-only: it selects a configured, healthy provider and never
makes a network request. A transport layer can report HTTP outcomes back to the
registry through ``report_response``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from arena_providers import (
    HEALTHY,
    NOT_CHECKED,
    PRIVATE,
    PUBLIC,
    Provider,
    ProviderRegistry,
)


DEFAULT_FALLBACK_CHAINS: dict[str, list[str]] = {
    "coding": ["ollama", "groq", "sambanova", "gemini", "openrouter", "mistral"],
    "reasoning": ["ollama", "gemini", "sambanova", "groq", "openrouter"],
    "general": ["gemini", "groq", "sambanova", "mistral", "ollama"],
    "vision": ["ollama", "gemini", "cohere", "cloudflare", "openrouter"],
    "research": ["jina", "tavily", "brave", "groq"],
    "embedding": ["voyage", "jina", "cohere", "ollama"],
    "reranking": ["voyage", "jina", "cohere"],
    "speech_to_text": ["groq", "deepgram", "assemblyai", "ollama"],
    "text_to_speech": ["groq", "deepgram"],
}

TASK_CAPABILITIES: dict[str, set[str]] = {
    "coding": {"chat", "coding"},
    "reasoning": {"chat", "reasoning"},
    "general": {"chat"},
    "vision": {"chat", "vision"},
    "research": {"search"},
    "embedding": {"embeddings"},
    "reranking": {"reranking"},
    "speech_to_text": {"speech_to_text"},
    "text_to_speech": {"text_to_speech"},
}


class RoutingError(RuntimeError):
    """Raised when policy leaves no safe provider candidate."""


@dataclass(frozen=True)
class RouteRequest:
    task_type: str
    privacy_class: str = PUBLIC
    required_capabilities: frozenset[str] = frozenset()
    requested_model: str | None = None
    modality: str | None = None
    context_length: int | None = None

    def capabilities(self) -> set[str]:
        return set(self.required_capabilities) or set(TASK_CAPABILITIES.get(self.task_type, {"chat"}))


@dataclass(frozen=True)
class RouteDecision:
    task_type: str
    provider_id: str
    provider_name: str
    model: str | None
    privacy_class: str
    fallback: bool
    reason: str
    alternatives: tuple[str, ...] = ()
    stages: tuple[dict[str, Any], ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "selected_provider": self.provider_id,
            "provider_name": self.provider_name,
            "selected_model": self.model,
            "privacy_class": self.privacy_class,
            "fallback": self.fallback,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
            "stages": [dict(stage) for stage in self.stages],
        }


class ProviderRouter:
    def __init__(
        self,
        registry: ProviderRegistry,
        fallback_chains: Mapping[str, list[str]] | None = None,
        *,
        allow_unchecked: bool = False,
    ):
        self.registry = registry
        self.fallback_chains = {
            key: list(value) for key, value in DEFAULT_FALLBACK_CHAINS.items()
        }
        if fallback_chains:
            for key, value in fallback_chains.items():
                if isinstance(value, list):
                    self.fallback_chains[str(key)] = [str(item) for item in value]
        self.allow_unchecked = allow_unchecked
        self._failed_providers: set[str] = set()

    def select(
        self,
        request: RouteRequest,
        *,
        exclude: set[str] | None = None,
        allow_unchecked: bool | None = None,
    ) -> RouteDecision:
        task = request.task_type.lower()
        if task not in TASK_CAPABILITIES and not request.required_capabilities:
            raise RoutingError(f"Unbekannter Task-Typ ohne Capabilities: {request.task_type}")
        excluded = exclude or set()
        candidates = self._ordered_candidates(
            request,
            excluded,
            allow_unchecked=self.allow_unchecked if allow_unchecked is None else allow_unchecked,
        )
        if not candidates:
            raise RoutingError(
                "Kein zulässiger Provider: "
                f"task={task}, privacy={request.privacy_class}, "
                f"capabilities={sorted(request.capabilities())}"
            )
        selected = candidates[0]
        model = self._choose_model(selected, request)
        alternatives = tuple(provider.id for provider in candidates[1:])
        chain = self.fallback_chains.get(task, [])
        selected_index = chain.index(selected.id) if selected.id in chain else len(chain)
        fallback = bool(excluded) or any(
            provider_id in self._failed_providers for provider_id in chain[:selected_index]
        )
        reason = "capability+privacy+health+configured+priority"
        if fallback:
            reason += "+fallback-after-failure"
        return RouteDecision(
            task_type=task,
            provider_id=selected.id,
            provider_name=selected.name,
            model=model,
            privacy_class=request.privacy_class.upper(),
            fallback=fallback,
            reason=reason,
            alternatives=alternatives,
        )

    def plan(self, request: RouteRequest) -> RouteDecision:
        """Create a two-stage plan for research: search, then synthesis."""
        if request.task_type.lower() != "research":
            return self.select(request)
        search_request = RouteRequest(
            task_type="research",
            privacy_class=request.privacy_class,
            required_capabilities=frozenset({"search"}),
            requested_model=request.requested_model,
        )
        search = self.select(search_request)
        synthesis_request = RouteRequest(
            task_type="general",
            privacy_class=request.privacy_class,
            required_capabilities=frozenset({"chat"}),
        )
        synthesis = self.select(synthesis_request)
        return RouteDecision(
            task_type="research",
            provider_id=search.provider_id,
            provider_name=search.provider_name,
            model=search.model,
            privacy_class=request.privacy_class.upper(),
            fallback=search.fallback or synthesis.fallback,
            reason="search-provider+llm-synthesis",
            alternatives=search.alternatives,
            stages=(
                {"stage": "search", **search.public_dict()},
                {"stage": "synthesis", **synthesis.public_dict()},
            ),
        )

    def report_response(
        self,
        provider_id: str,
        status_code: int,
        *,
        retry_after_seconds: float | None = None,
        latency_ms: float | None = None,
        detail: str | None = None,
    ) -> Provider:
        if status_code < 300:
            self._failed_providers.discard(provider_id)
        else:
            self._failed_providers.add(provider_id)
        return self.registry.record_response(
            provider_id,
            status_code,
            retry_after_seconds=retry_after_seconds,
            latency_ms=latency_ms,
            detail=detail,
        )

    def _ordered_candidates(
        self,
        request: RouteRequest,
        exclude: set[str],
        *,
        allow_unchecked: bool,
    ) -> list[Provider]:
        task = request.task_type.lower()
        chain = self.fallback_chains.get(task, [])
        ordered_ids = list(chain)
        for provider in self.registry.list():
            if provider.id not in ordered_ids:
                ordered_ids.append(provider.id)
        eligible = self.registry.eligible(
            privacy_class=request.privacy_class,
            capabilities=request.capabilities(),
            task_type=task,
            requested_model=request.requested_model,
            allow_unchecked=allow_unchecked,
        )
        eligible_by_id = {provider.id: provider for provider in eligible}
        ordered = [eligible_by_id[provider_id] for provider_id in ordered_ids if provider_id in eligible_by_id]
        return [provider for provider in ordered if provider.id not in exclude]

    @staticmethod
    def _choose_model(provider: Provider, request: RouteRequest) -> str | None:
        if request.requested_model:
            return request.requested_model
        task = request.task_type.lower()
        preferred = {
            "coding": ("openai/gpt-oss-120b", "qwen3-coder", "north-mini-code", "openrouter/free"),
            "reasoning": ("gemini-3.7-flash", "openai/gpt-oss-120b", "DeepSeek-V3.2", "command-a-reasoning"),
            "general": ("gemini-3.6-flash", "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "command-a-plus"),
            "vision": ("gemini-3.7-flash", "command-a-vision", "@cf/meta/llama-3.2-11b-vision-instruct"),
            "research": ("reader-api", "search-api", "groq/compound"),
            "speech_to_text": ("whisper-large-v3", "nova-3", "universal-2"),
        }.get(task, ())
        for model in preferred:
            if model in provider.models:
                return model
        return provider.models[0] if provider.models else None


def default_fallback_chains() -> dict[str, list[str]]:
    return {key: list(value) for key, value in DEFAULT_FALLBACK_CHAINS.items()}
