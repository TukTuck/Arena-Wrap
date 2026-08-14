# Changelog

## v0.9.0 — Stable Baseline

This baseline captures the completed Arena Phase 8 through Phase 9L work:

- Provider registry with capability-aware routing, privacy policy, health states,
  quotas, fallback metadata, and circuit-breaker handling.
- Isolated local Ollama transport plus gated OpenAI-compatible Groq and Gemini
  transports.
- Explicit external live-request gate; no automatic external provider traffic.
- Provider diagnostics CLI and Tkinter diagnostics dashboard.
- Bounded, sanitized local provider-health history.
- Offline health trends, local alerts, acknowledgement, suppression, resolution,
  filtering, bulk actions, and reports.
- Local JSON/TXT diagnostic exports and explicit archive rotation for health and
  alert state.
- Regression coverage for the Phase 8–9L local, gated architecture.

No API keys, payment methods, external provider requests, or model downloads are
part of this baseline.
