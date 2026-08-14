"""Provider transport contracts and the local Ollama adapter.

Routing, privacy, quota, and circuit-breaker policy remain in ``arena_router``
and ``arena_providers``. This module only performs provider-specific transport.
The adapter uses the standard library so the Arena client has no new package
runtime dependency.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from arena_credentials import CredentialStore


@dataclass(frozen=True)
class ProviderRequest:
    """Provider-neutral chat request."""

    model: str
    messages: tuple[Mapping[str, Any], ...]
    task_type: str = "general"
    timeout: float | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_messages(
        cls,
        model: str,
        messages: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        **kwargs: Any,
    ) -> "ProviderRequest":
        return cls(model=model, messages=tuple(messages), **kwargs)


@dataclass(frozen=True)
class ProviderResponse:
    """Sanitized provider response; raw headers and authorization data are absent."""

    provider: str
    model: str
    content: str
    latency_ms: float
    usage: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    healthy: bool
    models: tuple[str, ...] = ()
    latency_ms: float | None = None
    detail: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class ExternalLiveRequestGate:
    """Explicit capability required before any external provider request."""

    enabled: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.enabled and not str(self.reason or "").strip():
            raise ValueError("Eine aktivierte Live-Freigabe benötigt einen Grund")

    @classmethod
    def disabled(cls) -> "ExternalLiveRequestGate":
        return cls(enabled=False)

    @classmethod
    def explicit(cls, reason: str) -> "ExternalLiveRequestGate":
        safe_reason = str(reason).strip()
        if not safe_reason:
            raise ValueError("Eine explizite Live-Freigabe benötigt einen Grund")
        return cls(enabled=True, reason=safe_reason[:160])

    def require_enabled(self) -> None:
        if not self.enabled or not str(self.reason or "").strip():
            raise ProviderTransportError(
                "live_request_blocked",
                "external live request gate is disabled",
                provider="external",
            )


class ProviderTransportError(RuntimeError):
    """Normalized provider failure without request bodies, headers, or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.provider = provider
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        safe_message = message.replace("\r", " ").replace("\n", " ")[:300]
        super().__init__(f"{provider}:{code}: {safe_message}")


class ProviderTransport(ABC):
    """Common interface for future provider adapters."""

    provider_id: str

    @abstractmethod
    def health_check(self, *, timeout: float | None = None) -> ProviderHealth:
        raise NotImplementedError

    @abstractmethod
    def discover_models(self, *, timeout: float | None = None) -> tuple[str, ...]:
        raise NotImplementedError

    def model_available(self, model: str, *, timeout: float | None = None) -> bool:
        """Check availability without sending a generation request."""
        return model in self.discover_models(timeout=timeout)

    @abstractmethod
    def chat(
        self,
        request: ProviderRequest,
        *,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderResponse:
        raise NotImplementedError


Urlopen = Callable[..., Any]


class OpenAICompatibleTransport(ProviderTransport):
    """Generic OpenAI-compatible chat-completions adapter.

    Credential presence is checked locally. The adapter never performs a
    discovery or health network request automatically. Health probing and chat
    both require an explicit live gate and a configured credential.
    """

    def __init__(
        self,
        provider_id: str,
        base_url: str,
        credential_env: str,
        *,
        credential_store: CredentialStore | None = None,
        timeout: float = 30.0,
        models: tuple[str, ...] = (),
        urlopen: Urlopen | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.credential_env = credential_env
        self.credential_store = credential_store or CredentialStore()
        self.timeout = float(timeout)
        self.models = tuple(models)
        self._urlopen = urlopen or urllib.request.urlopen
        self.live_gate = live_gate or ExternalLiveRequestGate.disabled()

    def health_check(
        self,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderHealth:
        """Perform readiness-only checks by default; probe remotely only when gated."""
        secret = self.credential_store.get_secret(self.credential_env)
        if not secret:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                detail="not_configured",
            )
        gate = live_gate or self.live_gate
        if not gate.enabled:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                detail="not_checked",
            )
        started = time.monotonic()
        try:
            payload = self._request_json(
                "GET", "/models", secret=secret, timeout=timeout
            )
            discovered = _openai_model_ids(payload)
            return ProviderHealth(
                provider=self.provider_id,
                healthy=True,
                models=discovered or self.models,
                latency_ms=_elapsed_ms(started),
                detail="live_probe_ok",
            )
        except ProviderTransportError as exc:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                latency_ms=_elapsed_ms(started),
                detail=exc.code,
                status_code=exc.status_code,
                retry_after_seconds=exc.retry_after_seconds,
            )

    def discover_models(self, *, timeout: float | None = None) -> tuple[str, ...]:
        """Return registry/configured models; no implicit external /models call."""
        return self.models

    def chat(
        self,
        request: ProviderRequest,
        *,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderResponse:
        secret = self.credential_store.get_secret(self.credential_env)
        if not secret:
            raise ProviderTransportError(
                "not_configured",
                "provider credential is not configured",
                provider=self.provider_id,
            )
        (live_gate or self.live_gate).require_enabled()
        if not request.model.strip():
            raise ProviderTransportError(
                "invalid_request", "model is required", provider=self.provider_id
            )
        if not request.messages:
            raise ProviderTransportError(
                "invalid_request", "at least one message is required", provider=self.provider_id
            )

        body: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": False,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens

        started = time.monotonic()
        payload = self._request_json(
            "POST",
            "/chat/completions",
            body=body,
            secret=secret,
            timeout=request.timeout,
        )
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ProviderTransportError(
                "provider_error",
                "OpenAI-compatible response did not contain message.content",
                provider=self.provider_id,
            )
        usage = _numeric_usage(payload.get("usage"))
        response_model = payload.get("model")
        return ProviderResponse(
            provider=self.provider_id,
            model=response_model if isinstance(response_model, str) else request.model,
            content=content,
            latency_ms=_elapsed_ms(started),
            usage=usage,
            metadata={"finish_reason": first.get("finish_reason")}
            if isinstance(first, Mapping) and isinstance(first.get("finish_reason"), str)
            else {},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        secret: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError:
            raise ProviderTransportError(
                "connection_failed", "provider connection failed", provider=self.provider_id
            ) from None
        except TimeoutError:
            raise ProviderTransportError(
                "timeout", "provider request timed out", provider=self.provider_id
            ) from None
        except OSError:
            raise ProviderTransportError(
                "connection_failed", "provider connection failed", provider=self.provider_id
            ) from None
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderTransportError(
                "provider_error", "provider returned invalid JSON", provider=self.provider_id
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderTransportError(
                "provider_error", "provider returned a non-object JSON response", provider=self.provider_id
            )
        return decoded

    def _http_error(self, error: urllib.error.HTTPError) -> ProviderTransportError:
        status = int(error.code)
        if status == 400:
            code = "invalid_request"
        elif status in {401, 403}:
            code = "authentication_failed"
        elif status == 404:
            code = "model_not_found"
        elif status == 408:
            code = "timeout"
        elif status == 429:
            code = "rate_limited"
        elif status == 504:
            code = "timeout"
        elif status in {502, 503}:
            code = "provider_unavailable"
        elif status >= 500:
            code = "provider_error"
        else:
            code = "provider_error"
        retry_after = _retry_after(error.headers.get("Retry-After"))
        return ProviderTransportError(
            code,
            f"HTTP {status}",
            provider=self.provider_id,
            status_code=status,
            retry_after_seconds=retry_after,
        )


class GeminiTransport(ProviderTransport):
    """Adapter for Google's official Gemini ``generateContent`` API.

    The API key is read from the configured environment variable only and is
    placed in the request query string because that is the official Gemini API
    authentication mechanism. No discovery or generation request is performed
    unless an explicit ``ExternalLiveRequestGate`` is supplied.
    """

    provider_id = "gemini"

    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com",
        credential_env: str = "GOOGLE_API_KEY",
        *,
        credential_store: CredentialStore | None = None,
        timeout: float = 30.0,
        models: tuple[str, ...] = (),
        urlopen: Urlopen | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> None:
        self.provider_id = "gemini"
        self.base_url = base_url.rstrip("/")
        self.credential_env = credential_env
        self.credential_store = credential_store or CredentialStore()
        self.timeout = float(timeout)
        self.models = tuple(models)
        self._urlopen = urlopen or urllib.request.urlopen
        self.live_gate = live_gate or ExternalLiveRequestGate.disabled()

    def health_check(
        self,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderHealth:
        secret = self.credential_store.get_secret(self.credential_env)
        if not secret:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                detail="not_configured",
            )
        gate = live_gate or self.live_gate
        if not gate.enabled:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                detail="not_checked",
            )
        started = time.monotonic()
        try:
            discovered = self.discover_models(timeout=timeout, live_gate=gate)
            return ProviderHealth(
                provider=self.provider_id,
                healthy=True,
                models=discovered or self.models,
                latency_ms=_elapsed_ms(started),
                detail="live_probe_ok",
            )
        except ProviderTransportError as exc:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                models=self.models,
                latency_ms=_elapsed_ms(started),
                detail=exc.code,
                status_code=exc.status_code,
                retry_after_seconds=exc.retry_after_seconds,
            )

    def discover_models(
        self,
        *,
        timeout: float | None = None,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> tuple[str, ...]:
        """Discover Gemini models only when an explicit gate is supplied."""
        secret = self.credential_store.get_secret(self.credential_env)
        if not secret or not (live_gate or self.live_gate).enabled:
            return self.models
        payload = self._request_json(
            "GET", "/v1beta/models", secret=secret, timeout=timeout
        )
        return _gemini_model_ids(payload) or self.models

    def chat(
        self,
        request: ProviderRequest,
        *,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderResponse:
        secret = self.credential_store.get_secret(self.credential_env)
        if not secret:
            raise ProviderTransportError(
                "not_configured",
                "provider credential is not configured",
                provider=self.provider_id,
            )
        (live_gate or self.live_gate).require_enabled()
        if not request.model.strip():
            raise ProviderTransportError(
                "invalid_request", "model is required", provider=self.provider_id
            )
        if not request.messages:
            raise ProviderTransportError(
                "invalid_request", "at least one message is required", provider=self.provider_id
            )

        body = self._generation_body(request)
        started = time.monotonic()
        payload = self._request_json(
            "POST",
            f"/v1beta/models/{request.model}:generateContent",
            body=body,
            secret=secret,
            timeout=request.timeout,
        )
        content = _gemini_text(payload)
        if content is None:
            raise ProviderTransportError(
                "provider_error",
                "Gemini response did not contain text content",
                provider=self.provider_id,
            )
        usage = _numeric_usage(payload.get("usageMetadata"))
        candidates = payload.get("candidates")
        first = candidates[0] if isinstance(candidates, list) and candidates else None
        finish_reason = first.get("finishReason") if isinstance(first, Mapping) else None
        metadata = {"finish_reason": finish_reason} if isinstance(finish_reason, str) else {}
        return ProviderResponse(
            provider=self.provider_id,
            model=request.model,
            content=content,
            latency_ms=_elapsed_ms(started),
            usage=usage,
            metadata=metadata,
        )

    @staticmethod
    def _generation_body(request: ProviderRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in request.messages:
            role = str(message.get("role", "user")).lower()
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ProviderTransportError(
                    "invalid_request",
                    "Gemini text transport requires non-empty string message content",
                    provider="gemini",
                )
            part = {"text": content}
            if role == "system":
                system_parts.append(part)
                continue
            contents.append({"role": "model" if role == "assistant" else "user", "parts": [part]})
        if not contents:
            raise ProviderTransportError(
                "invalid_request", "Gemini request requires a user or assistant message", provider="gemini"
            )
        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        generation_config: dict[str, Any] = {}
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation_config["maxOutputTokens"] = request.max_tokens
        if generation_config:
            body["generationConfig"] = generation_config
        return body

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        secret: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        if secret:
            url = f"{url}?key={urllib.parse.quote(secret, safe='')}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError:
            raise ProviderTransportError(
                "connection_failed", "provider connection failed", provider=self.provider_id
            ) from None
        except TimeoutError:
            raise ProviderTransportError(
                "timeout", "provider request timed out", provider=self.provider_id
            ) from None
        except OSError:
            raise ProviderTransportError(
                "connection_failed", "provider connection failed", provider=self.provider_id
            ) from None
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderTransportError(
                "provider_error", "provider returned invalid JSON", provider=self.provider_id
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderTransportError(
                "provider_error", "provider returned a non-object JSON response", provider=self.provider_id
            )
        return decoded

    def _http_error(self, error: urllib.error.HTTPError) -> ProviderTransportError:
        status = int(error.code)
        if status == 400:
            code = "invalid_request"
        elif status in {401, 403}:
            code = "authentication_failed"
        elif status == 404:
            code = "model_not_found"
        elif status == 408:
            code = "timeout"
        elif status == 429:
            code = "rate_limited"
        elif status == 504:
            code = "timeout"
        elif status in {502, 503}:
            code = "provider_unavailable"
        elif status >= 500:
            code = "provider_error"
        else:
            code = "provider_error"
        retry_after = _retry_after(error.headers.get("Retry-After"))
        return ProviderTransportError(
            code,
            f"HTTP {status}",
            provider=self.provider_id,
            status_code=status,
            retry_after_seconds=retry_after,
        )


class OllamaTransport(ProviderTransport):
    """Reference adapter for Ollama's local ``/api/tags`` and ``/api/chat`` APIs."""

    provider_id = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        timeout: float = 30.0,
        urlopen: Urlopen | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._urlopen = urlopen or urllib.request.urlopen

    def health_check(self, *, timeout: float | None = None) -> ProviderHealth:
        started = time.monotonic()
        try:
            models = self.discover_models(timeout=timeout)
        except ProviderTransportError as exc:
            return ProviderHealth(
                provider=self.provider_id,
                healthy=False,
                latency_ms=_elapsed_ms(started),
                detail=exc.code,
                status_code=exc.status_code,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return ProviderHealth(
            provider=self.provider_id,
            healthy=True,
            models=models,
            latency_ms=_elapsed_ms(started),
        )

    def discover_models(self, *, timeout: float | None = None) -> tuple[str, ...]:
        payload = self._request_json("GET", "/api/tags", timeout=timeout)
        raw_models = payload.get("models", [])
        if not isinstance(raw_models, list):
            raise ProviderTransportError(
                "provider_error",
                "Ollama /api/tags returned an invalid models field",
                provider=self.provider_id,
            )
        names: list[str] = []
        for item in raw_models:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                name = item["name"].strip()
                if name and name not in names:
                    names.append(name)
        return tuple(names)

    def chat(
        self,
        request: ProviderRequest,
        *,
        live_gate: ExternalLiveRequestGate | None = None,
    ) -> ProviderResponse:
        if not request.model.strip():
            raise ProviderTransportError(
                "invalid_request", "model is required", provider=self.provider_id
            )
        if not request.messages:
            raise ProviderTransportError(
                "invalid_request", "at least one message is required", provider=self.provider_id
            )
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": False,
        }
        options: dict[str, Any] = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        if options:
            body["options"] = options

        started = time.monotonic()
        payload = self._request_json(
            "POST",
            "/api/chat",
            body=body,
            timeout=request.timeout,
        )
        message = payload.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ProviderTransportError(
                "provider_error",
                "Ollama response did not contain message.content",
                provider=self.provider_id,
            )
        usage = {
            key: payload[key]
            for key in ("prompt_eval_count", "eval_count", "total_duration")
            if key in payload and isinstance(payload[key], (int, float))
        }
        response_model = payload.get("model")
        return ProviderResponse(
            provider=self.provider_id,
            model=response_model if isinstance(response_model, str) else request.model,
            content=content,
            latency_ms=_elapsed_ms(started),
            usage=usage,
            metadata={"done": payload.get("done")} if "done" in payload else {},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                raw = response.read()
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError:
            raise ProviderTransportError(
                "connection_failed", "local provider connection failed", provider=self.provider_id
            ) from None
        except TimeoutError:
            raise ProviderTransportError(
                "timeout", "local provider request timed out", provider=self.provider_id
            ) from None
        except OSError:
            raise ProviderTransportError(
                "connection_failed", "local provider connection failed", provider=self.provider_id
            ) from None
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderTransportError(
                "provider_error", "provider returned invalid JSON", provider=self.provider_id
            ) from None
        if not isinstance(decoded, dict):
            raise ProviderTransportError(
                "provider_error", "provider returned a non-object JSON response", provider=self.provider_id
            )
        return decoded

    def _http_error(self, error: urllib.error.HTTPError) -> ProviderTransportError:
        status = int(error.code)
        if status == 404:
            code = "model_not_found"
        elif status == 400:
            code = "invalid_request"
        elif status == 429:
            code = "rate_limited"
        elif status >= 500:
            code = "provider_error"
        else:
            code = "provider_unavailable"
        retry_after = _retry_after(error.headers.get("Retry-After"))
        return ProviderTransportError(
            code,
            f"HTTP {status}",
            provider=self.provider_id,
            status_code=status,
            retry_after_seconds=retry_after,
        )


def _gemini_model_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return ()
    models: list[str] = []
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        name = item.get("name")
        if isinstance(name, str):
            model = name.removeprefix("models/").strip()
            if model and model not in models:
                models.append(model)
    return tuple(models)


def _gemini_text(payload: Mapping[str, Any]) -> str | None:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    content = first.get("content") if isinstance(first, Mapping) else None
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, list):
        return None
    text = "".join(
        part["text"]
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )
    return text or None


def _openai_model_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    data = payload.get("data")
    if not isinstance(data, list):
        return ()
    models: list[str] = []
    for item in data:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            model = item["id"].strip()
            if model and model not in models:
                models.append(model)
    return tuple(models)


def _numeric_usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)
