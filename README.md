# Arena Wrap — Nebenstrang-Verwaltung

**Arena Version:** `v0.9.0`

**Status:** Stable Baseline

Dieses Repo bündelt die **Nebenstränge** (Seitenprojekte) des Arena-Wrap-Projekts.
Regel ab sofort: **Jeder Nebenstrang liegt in einem eigenen Ordner UND wird auf
einem eigenen Branch weiterentwickelt.**

## 📄 Lokale System-Abhängigkeiten (Hinweis)

Die Verdrahtung der lokalen Dienste — **Codex CLI ↔ ChatGPT-Web-Bridge (17841) ↔
OmniRoute (20128) ↔ Ollama (11434)** sowie **wer was an Konfiguration überschreibt** —
steht in **[DEPENDENCIES.md](./DEPENDENCIES.md)**. Vor Änderungen an
`~/.codex/config.toml`, an OmniRoute oder an der Bridge bitte lesen (App-verwaltete
Config, Stale-Lock `~/.omniroute/server/.pid`, Port-Verwechslungsgefahr 20128/20129,
Quarantäne-Wiederherstellung).

## 📁 Struktur

```
Arena Wrap/                         ← Git-Repo (Branch: main)
│
├── README.md                       ← diese Übersicht
├── .gitignore
├── arena-client/                   ← Stand-alone-Arena-Client-Grundlage
│   ├── arena_launcher.py           ← fail-closed Desktop-Launcher
│   ├── arena_runtime.py            ← Runtime-Validierung und Health-Checks
│   ├── arena_api.py                ← Arena-Produktsteuerung
│   ├── arena_providers.py          ← Provider Registry / Health-Metadaten
│   ├── arena_credentials.py        ← Secret-sichere Env-Referenzen
│   ├── arena_router.py             ← Privacy-/Capability-/Fallback-Routing
│   ├── arena_state.py              ← eigener Arena-Metadaten-State
│   ├── arena_projects.py           ← Projekt-Metadaten-CRUD
│   ├── arena_sessions.py           ← Session-Metadaten-CRUD
│   ├── arena_agents.py             ← lokale Agent-Metadaten
│   ├── arena_app.py                ← schlanke Arena-Control-Shell
│   ├── test_arena_client.py        ← modellfreier Test
│   ├── arena-config.example.json   ← portable Konfigurationsvorlage
│   └── README.md
│
├── schaltwerk/                     ← NEBENSTRANG 1 · Branch: feature/schaltwerk
│   └── omni-proxy-exchange/        ← Omni-Proxy-Server (Python, lokal)
│       ├── server.py
│       ├── requirements.txt
│       ├── STARTEN.bat
│       └── static/index.html
│
└── privategpt-sprachassistent/     ← NEBENSTRANG 2 · Branch: feature/privategpt
    ├── start-private-gpt.ps1       ← PrivateGPT-Start/Stop/Repair (Port 8080)
    ├── sprechen.ps1                ← Sprachausgabe (Katja-Neural-Stimme)
    ├── private-gpt-upload.ps1      ← Drag&Drop-Uploader für Dokumente
    ├── Modelfile.gpu10             ← Ollama-Modellvariante (69% CPU / 31% GPU)
    ├── Modelfile.cpu               ← Ollama-Modellvariante (100% CPU)
    ├── private-gpt-patches/        ← Windows-Patches (fcntl, magic, qdrant)
    ├── diktat/                     ← Diktat + Hotkey + Assistent
    │   ├── diktat.py · diktat.bat
    │   ├── sprachassistent.py · sprachassistent.bat
    │   ├── hotkey.py · hotkey.bat
    │   ├── PrivateGPT-Assistent.bat.deaktiviert   ← Autostart (aus)
    │   └── sounds/                 ← weiche Signal-Töne (start/stop)
    └── testdokumente/              ← Beispieldokumente für RAG-Tests
        ├── kaffeekraft_test.txt · kaffeekraft_handbuch.txt
        └── ingest_doc.py · ingest_handbuch.py
```

## 🌿 Branch-Modell

```
main ────────────────────────────── Basis: Struktur, README, .gitignore
 ├── feature/schaltwerk            Weiterentwicklung des Schaltwerk-Nebenstrangs
 └── feature/privategpt            Weiterentwicklung des PrivateGPT+Stimme-Nebenstrangs
```

- **main** enthält immer **alle Ordner** (man sieht stets alles).
- **feature/*-Branches** sind die Entwicklungslinien je Nebenstrang:
  Änderungen nur im eigenen Ordner, dann Merge zurück nach main.

## ⚙️ Maschinen-spezifische Pfade (nicht im Repo)

Die Python-Umgebung (`diktat\.venv`) liegt **außerhalb des Repos** unter
`C:\Users\Hansi\diktat\.venv` und wird von den Skripten über absolute Pfade
referenziert. Sie ist in `.gitignore` ausgeschlossen — bei einem neuen Rechner
wird sie neu aufgebaut (siehe `diktat/requirements.txt`-Hinweis im Ordner).

## 🧭 Arena Stand-alone Client

`arena-client/` ist die neue Produktgrenze für Arena. Der Launcher startet Hermes
Desktop ausschließlich mit einem expliziten Arena-Checkout, einer eigenen
Virtualenv, einem eigenen `HERMES_HOME` und eigenen Electron-Daten. Fehlt ein
Pfad, bricht er ab; globale Hermes-Defaults werden nicht verwendet.

```powershell
cd arena-client
python arena_launcher.py --config arena-config.json --check
python arena_launcher.py --config arena-config.json --smoke --json
```

Die portable Runtime-Struktur, der Provider-Pool und alle Voraussetzungen stehen in
`arena-client/README.md`. Phase 8 registriert Provider ohne Keys anzulegen und blockiert
externe Provider standardmäßig für PRIVATE/SECRET-Daten. Phase 9A enthält den
lokalen Ollama-Transport; Phase 9B den OpenAI-kompatiblen Groq-Fixture-Transport
ohne automatische externe Requests. Phase 9C hält externe Requests zusätzlich
über ein standardmäßig deaktiviertes Explicit Live Request Gate gesperrt. Der frühere
`hermes-os/hermes.py`-Tkinter-Prototyp bleibt
als Legacy/Prototype erhalten und ist nicht die primäre Arena UI. Doppler ist
optional und darf ausschließlich die bestehenden Environment-Variablen für
Provider injizieren; das ExternalLiveRequestGate bleibt unabhängig maßgeblich.

## 🚀 Kurzanleitung PrivateGPT-Nebenstrang

```powershell
# PrivateGPT starten (inkl. Ollama-Check + Windows-Patches)
powershell -ExecutionPolicy Bypass -File privategpt-sprachassistent\start-private-gpt.ps1 -Action Start
# UI: http://localhost:8080/ui  (Modell qwen3:4b, Collection test_de_lang)

# Diktat (Enter = Start/Stop, Ergebnis → Zwischenablage)
privategpt-sprachassistent\diktat\diktat.bat

# Sprachassistent (Diktat → RAG-Frage → Antwort vorlesen)
privategpt-sprachassistent\diktat\sprachassistent.bat

# Globaler Hotkey (Strg+Umschalt+D = Diktat+Frage+Antwort, Strg+Umschalt+X = Ende)
privategpt-sprachassistent\diktat\hotkey.bat

# Sprachausgabe (liest Argument oder Zwischenablage vor)
powershell -ExecutionPolicy Bypass -File privategpt-sprachassistent\sprechen.ps1 "Hallo"
```

## 🚀 Kurzanleitung Schaltwerk-Nebenstrang

```bat
schaltwerk\omni-proxy-exchange\STARTEN.bat
```
Details stehen in `schaltwerk\omni-proxy-exchange\README.md`.

## 🔒 Nicht versioniert (bewusst ausgeschlossen)

- `.freebuff/`, `Chats/`, `archive/`, `Arena Wrap.code-workspace` — Arbeitsumgebung
  und Brain-Dokumentation (Thema des nächsten Planungs-Schritts)
- `omni-proxy-exchange/` — leerer, gesperrter Restordner (nach Freigabe löschbar)
- `.venv/`, `__pycache__/`, Logs, TTS-Test-MP3s
