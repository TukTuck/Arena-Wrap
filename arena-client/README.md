# Arena Client — Stand-alone Foundation

**Version:** `v0.9.0`

**Status:** Stable Baseline

Diese Komponente bildet die kontrollierte Arena-Schicht um Hermes Desktop. Sie
enthält weiterhin keine Multi-Agent-Ausführung, kein Kanban und keine
Datenmigration. Phase 8 ergänzt den lokalen Provider-Katalog und die policy-basierte
Routingvorbereitung. Phase 9A ergänzt eine gemeinsame Transport-Schnittstelle
und ausschließlich den lokalen Ollama-Adapter. Phase 9B ergänzt den generischen
OpenAI-kompatiblen Transport für Groq; Phase 9C und 9D schützen externe
Transporte mit einem expliziten Live-Gate und ergänzen den offiziellen Gemini-
Adapter. Externe Provider werden weiterhin nicht automatisch aufgerufen.

```text
Arena Launcher
    ↓
arena-config.json
    ↓
Arena Runtime Manager + Provider Registry
    ↓
Policy-/Capability-Router
    ↓
Hermes Desktop / OmniRoute / Provider
```

## Runtime-Grenze

Jede Hermes-Runtime muss in der Konfiguration explizit angegeben werden. Der
Launcher verwendet keine globalen Hermes-Defaults und setzt für den Desktop:

```text
HERMES_HOME
HERMES_DESKTOP_USER_DATA_DIR
HERMES_DESKTOP_HERMES_ROOT
HERMES_DESKTOP_PYTHON
HERMES_DESKTOP_IGNORE_EXISTING=1
PYTHONNOUSERSITE=1
```

Fehlt ein Pfad oder zeigt er auf den globalen Hermes-Bereich, bricht der
Launcher ab. Es gibt keinen stillen PATH-/System-Python-/globalen-Hermes-
Fallback.

## Zielstruktur

`arena-config.example.json` beschreibt die portable Produktstruktur:

```text
Arena Client/
├── arena-config.json          lokale, nicht versionierte Konfiguration
├── arena_launcher.py          Start und Smoke-Test
├── arena_runtime.py           Validierung, Start, Health-Check, Stop
├── arena_providers.py         Provider Registry und Provider-Metadaten
├── arena_credentials.py       Secret-sichere Env-Referenzen
├── arena_router.py             Capability-/Privacy-/Fallback-Routing
├── arena_transport.py          Transportvertrag + lokaler Ollama-Adapter
├── runtime/
│   └── hermes-agent/          eigener Hermes Checkout + venv
├── state/
│   ├── hermes-home/            eigener Hermes Runtime-State
│   ├── desktop-data/           eigene Electron-Daten
│   └── arena/                  eigener Arena-Metadaten-State
├── desktop/
│   └── Hermes.exe              später gebündelter Desktop
└── logs/                       optionaler Arena-Launcher-Logbereich
```

Die tatsächlichen Laufzeitpfade werden ausschließlich aus der Konfiguration
aufgelöst. `arena-config.json` ist absichtlich nicht Bestandteil des
Repositorys; nur das Beispiel wird versioniert.

## Provider Registry und Routing

Phase 8 registriert folgende Provider ohne automatisch Credentials anzulegen:

```text
Ollama, Groq, Gemini, SambaNova, Cohere, Mistral, OpenRouter,
Jina, Voyage, Tavily, Brave, Deepgram, AssemblyAI,
Cloudflare Workers AI
```

Fehlende API-Keys führen nur zu:

```text
configured=false
health_status=not_configured
```

und nicht zu einem Startup-Crash. Credentials werden ausschließlich über
Umgebungsvariablen referenziert, niemals in JSON, Python, Git oder Logs
gespeichert.

Unterstützte Datenklassen:

```text
PUBLIC → explizit freigegebene externe Provider
INTERNAL → nur konfigurierte Allowlist-Provider
PRIVATE → standardmäßig Ollama oder explizit genehmigter Provider
SECRET → standardmäßig ausschließlich Ollama
```

Der Router berücksichtigt zusätzlich Task-Typ, Capabilities, Modell, Health,
Circuit-Breaker und Fallback-Kette. `research` erzeugt einen zweistufigen Plan:
Search Provider und danach LLM-Synthese. Dies ist nur eine Planungsentscheidung;
es findet dabei kein Netzwerkaufruf und keine Modellinferenz statt.

Health-Zustände umfassen unter anderem:

```text
healthy, degraded, rate_limited, quota_exhausted,
provider_down, model_unavailable, authentication_failed,
privacy_blocked, not_configured, not_checked, disabled
```

HTTP-429-/Retry-After-Informationen werden über `ProviderRouter.report_response`
in den Circuit-Breaker übernommen. Es gibt keine aggressive Retry-Kaskade und
keine Limitumgehung.

## Credential-Variablen

```text
GROQ_API_KEY
GOOGLE_API_KEY
SAMBANOVA_API_KEY
COHERE_API_KEY
MISTRAL_API_KEY
OPENROUTER_API_KEY
JINA_API_KEY
VOYAGE_API_KEY
TAVILY_API_KEY
BRAVE_API_KEY
DEEPGRAM_API_KEY
ASSEMBLYAI_API_KEY
CLOUDFLARE_API_TOKEN
```

Die Variablen müssen nicht gesetzt sein. Der Providerstatus bleibt dann
`not_configured`. Ihre Werte werden weder diagnostiziert noch ausgegeben.

## Optionale Doppler-Integration (Phase 9M)

Doppler ist optional und dient ausschließlich als Environment-Injektor. Die
Provider-Adapter bleiben unverändert und lesen weiterhin nur die bestehenden
Variablen `GROQ_API_KEY` und `GOOGLE_API_KEY` über `CredentialStore` aus dem
Prozess-Environment. Arena speichert keine Doppler-Tokens und enthält keine
Doppler-Abhängigkeit.

Die Doppler CLI war beim v0.9.0-Integrationscheck nicht installiert. Nach einer
separaten, manuellen Doppler-Einrichtung kann der Workflow beispielsweise so
aussehen:

```powershell
doppler run --project arena --config dev -- python arena-client/arena_launcher.py --version
doppler run --project arena --config dev -- python arena-client/arena_launcher.py diagnostics --dry-run
```

Erwartet wird:

```text
v0.9.0
Network Requests: 0
```

`doppler run` darf nur Variablen injizieren. Es aktiviert niemals das bestehende
`ExternalLiveRequestGate`; ein vorhandener Key bei deaktiviertem Gate führt daher
weiterhin zu null externen Requests. Tests verwenden ausschließlich synthetische
Fixture-Environment-Werte. Doppler-Projektmetadaten oder lokale CLI-Artefakte
werden nicht versioniert.

## Provider-Transport (Phase 9A/9B)

Der gemeinsame Transportvertrag liegt in `arena_transport.py`:

```text
ProviderRequest / ProviderResponse
        ↓
ProviderTransport
        ↓
OllamaTransport
        ↓
http://127.0.0.1:11434/api/tags oder /api/chat
```

`ArenaControl.ollama_health()` prüft ausschließlich `/api/tags` und aktualisiert
Ollama-Modellnamen/Health in der bestehenden Registry. Eine lokale Chat-Anfrage
ist nur über den expliziten `chat_with_ollama()`-Pfad möglich; dieser führt keinen
Cloud-Fallback aus. `send_provider_request()` verwendet den Phase-8-Router und
bricht für Provider ohne Adapter kontrolliert ab. Providerfehler werden als
`ProviderTransportError` mit Codes wie `connection_failed`, `timeout`,
`model_not_found` oder `provider_error` normalisiert.

Die Tests verwenden Fixtures und senden keine externen Requests. Ein echter
lokaler Modelltest ist separat und nur mit einem bereits vorhandenen Ollama-
Modell sinnvoll; es wird niemals automatisch `ollama pull` ausgeführt.

### OpenAI-kompatibler Transport / Groq (Phase 9B/9C)

`OpenAICompatibleTransport` verwendet dieselben `ProviderRequest`-,
`ProviderResponse`- und `ProviderTransportError`-Modelle wie Ollama. Groq ist als
aktiver Adapter vorbereitet:

```text
Base URL: https://api.groq.com/openai/v1
Endpoint: /chat/completions
Credential: GROQ_API_KEY (nur Environment-Referenz)
```

Fehlt `GROQ_API_KEY`, bleibt Groq `not_configured` und es wird kein HTTP-
Request gesendet. Die Health-Prüfung ist in diesem Fall ausschließlich lokal;
auch bei gesetztem Key führt sie keinen automatischen Probe-Request aus. Ein
externer Request ist nur möglich, wenn die Anwendung ausdrücklich einen
Groq-Transport ausführt. Die Phase-9B-Tests verwenden ausschließlich Fixtures.

`PRIVATE` und `SECRET` werden weiterhin vor dem Transport durch den bestehenden
Router blockiert, sofern kein ausdrücklich erlaubter Privacy-Provider vorhanden
ist. Der Transport besitzt keine eigene Fallback- oder Retry-Logik.

### Gemini-Transport (Phase 9D)

`GeminiTransport` verwendet ausschließlich die offizielle Google-Gemini-
`generateContent`-API. Der API-Key wird nur aus `GOOGLE_API_KEY` gelesen und
niemals gespeichert, geloggt oder in Diagnosen zurückgegeben. Modell-Discovery
über `/v1beta/models` und Textgenerierung sind beide gated; ohne Credential oder
ohne `ExternalLiveRequestGate.explicit(...)` bleibt der Netzwerkverkehr bei null.
Die Implementierung unterstützt in dieser Phase nur nicht-streamende
Textgenerierung. Es wird kein Proxy und kein OpenRouter-Umweg verwendet.

### Explicit Live Request Gate (Phase 9C)

Externe Requests sind standardmäßig gesperrt:

```text
ExternalLiveRequestGate.disabled()
```

Der normale `send_provider_request()`-Pfad darf deshalb keinen externen Groq-
Request ausführen. Für einen kontrollierten Test muss der Aufrufer explizit eine
Freigabe mit Grund erzeugen und den separaten Live-Pfad verwenden:

```python
live_gate = ExternalLiveRequestGate.explicit("begründeter Test")
control.live_request("groq", route, request, live_gate=live_gate)
```

`dry_run_provider_request()` zeigt Provider, Modell, Credential-Status,
Privacy, Health und Endpoint ohne Netzwerkzugriff. `HealthSynchronizer` bündelt
seit Phase 9E die Health-Synchronisierung für Ollama, Groq und Gemini in der
bestehenden Registry. Ollama wird lokal über `/api/tags` geprüft; Groq und
Gemini verwenden nur mit explizitem Gate ihre jeweiligen Model-Discovery-
Endpoints. Ohne Gate bleibt der externe Zustand `not_checked` und es entstehen
null externe Requests. Authentifizierungs-, Rate-Limit-, Modell- und Netzwerk-
fehler werden in die vorhandenen Registry-/Circuit-Breaker-Zustände übersetzt.
Kein Startup, Registry-Laden oder normales Routing führt einen externen Probe-
oder Modellrequest aus.

## Provider-Diagnose (Phase 9F)

Die Diagnose ist standardmäßig vollständig netzwerkfrei und verwendet nur die
Registry-/Credential-Metadaten:

```powershell
python arena_launcher.py diagnostics --dry-run
python arena_launcher.py diagnostics --dry-run --provider ollama --json
python arena_launcher.py diagnostics --dry-run --provider groq --json
```

Ein Health-Check darf nur mit einer expliziten Begründung und dem bestehenden
Live-Gate gestartet werden:

```powershell
python arena_launcher.py diagnostics --live --reason "manueller Health-Test" --provider groq
```

`--live` ohne `--reason` wird mit Exit-Code `3` abgewiesen. Der Live-Modus
prüft ausschließlich die ausgewählten Adapter, führt keine Retries und keine
Fallback-Kaskade aus. Provider ohne gesetzte Credentials bleiben
`not_configured`; ihre Requests werden nicht gesendet. `--json` liefert nur
sanitized Registry-Daten ohne API-Keys oder Authorization-Header.

Exit-Codes:

```text
0 = Diagnose erfolgreich
1 = Live-Provider nicht verfügbar oder fehlerhaft
2 = ungültige Argumente oder Konfiguration
3 = Live-Modus ohne explizite Freigabe
```

## Provider Diagnostics Dashboard (Phase 9G)

Die bestehende Tkinter-Shell enthält einen zusätzlichen Tab `Provider
Diagnostics`. Beim Öffnen wird ausschließlich `ArenaControl.provider_diagnostics()`
ohne Live-Gate verwendet; dadurch bleibt der initiale Aufruf netzwerkfrei.
Angezeigt werden Status, Credential-Status, Adapter, Modelle, Circuit Breaker,
Retry-After, Network und die letzte sanitizte Fehlermeldung. Provider ohne
Transportadapter bleiben sichtbar und werden als `unavailable` markiert.

`Refresh Diagnostics (Dry Run)` bleibt netzwerkfrei. `Live Check Selected`
erfordert die Auswahl genau eines Providers und eine explizite Bestätigung.
Erst danach wird das bestehende `ExternalLiveRequestGate.explicit(...)` verwendet.
Es gibt keinen automatischen Providerwechsel, keine Retries und keine
Modellanfrage. Die Privacy-Klassen bleiben sichtbar; `PRIVATE` und `SECRET`
werden nicht automatisch an externe Provider geroutet.

Start der Shell:

```powershell
python arena_app.py --config arena-config.json
```

## Provider Health History (Phase 9H)

Health-Checks und relevante Provider-/Circuit-Breaker-Zustandswechsel werden in
einer begrenzten lokalen Datei unter dem konfigurierten Arena-State gespeichert:

```text
state/arena/provider-health-history.json
```

Gespeichert werden ausschließlich sanitizte Metadaten wie Zeit, Provider,
Event-Typ, Health-Status, Statuscode, Retry-After, Circuit-State und eine kurze
technische Meldung. Prompts, Modellantworten, Request-Bodies und Credentials
gehören nicht zum Event-Modell.

Das Dashboard bietet:

```text
Provider Health History
All Providers / Ollama / Groq / Gemini
All Events / Errors / Rate Limits / Circuit Breaker / Health Checks
Refresh History
Clear History
Export Diagnostics
```

Das Laden, Filtern, Löschen und Exportieren der History ist lokal und erzeugt
keine Provider-Requests. Ein Health-Poller ist nicht implementiert.

## Provider Health Trends / Alerts (Phase 9I)

`ProviderHealthAnalyzer` wertet ausschließlich die vorhandene lokale History aus
und erzeugt keine Netzwerkverbindung. Unterstützte Zeitfenster sind `1h`, `6h`,
`24h` und `7d`; Trends zählen unter anderem erfolgreiche Checks, Ausfälle,
Rate-Limits, Authentifizierungsfehler, Modellunverfügbarkeit und
Circuit-Breaker-Öffnungen. Provider können dabei miteinander verglichen und
Events über die bestehenden History-Filter eingeschränkt werden.

Die Alert-Policy ist bewusst lokal und informativ. Sie meldet nur wiederholte
Ausfälle, Rate-Limits, Authentifizierungsfehler oder Circuit-Breaker-Öffnungen
über konfigurierbaren Schwellenwerten. Es gibt keinen E-Mail-Versand, Webhook,
automatischen Fallback, Key-Lock oder Provider-Request. Alert- und Trenddaten
enthalten nur sanitizte Health-Metadaten. Das Dashboard zeigt sie kompakt unter
`Health Trends / Alerts`; `Network: NO` bleibt dabei garantiert.

## Lokale Alert-Filter, Bulk-Aktionen und Report (Phase 9K)

Die Alert-Ansicht kann lokal nach Provider, Status, Severity, Alert-Typ und
Zeitfenster gefiltert werden. Mehrfachauswahl unterstützt `Acknowledge Selected`,
`Suppress Selected` mit `15m`, `1h`, `6h` oder `24h` sowie `Resolve Selected`.
Jede Aktion betrifft ausschließlich explizit ausgewählte Alert-IDs; für Resolve
ist eine Benutzerbestätigung erforderlich.

`provider_health_report()` erzeugt aus Diagnostics, History, Trends und
Lifecycle-Zuständen einen lokalen Report. Er kann als JSON oder als
menschenlesbarer TXT-Report exportiert werden. Filter, Bulk-Aktionen,
Report-Erzeugung und Export erzeugen keine Netzwerkverbindung.

## Alert-Lifecycle und lokaler Diagnoseexport (Phase 9J)

Alert-Zustände werden lokal und begrenzt unter
`state/arena/provider-alert-state.json` gespeichert. Die stabile Alert-ID wird
nur aus Provider, Alert-Typ und Zeitfenster gebildet. Unterstützt werden:

```text
Acknowledge
Suppress: 15m / 1h / 6h / 24h
Clear / Resolve
```

Diese Zustände verändern weder Routing, Providerstatus, Circuit Breaker noch
Credentials. Unterdrückung betrifft ausschließlich die lokale Darstellung.
Es gibt weiterhin keinen Poller, keine automatische Resolution und keine
Außenkommunikation.

`export_provider_diagnostics()` erzeugt einen lokalen JSON-Export mit
Provider-Diagnose, History, Trends, Alerts und Alert-Lifecycle-Zuständen. Der
Export ist sanitiziert und enthält keine Keys, Tokens, Prompts,
Modellantworten, Request-Bodies oder Authorization-Header.

## Offline CLI Reports und Archivrotation (Phase 9L)

Der Launcher bietet lokale Reports ohne Health-Check oder Providerkontakt:

```powershell
python arena_launcher.py report
python arena_launcher.py report --window 24h --format text
python arena_launcher.py report --provider groq --window 6h --format json
python arena_launcher.py report --format json --output report.json
```

Vorhandene Report-Zieldateien werden nicht überschrieben. Die manuelle
Archivrotation verwendet:

```powershell
python arena_launcher.py archive --history
python arena_launcher.py archive --alerts
python arena_launcher.py archive --all --output .\archive
```

Ohne Auswahl archiviert `archive` beide lokalen Dateien. Erst nach erfolgreicher
JSON-Erzeugung und Validierung wird die aktive begrenzte Datei geleert. Bei
Fehlern bleiben die Quelldaten erhalten. Die aktive Grenze von 100 Health-Events
bleibt unverändert. Es gibt keine automatische Rotation und keinen Hintergrund-
Poller.

## Verwendung

Aus diesem Ordner:

```powershell
# Nur Runtime und Pfade prüfen, nichts starten
python arena_launcher.py --config arena-config.json --check

# Desktop dauerhaft starten
python arena_launcher.py --config arena-config.json

# Isolierter Smoke-Test: Runtime, HTTP, WebSocket-Gate, State und Registry
python arena_launcher.py --config arena-config.json --smoke --json
```

Der Desktop führt seinen eigenen authentifizierten WebSocket-Probe während des
Bootablaufs durch. Der Launcher führt keinen Modellrequest aus. Die Provider-
Registry wird dabei nur geladen und auf fehlende Konfiguration geprüft.

## Aktueller Stand

- `arena_version.py`: zentrale Anwendungsversion `v0.9.0`
- `CredentialStore`: Environment-basierte Credentials, optional Doppler-injiziert
- `arena_runtime.py`: standard-library-only Runtime-Abstraktion
- `arena_launcher.py`: fail-closed Start-, Check- und Smoke-Test-Modi
- `arena_api.py`: Arena-Produktsteuerung oberhalb der Hermes-Runtime
- `arena_state.py`: atomarer Arena-Metadaten-State, getrennt von Hermes
- `arena_projects.py`, `arena_sessions.py`, `arena_agents.py`: lokale Metadaten-CRUDs
- `arena_providers.py`: zentraler Providerkatalog, Privacy und Health-Metadaten
- `arena_health.py`: zentrale gated Health-Synchronisierung für Ollama, Groq und Gemini
- `ArenaControl.provider_diagnostics()`: sanitizte Dry-Run-/Live-Diagnose der Provider
- `arena_launcher.py diagnostics`: expliziter CLI-Diagnosemodus mit JSON-Ausgabe
- `ProviderDiagnosticsDashboard`: dünne Tkinter-Darstellung ohne eigene Providerlogik
- `test_provider_dashboard.py`: headless UI-/Gate-/Darstellungs-Fixtures
- `arena_history.py`: begrenzte lokale ProviderHealthEvent-History mit Sanitization
- `arena_trends.py`: offline Trendaggregation und lokale Alert-Policy
- `arena_alerts.py`: lokaler Alert-Lifecycle mit Acknowledge/Suppression/Resolve und Filtern
- `arena_reports.py`: sanitizte Offline-Reports in JSON/TXT
- `arena_archive.py`: manuelle lokale Archivierung und sichere History-Rotation
- `test_phase9l.py`: CLI-, Archiv-, Validierungs- und Zero-Network-Fixtures
- `test_health_history.py`: History-, Persistenz-, Filter-, Export- und Sicherheits-Fixtures
- `test_phase9k.py`: Alert-Filter-, Bulk-, Report- und Zero-Network-Fixtures
- `test_alert_lifecycle.py`: Alert-ID-, Lifecycle-, Persistenz- und Export-Fixtures
- `test_health_trends.py`: Zeitfenster-, Schwellenwert-, Filter- und Zero-Network-Fixtures
- `test_health_sync.py`: Phase-9E Health-, Gate-, Circuit-Breaker- und Routing-Fixtures
- `arena_credentials.py`: Secret-sichere Environment-Referenzen
- `arena_router.py`: deterministisches Capability-/Privacy-/Fallback-Routing
- `arena_transport.py`: gemeinsamer Transportvertrag, Ollama-, OpenAI-kompatibler und Gemini-Adapter
- `test_gemini_transport.py`: Gemini-Fixture-, Gate-, Privacy- und Fehler-Regressionen
- `test_openai_transport.py`: Groq/OpenAI-kompatible Fixture- und Privacy-Tests
- `test_live_gate.py`: Phase-9C-Gate-, Dry-Run- und Zero-Network-Tests
- `arena_app.py`: optionale schlanke Arena-Shell
- `test_arena_client.py`: Metadata-/Konfigurationstests
- `test_provider_routing.py`: Provider-/Routing-/Circuit-Breaker-Fixtures
- Hermes Desktop wird nicht geforkt oder gepatcht
- globale Profile, Sessions, Datenbanken, Memories und Credentials werden nicht
  übernommen
- keine Provider- oder Modellrequests gehören zum Registry-/Routing-Smoke-Test

## `hermes-os/hermes.py`

Der frühere Tkinter-/Ollama-Prototyp bleibt als historischer Prototyp erhalten.
Er ist nicht die primäre Arena UI: Der nachgewiesene Hermes Desktop besitzt die
vollständige Desktop-/Backend-/WebSocket-Kette. Der Prototyp wird in dieser
Phase weder gelöscht noch migriert.
