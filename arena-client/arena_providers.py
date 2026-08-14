"""Provider registry and policy metadata for Arena's local routing layer.

This module does not call providers and does not contain credentials. It models
what is configured and allowed; transports can be added above it later.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from arena_credentials import CredentialStore


PUBLIC = "PUBLIC"
INTERNAL = "INTERNAL"
PRIVATE = "PRIVATE"
SECRET = "SECRET"
PRIVACY_CLASSES = frozenset({PUBLIC, INTERNAL, PRIVATE, SECRET})

HEALTHY = "healthy"
DEGRADED = "degraded"
RATE_LIMITED = "rate_limited"
QUOTA_EXHAUSTED = "quota_exhausted"
PROVIDER_DOWN = "provider_down"
MODEL_UNAVAILABLE = "model_unavailable"
AUTHENTICATION_FAILED = "authentication_failed"
PRIVACY_BLOCKED = "privacy_blocked"
NOT_CONFIGURED = "not_configured"
NOT_CHECKED = "not_checked"
DISABLED = "disabled"

_OPENAI_COMPATIBLE = "openai_compatible"


@dataclass
class Provider:
    id: str
    name: str
    type: str
    enabled: bool
    endpoint: str
    authentication_type: str
    credential_env: str | None
    capabilities: set[str]
    models: list[str]
    privacy_class: str
    allowed_privacy_classes: set[str]
    cost_class: str
    priority: int
    rate_limits: dict[str, Any] = field(default_factory=dict)
    health_status: str = NOT_CHECKED
    circuit_breaker: dict[str, Any] = field(
        default_factory=lambda: {
            "state": "closed",
            "failure_count": 0,
            "opened_until": None,
            "last_error": None,
        }
    )
    fallback_priority: int = 100
    configured: bool = False
    latency_ms: float | None = None
    quota_remaining: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "enabled": self.enabled,
            "configured": self.configured,
            "endpoint": self.endpoint,
            "authentication_type": self.authentication_type,
            "credential_env": self.credential_env,
            "capabilities": sorted(self.capabilities),
            "models": list(self.models),
            "privacy_class": self.privacy_class,
            "allowed_privacy_classes": sorted(self.allowed_privacy_classes),
            "cost_class": self.cost_class,
            "priority": self.priority,
            "rate_limits": _public_copy(self.rate_limits),
            "health_status": self.health_status,
            "circuit_breaker": _public_copy(self.circuit_breaker),
            "fallback_priority": self.fallback_priority,
            "latency_ms": self.latency_ms,
            "quota_remaining": _public_copy(self.quota_remaining),
        }


class ProviderRegistry:
    """In-memory registry with safe defaults and no network side effects."""

    def __init__(self, providers: Mapping[str, Provider]):
        self.providers = dict(providers)

    @classmethod
    def default(cls, environ: Mapping[str, str] | None = None) -> "ProviderRegistry":
        credentials = CredentialStore(environ)
        specs = _default_specs()
        providers: dict[str, Provider] = {}
        for spec in specs:
            provider = Provider(**spec)
            provider.configured = credentials.reference(provider.credential_env).configured
            if provider.enabled and not provider.configured and provider.credential_env:
                provider.health_status = NOT_CONFIGURED
            elif not provider.enabled:
                provider.health_status = DISABLED
            else:
                provider.health_status = NOT_CHECKED
            providers[provider.id] = provider
        return cls(providers)

    @classmethod
    def from_config(
        cls,
        provider_config: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ProviderRegistry":
        raw = provider_config if isinstance(provider_config, Mapping) else {}
        registry = cls.default(environ)
        overrides = raw.get("overrides", {})
        if not isinstance(overrides, Mapping):
            overrides = {}
        enabled_ids = raw.get("enabled")
        if isinstance(enabled_ids, list):
            enabled_set = {str(item) for item in enabled_ids}
            for provider in registry.providers.values():
                provider.enabled = provider.id in enabled_set
                if not provider.enabled:
                    provider.health_status = DISABLED
        for provider_id, override in overrides.items():
            if provider_id not in registry.providers or not isinstance(override, Mapping):
                continue
            provider = registry.providers[provider_id]
            for name in ("enabled", "endpoint", "priority", "fallback_priority", "cost_class"):
                if name in override and isinstance(override[name], type(getattr(provider, name))):
                    setattr(provider, name, override[name])
            if isinstance(override.get("allowed_privacy_classes"), list):
                values = {str(value).upper() for value in override["allowed_privacy_classes"]}
                provider.allowed_privacy_classes = values & set(PRIVACY_CLASSES)
            if isinstance(override.get("models"), list):
                provider.models = [str(model) for model in override["models"]]
            if isinstance(override.get("rate_limits"), Mapping):
                provider.rate_limits.update(_public_copy(dict(override["rate_limits"])))
            if override.get("configured") is True:
                # Fixture/test hook only; production configuration cannot place
                # a secret in JSON and transports still need a real credential.
                provider.configured = True
            if not provider.enabled:
                provider.health_status = DISABLED
            elif not provider.configured and provider.credential_env:
                provider.health_status = NOT_CONFIGURED
            elif provider.configured and provider.health_status == NOT_CONFIGURED:
                provider.health_status = NOT_CHECKED
        return registry

    def get(self, provider_id: str) -> Provider:
        try:
            return self.providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unbekannter Arena-Provider: {provider_id}") from exc

    def list(self) -> list[Provider]:
        return sorted(self.providers.values(), key=lambda item: (item.fallback_priority, item.id))

    def summary(self) -> list[dict[str, Any]]:
        return [provider.public_dict() for provider in self.list()]

    def set_health(
        self,
        provider_id: str,
        status: str,
        *,
        latency_ms: float | None = None,
        quota_remaining: Mapping[str, Any] | None = None,
        detail: str | None = None,
    ) -> Provider:
        provider = self.get(provider_id)
        provider.health_status = status
        provider.latency_ms = latency_ms
        if quota_remaining is not None:
            provider.quota_remaining = _public_copy(dict(quota_remaining))
        if detail:
            provider.circuit_breaker["last_error"] = detail[:300]
        return provider

    def record_response(
        self,
        provider_id: str,
        status_code: int,
        *,
        retry_after_seconds: float | None = None,
        latency_ms: float | None = None,
        detail: str | None = None,
    ) -> Provider:
        """Update health from an HTTP result without retrying or making calls."""
        provider = self.get(provider_id)
        now = time.time()
        breaker = provider.circuit_breaker
        provider.latency_ms = latency_ms
        if status_code < 300:
            provider.health_status = HEALTHY
            breaker.update(state="closed", failure_count=0, opened_until=None, last_error=None)
            return provider
        if status_code == 401:
            provider.health_status = AUTHENTICATION_FAILED
        elif status_code == 403:
            provider.health_status = PRIVACY_BLOCKED
        elif status_code == 402:
            provider.health_status = QUOTA_EXHAUSTED
        elif status_code == 429:
            provider.health_status = RATE_LIMITED
        elif status_code == 404:
            provider.health_status = MODEL_UNAVAILABLE
        elif status_code >= 500:
            provider.health_status = PROVIDER_DOWN
        else:
            provider.health_status = DEGRADED
        breaker["failure_count"] = int(breaker.get("failure_count") or 0) + 1
        breaker["last_error"] = (detail or f"HTTP {status_code}")[:300]
        if retry_after_seconds is not None:
            breaker["opened_until"] = now + max(0.0, retry_after_seconds)
        elif breaker["failure_count"] >= 3 or status_code in {401, 402, 403}:
            breaker["opened_until"] = now + 60.0
        if breaker.get("opened_until") and float(breaker["opened_until"]) > now:
            breaker["state"] = "open"
        return provider

    def circuit_allows(self, provider: Provider, now: float | None = None) -> bool:
        if provider.health_status in {
            DISABLED,
            NOT_CONFIGURED,
            AUTHENTICATION_FAILED,
            PRIVACY_BLOCKED,
            QUOTA_EXHAUSTED,
            PROVIDER_DOWN,
            MODEL_UNAVAILABLE,
        }:
            return False
        if provider.health_status == RATE_LIMITED:
            opened_until = provider.circuit_breaker.get("opened_until")
            if opened_until and float(opened_until) > (time.time() if now is None else now):
                return False
            provider.circuit_breaker["state"] = "half_open"
            return True
        opened_until = provider.circuit_breaker.get("opened_until")
        if opened_until and float(opened_until) > (time.time() if now is None else now):
            return False
        return True

    def eligible(
        self,
        *,
        privacy_class: str,
        capabilities: set[str],
        task_type: str | None = None,
        requested_model: str | None = None,
        allow_unchecked: bool = False,
    ) -> list[Provider]:
        privacy = privacy_class.upper()
        if privacy not in PRIVACY_CLASSES:
            raise ValueError(f"Unbekannte Privacy-Klasse: {privacy_class}")
        result = []
        for provider in self.list():
            if not provider.enabled or not provider.configured:
                continue
            if provider.health_status == NOT_CHECKED and not allow_unchecked:
                continue
            if provider.health_status not in {HEALTHY, NOT_CHECKED, RATE_LIMITED}:
                continue
            if not self.circuit_allows(provider):
                continue
            if privacy not in provider.allowed_privacy_classes:
                continue
            if not capabilities.issubset(provider.capabilities):
                continue
            if requested_model and requested_model not in provider.models:
                continue
            result.append(provider)
        return result

    def probe_ollama(self, timeout: float = 2.0) -> Provider:
        """Read-only local Ollama health probe; never invokes a model."""
        provider = self.get("ollama")
        request = urllib.request.Request(
            f"{provider.endpoint.rstrip('/')}/api/tags", method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    provider.configured = True
                    provider.health_status = HEALTHY
                    return provider
                provider.health_status = DEGRADED
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            provider.health_status = PROVIDER_DOWN
            provider.circuit_breaker["last_error"] = str(exc)[:300]
        return provider


def _default_specs() -> list[dict[str, Any]]:
    external_public = {PUBLIC}
    return [
        {
            "id": "ollama", "name": "Ollama", "type": "local", "enabled": True,
            "endpoint": "http://127.0.0.1:11434", "authentication_type": "none",
            "credential_env": None, "capabilities": {"chat", "coding", "reasoning", "vision", "embeddings", "tool_calling"},
            "models": ["gpt-oss-20b", "qwen3-coder", "qwen3", "devstral-small", "gemma", "llama"],
            "privacy_class": PRIVATE, "allowed_privacy_classes": set(PRIVACY_CLASSES),
            "cost_class": "local", "priority": 1, "fallback_priority": 1,
        },
        {
            "id": "groq", "name": "Groq", "type": "llm", "enabled": True,
            "endpoint": "https://api.groq.com/openai/v1", "authentication_type": "bearer",
            "credential_env": "GROQ_API_KEY", "capabilities": {"chat", "coding", "reasoning", "tool_calling", "vision", "speech_to_text", "search"},
            "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "groq/compound", "groq/compound-mini", "whisper-large-v3", "whisper-large-v3-turbo"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_rate_limited", "priority": 10, "fallback_priority": 10,
            "rate_limits": {"source": "official docs; account/model dependent", "http_429": True},
        },
        {
            "id": "gemini", "name": "Google Gemini API", "type": "multimodal", "enabled": True,
            "endpoint": "https://generativelanguage.googleapis.com", "authentication_type": "api_key",
            "credential_env": "GOOGLE_API_KEY", "capabilities": {"chat", "coding", "reasoning", "vision", "tool_calling", "long_context"},
            "models": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"],
            "privacy_class": PUBLIC, "allowed_privacy_classes": external_public,
            "cost_class": "free_rate_limited", "priority": 20, "fallback_priority": 20,
            "rate_limits": {"source": "official docs; per project/model; inspect AI Studio", "project_scoped": True},
        },
        {
            "id": "sambanova", "name": "SambaNova", "type": "llm", "enabled": True,
            "endpoint": "https://api.sambanova.ai/v1", "authentication_type": "bearer",
            "credential_env": "SAMBANOVA_API_KEY", "capabilities": {"chat", "coding", "reasoning", "tool_calling"},
            "models": ["DeepSeek-V3.2", "Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b", "gemma-4-31B-it"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_rate_limited", "priority": 30, "fallback_priority": 30,
            "rate_limits": {"source": "official docs", "free_rpm": 20, "free_rpd": 20, "free_tpd": 200000},
        },
        {
            "id": "cohere", "name": "Cohere", "type": "llm", "enabled": True,
            "endpoint": "https://api.cohere.com/v2", "authentication_type": "bearer",
            "credential_env": "COHERE_API_KEY", "capabilities": {"chat", "coding", "reasoning", "vision", "embeddings", "reranking", "speech_to_text", "tool_calling"},
            "models": ["command-a-plus", "command-a-reasoning", "command-a-vision", "north-mini-code"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_trial", "priority": 40, "fallback_priority": 40,
            "rate_limits": {"source": "official docs", "trial_calls_per_month": 1000, "chat_rpm": 20, "rerank_rpm": 10},
        },
        {
            "id": "mistral", "name": "Mistral", "type": "llm", "enabled": True,
            "endpoint": "https://api.mistral.ai/v1", "authentication_type": "bearer",
            "credential_env": "MISTRAL_API_KEY", "capabilities": {"chat", "coding", "reasoning", "vision", "tool_calling"},
            "models": ["devstral-small", "mistral-small", "mistral-large", "codestral"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_experiment", "priority": 50, "fallback_priority": 50,
            "rate_limits": {"source": "account console; do not assume static community limits"},
        },
        {
            "id": "openrouter", "name": "OpenRouter", "type": "aggregator", "enabled": True,
            "endpoint": "https://openrouter.ai/api/v1", "authentication_type": "bearer",
            "credential_env": "OPENROUTER_API_KEY", "capabilities": {"chat", "coding", "reasoning", "vision", "tool_calling", "search", "long_context"},
            "models": ["openrouter/free"],
            "privacy_class": PUBLIC, "allowed_privacy_classes": external_public,
            "cost_class": "aggregator_rate_limited", "priority": 60, "fallback_priority": 60,
            "rate_limits": {"source": "official docs; dynamic free-model caps", "additional_keys_do_not_raise_limits": True},
        },
        {
            "id": "jina", "name": "Jina AI", "type": "search", "enabled": True,
            "endpoint": "https://api.jina.ai", "authentication_type": "bearer",
            "credential_env": "JINA_API_KEY", "capabilities": {"search", "embeddings", "reranking"},
            "models": ["reader-api", "jina-embeddings-v5-text", "jina-reranker-v3.5"],
            "privacy_class": PUBLIC, "allowed_privacy_classes": external_public,
            "cost_class": "free_rate_limited", "priority": 10, "fallback_priority": 10,
            "rate_limits": {"source": "official docs; free-key RPM/TPM", "reader_free_rpm": 500, "embedding_free_tpm": 2000000},
        },
        {
            "id": "voyage", "name": "Voyage AI", "type": "embedding", "enabled": True,
            "endpoint": "https://api.voyageai.com/v1", "authentication_type": "bearer",
            "credential_env": "VOYAGE_API_KEY", "capabilities": {"embeddings", "reranking"},
            "models": ["voyage-4-large", "voyage-4", "voyage-4-lite", "voyage-code-4", "rerank-2.5", "rerank-2.5-lite"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_token_credit", "priority": 10, "fallback_priority": 10,
            "rate_limits": {"source": "official pricing", "free_embedding_tokens": 200000000, "free_rerank_tokens": 200000000},
        },
        {
            "id": "tavily", "name": "Tavily", "type": "search", "enabled": True,
            "endpoint": "https://api.tavily.com", "authentication_type": "api_key",
            "credential_env": "TAVILY_API_KEY", "capabilities": {"search"}, "models": ["search-api"],
            "privacy_class": PUBLIC, "allowed_privacy_classes": external_public,
            "cost_class": "free_monthly_credit", "priority": 20, "fallback_priority": 20,
            "rate_limits": {"source": "official docs", "free_credits_per_month": 1000},
        },
        {
            "id": "brave", "name": "Brave Search", "type": "search", "enabled": True,
            "endpoint": "https://api.search.brave.com/res/v1", "authentication_type": "api_key",
            "credential_env": "BRAVE_API_KEY", "capabilities": {"search"}, "models": ["search-api"],
            "privacy_class": PUBLIC, "allowed_privacy_classes": external_public,
            "cost_class": "monthly_credit", "priority": 30, "fallback_priority": 30,
            "rate_limits": {"source": "official pricing", "monthly_free_credits_usd": 5, "price_per_1000_requests_usd": 5},
        },
        {
            "id": "deepgram", "name": "Deepgram", "type": "speech_to_text", "enabled": True,
            "endpoint": "https://api.deepgram.com/v1", "authentication_type": "bearer",
            "credential_env": "DEEPGRAM_API_KEY", "capabilities": {"speech_to_text", "text_to_speech"},
            "models": ["nova-3", "flux", "whisper"], "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_trial_credit", "priority": 20, "fallback_priority": 20,
            "rate_limits": {"source": "official pricing", "free_credit_usd": 200},
        },
        {
            "id": "assemblyai", "name": "AssemblyAI", "type": "speech_to_text", "enabled": True,
            "endpoint": "https://api.assemblyai.com", "authentication_type": "bearer",
            "credential_env": "ASSEMBLYAI_API_KEY", "capabilities": {"speech_to_text"},
            "models": ["universal-2", "universal-3.5"], "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_trial_credit", "priority": 30, "fallback_priority": 30,
            "rate_limits": {"source": "official pricing", "free_credit_usd": 50},
        },
        {
            "id": "cloudflare", "name": "Cloudflare Workers AI", "type": "multimodal", "enabled": True,
            "endpoint": "https://api.cloudflare.com/client/v4", "authentication_type": "bearer",
            "credential_env": "CLOUDFLARE_API_TOKEN", "capabilities": {"chat", "coding", "reasoning", "vision", "embeddings", "speech_to_text"},
            "models": ["@cf/openai/gpt-oss-120b", "@cf/qwen/qwen3-embedding-0.6b", "@cf/meta/llama-3.2-11b-vision-instruct"],
            "privacy_class": INTERNAL, "allowed_privacy_classes": external_public,
            "cost_class": "free_daily_neurons", "priority": 70, "fallback_priority": 70,
            "rate_limits": {"source": "official docs", "free_neurons_per_day": 10000},
        },
    ]


def _public_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _public_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_copy(item) for item in value]
    if isinstance(value, set):
        return sorted(_public_copy(item) for item in value)
    return value
