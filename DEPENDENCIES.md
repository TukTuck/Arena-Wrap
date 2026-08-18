# DEPENDENCIES.md — Lokale System-Abhängigkeiten (Hinweis-Dokument)

> **Zweck:** Dieses Dokument beschreibt, **welche lokalen Komponenten auf dieser Maschine
> voneinander abhängen, warum sie so verdrahtet sind und — besonders wichtig — wer was
> überschreibt**. Es ist ein **Hinweis/Plan-Dokument**, keine Anleitung zum Nachbau.
> Stand: 2026-08-17, nach der Wiederherstellung (Reinstall von Codex Web GPT 2.1.11).

---

## 1. System-Übersicht

```text
Traycer (VS-Code-Erweiterung / Agent-Host)
  └─ nutzt Harness: Codex CLI 0.147.0
       │  liest:  ~/.codex/config.toml, ~/.codex/auth.json
       │  Default-Modell: chatgpt-web/luna
       ▼
Codex Web GPT App (Launcher, v2.1.11)
  ├─ Config:  ~/.codex-chatgpt-web/  (config.json, versions/, runtime/, tunnel/, secrets/)
  ├─ Profil:  %APPDATA%\Codex Web GPT  (Browser-Login-Session, launcher-state.json)
  ▼
Bridge  http://127.0.0.1:17841/v1   (NUR Responses-API; verlangt Bearer-Auth)
  ▼
ChatGPT Web (eingebetteter Browser)
  └─ Full-Harness: MCP-Connector „Codex Native3" ↔ OpenAI-Tunnel ↔ Codex-Task-Tools

OmniRoute (npm global, v3.8.49) — lokaler AI-Router
  ├─ Server:  omniroute serve  →  http://127.0.0.1:20128  (Dashboard + API /v1)
  ├─ Daten:   ~/.omniroute/  (storage.sqlite, .env, config.json, logs/, server/.pid)
  ├─ Konsumenten: Arena (arena_transport), Codex-Claude-Pfad (ANTHROPIC_BASE_URL),
  │              schaltwerk/omni-proxy-exchange (Management-UI)
  └─ CLI-Kontexte: ~/.omniroute/config.json  („default" → 20129 E-OmniRoute; legacy → localhost:0)

Ollama (lokal, Port 11434) — lokale Modelle (z. B. qwen3:4b-cpu)
  ├─ Arena (OllamaTransport)
  └─ Kann von OmniRoute als Modell-Backend geroutet werden (ollama/…)

Doppler (optional) → Environment-Variablen (GROQ_API_KEY, GOOGLE_API_KEY)
  └─ Arena CredentialStore → Provider-Adapter (Ollama/Groq/Gemini)
```

---

## 2. Wer liest / schreibt / überschreibt was

| Komponente | liest | schreibt / überschreibt |
|---|---|---|
| **Codex CLI** | `~/.codex/config.toml`, `~/.codex/auth.json` | — (nur Sitzungsdaten) |
| **codex-chatgpt-web App** | `config.toml`, `~/.codex-chatgpt-web/`, `%APPDATA%\Codex Web GPT` | **`config.toml`-Kopf** („Install models"), Browser-Session, `launcher-state.json` |
| **Bridge 17841** | `~/.codex-chatgpt-web/` (config, secrets, tunnel) | `responses-state.json`, Browser-Session |
| **OmniRoute serve** | `~/.omniroute/` (storage.sqlite, .env, config.json) | storage.sqlite, Logs, **`server/.pid`-Lock** |
| **Schaltwerk** (omni-proxy-exchange) | `omni_url` (Default 20128) | OmniRoute-Registry (Proxies per Management-API) |
| **Doppler** | — | injiziert Environment-Variablen |
| **Ollama** | — | — |

---

## 3. Abhängigkeiten im Detail (was → warum)

1. **Codex CLI → `~/.codex/config.toml`**
   Der globale Wert `openai_base_url` bestimmt den **Default-Provider** des Codex CLI.
   Aktuell: `http://127.0.0.1:17841/v1` → alle Codex-Modellanfragen gehen an die
   ChatGPT-Web-Bridge. `model = "chatgpt-web/luna"` ist das Standard-Modell.

2. **Codex CLI → `~/.codex/auth.json`**
   Die Bridge verlangt zwingend einen Bearer-Token („Native Codex passthrough requires
   the incoming Bearer authorization"). Codex liefert diesen automatisch aus `auth.json`
   (id_token/access_token/refresh_token). **Ohne gültige Tokens → HTTP 502.**

3. **Bridge 17841 → ChatGPT-Web-Session**
   Die Bridge fährt einen eingebetteten Browser; die Login-Session liegt in
   `%APPDATA%\Codex Web GPT`. Die App verwaltet diese Session selbst (inkl. Quarantäne
   beim Deinstallieren, siehe §5).

4. **Bridge spricht NUR die Responses-API**
   `/v1/chat/completions` → **404**. Es funktioniert nur `/v1/responses`
   (bzw. `/v1/models`). Clients, die den Legacy-Chat-Endpunkt erwarten, funktionieren
   nicht gegen die Bridge.

5. **Codex → OmniRoute (Claude-Pfad)**
   In `config.toml` unter `[shell_environment_policy.set]`:
   `ANTHROPIC_BASE_URL = "http://localhost:20128"`, `ANTHROPIC_MODEL = "auto"`.
   Damit laufen Claude/Anthropic-Subprozesse, die Codex startet, **durch OmniRoute**
   (Routing, Fallback, Proxies) statt direkt zu Anthropic.

6. **Arena → OmniRoute (20128)**
   `arena-client` (Transport-Schicht) spricht direkt den OpenAI-kompatiblen Endpunkt
   `http://127.0.0.1:20128/v1`. Arena ist damit **unabhängig** von `config.toml` —
   OmniRoute kann laufen, auch wenn Codex auf die Bridge zeigt.

7. **OmniRoute → `~/.omniroute/`**
   Server-Daten (SQLite, `.env`, Kontexte, Logs). Der **`server/.pid`-Lock** wird beim
   Start geschrieben; ein abgebrochener Prozess hinterlässt einen **stale Lock**, der
   den nächsten Start blockiert (kein Port, keine Logs) → Lock entfernen, wenn die PID tot ist.

8. **Schaltwerk → OmniRoute**
   `schaltwerk/omni-proxy-exchange` (Management-UI, Port 8765) liest/schreibt
   OmniRoute-Registry-Einträge (Proxies) über die Management-API. Default-URL: 20128.

9. **Doppler → Arena-Credentials**
   Doppler ist ausschließlich **Environment-Injektor** (GROQ_API_KEY, GOOGLE_API_KEY).
   Die Provider-Adapter lesen nur die Environment-Variablen — keine Doppler-Abhängigkeit
   im Code. Das `ExternalLiveRequestGate` bleibt maßgeblich (Credential vorhanden +
   Gate deaktiviert = 0 externe Requests).

10. **Traycer → Codex-Harness**
    Traycer führt Agents über den `codex`-Harness aus; Provider-Konten stehen in
    `~/.traycer/host/config/provider-accounts.json`. Traycer ist **nicht** an die
    Bridge gekoppelt, nutzt aber denselben Codex CLI.

---

## 4. Überschreib-/Konflikt-Beziehungen (das Wichtigste)

### 4.1 codex-chatgpt-web überschreibt `config.toml` — erwartetes Verhalten
Die App **verwaltet** den Kopf von `~/.codex/config.toml`. Beim Klick auf
**„Install models"** werden u. a. überschrieben/ergänzt:

```toml
model = "chatgpt-web/luna"
model_reasoning_effort = "low"
openai_base_url = "http://127.0.0.1:17841/v1"
[model_providers.fcc]            # Free Claude Code (separate Bridge, Port 8082)
[features]                       # remote_compaction_v2 / multi_agent / multi_agent_v2
```

Kennzeichnung in der Datei:
`# Managed by codex-chatgpt-web; \`codex-chatgpt-web uninstall\` restores prior values.`

**Konsequenz:** Manuelle Änderungen am App-verwalteten Kopf werden bei der nächsten
„Install models"-Aktion überschrieben. OmniRoute-Default und Bridge-Default schließen
sich am globalen `openai_base_url` gegenseitig aus (genau EIN Default-Provider).
**Koexistenz nur über Nebenwege:** OmniRoute via `ANTHROPIC_BASE_URL` (Claude) und als
eigener Server für Arena.

### 4.2 Bridge erwartet Bearer → wer kein Token hat, bekommt 502
`/v1/models` ohne Bearer → **502** („requires the incoming Bearer authorization").
Gegen die Bridge kommen nur Clients mit Sitzung durch (Codex CLI mit `auth.json`).
Einfache curl-Tests ohne Token scheitern also erwartungsgemäß.

### 4.3 OmniRoute-Port: 20128 (Server) vs. 20129 (CLI-Kontext) — Verwechslungsquelle
- **Server/Gateway:** 20128 (Dashboard + `/v1`), von Arena + Schaltwerk + Codex-Anthropic genutzt.
- **CLI-Kontext „default"** in `~/.omniroute/config.json`: `http://localhost:20129`
  („E-OmniRoute 20129 (OpenRouter Free)") — das ist der Endpunkt, den die **CLI** anspricht.
- Zusätzlich: 20131 (EmbedWsProxy), 20132 (LiveWS), 8765 (Schaltwerk-UI).
- **Regel:** Wenn ein Dienst auf „20128 soll OmniRoute sein" zeigt, meint das den Server.
  Der 20129-Kontext ist nur die CLI-Sicht und darf nicht mit dem Server-Port verwechselt werden.

### 4.4 OmniRoute-Stale-Lock (`~/.omniroute/server/.pid`)
Abgebrochene `omniroute serve`-Prozesse hinterlassen eine `.pid`. Der nächste Start
hängt dann still (kein Port, keine neuen Logs). Behebung: Lock-Datei entfernen,
wenn die enthaltene PID tot ist, dann neu starten.

### 4.5 WebSocket-Fallback (426 Upgrade Required)
Codex versucht zuerst `ws://127.0.0.1:17841/v1/responses` → Bridge antwortet
**426 Upgrade Required** → Codex fällt auf HTTP-Polling zurück. Funktioniert,
hinterlässt aber eine Fehlerzeile im Codex-Log. Kein Blockierer, aber bekanntes
Verhalten der Bridge in 2.1.11.

### 4.6 Deinstallation der App ist reversibel (Quarantäne)
Der Uninstaller bzw. „Remove Codex integration" der App **löscht nicht einfach**,
sondern verschiebt sensible Zustände in Quarantäne-Ordner:

```text
~/codex-chatgpt-web-quarantine-<zeitstempel>/
  ├─ AppData-Roaming-Codex-Web-GPT/   (Browser-Login, launcher-state.json)
  ├─ dot-codex-chatgpt-web/           (App-Config inkl. Versionen/Tunnel)
  ├─ local-state-tunnel-client/       (Tunnel-Client-State)
  └─ codex-debug-tunnel-mcp/          (MCP-Debug)
~/codex-app-profile-quarantine-<zeitstempel>/Codex   (Browser-Profil)
```

**Wiederherstellung:** App neu installieren → Quarantäne-Inhalte an ihre Zielorte
zurückkopieren → Login-Session ist wieder da (kein erneuter Login nötig).

### 4.7 Backup-Dateien mit irreführenden Namen
| Datei | Inhalt (real) |
|---|---|
| `~/.codex/config.toml.pre-chatgpt-web.bak` | **chatgpt-web-INTEGRIERTER** Zustand (trotz Namens — vor dem OmniRoute-Reset gesichert) |
| `~/.codex/config.toml.omniroute-default.bak` | OmniRoute-Default-Zustand (modell + base_url 20128) |

---

## 5. Port-Belegung (Stand 2026-08-17)

| Port | Dienst |
|---|---|
| 11434 | Ollama |
| 17841 | codex-chatgpt-web Bridge (Responses-API) |
| 20128 | OmniRoute Server (Dashboard + API `/v1`) |
| 20129 | OmniRoute CLI-Kontext „E-OmniRoute" (OpenRouter Free) |
| 20131 / 20132 | OmniRoute EmbedWsProxy / LiveWS |
| 8765 | Schaltwerk (omni-proxy-exchange) Management-UI |
| 8082 | Free-Claude-Code-Bridge (fcc) — aktuell nicht aktiv |

---

## 6. Empfohlene Regeln (Hinweise für die weitere Arbeit)

1. **Vor jedem „Install models"-Klick oder App-Update:** `~/.codex/config.toml` UND
   `~/.codex-chatgpt-web/` sichern (die App überschreibt den Config-Kopf).
2. **Quarantäne- und Backup-Ordner nicht löschen** — sie sind die Wiederherstellungsquelle
   für Login-Session und App-State.
3. **Nach abgebrochenem `omniroute serve`:** `~/.omniroute/server/.pid` prüfen und
   bei toter PID entfernen, sonst blockiert der Lock den Neustart.
4. **OmniRoute-Default und ChatGPT-Web-Default nicht im selben `openai_base_url` mischen**
   (exklusiv). Falls beide als Codex-Default gebraucht werden: separaten Codex-Home-Profile
   (`CODEX_HOME`) verwenden statt den App-verwalteten Kopf zu editieren.
5. **Tests gegen die Bridge** immer über den Codex CLI (Bearer + Responses-API) führen,
   nicht per curl auf `/chat/completions`.
6. **Keine Secrets in dieses Dokument oder in Repo-Dateien** — Credentials nur über
   Environment/Doppler referenzieren.
