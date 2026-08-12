# Arena Wrap — Nebenstrang-Verwaltung

Dieses Repo bündelt die **Nebenstränge** (Seitenprojekte) des Arena-Wrap-Projekts.
Regel ab sofort: **Jeder Nebenstrang liegt in einem eigenen Ordner UND wird auf
einem eigenen Branch weiterentwickelt.**

## 📁 Struktur

```
Arena Wrap/                         ← Git-Repo (Branch: main)
│
├── README.md                       ← diese Übersicht
├── .gitignore
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
