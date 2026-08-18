# Arena Wrap — Plan: Kurzzeitziel + Überarbeitung der Gesamtziele

> Stand: 2026-08-13 | Status: Entwurf zur gemeinsamen Abstimmung
> Sortierung: **Jeder Nebenstrang = eigener Ordner + eigener Branch** (siehe README.md)

---

## 1. Kurzzeitziel (nächste 1–2 Wochen)

**„Der lokale Arbeitsplatz läuft Ende-zu-Ende und ist abgesichert."**

Die Bausteine existieren und sind getestet — jetzt geht es um Konsolidierung:

| # | Schritt | Warum | Aufwand |
|---|---|---|---|
| 1 | `feature/privategpt`-Branch für Weiterentwicklung nutzen | Neue Ideen nicht auf main verwalten | gering |
| 2 | Beide Testdokumente in **eine** Collection (`test_de_lang`) konsolidieren | Kein Collection-Wechsel in der UI nötig | gering |
| 3 | TTS ohne Media-Player-Fenster (direkt abspielen) | Kein lästiges „Filme & TV"-Fenster mehr | mittel |
| 4 | Autostart optional reaktivieren (Datei liegt bereit) | Nach Entscheidung ein Kopier-Schritt | gering |
| 5 | `schaltwerk`: `STARTEN.bat`-Ablauf auf dem aktuellen Stand verifizieren | Nebenstrang 1 wieder lauffähig dokumentieren | mittel |

**Abbruchkriterium:** PrivateGPT + Sprach-Assistent starten aus dem Repo heraus
mit einem Befehl und laufen stabil — dokumentiert im `feature/privategpt`-Branch.

---

## 2. Überarbeitung der Gesamtziele (Brain)

Abgeleitet aus `archive/INDEX.md`, den ADRs und der bisherigen Arbeit.
**Neu:** Jedes Ziel bekommt einen Status + einen zugeordneten Nebenstrang/Branch.

### Kritisch
| Ziel | Status | Nebenstrang |
|---|---|---|
| OmniRoute + Hermes Integration (ADR-001/002) | offen | *(geplant: eigene Ordner+Branches)* |
| Bad Wolf Cooperation | offen | *(geplant)* |

### Hoch
| Ziel | Status | Nebenstrang |
|---|---|---|
| Proxy-Infrastruktur (ADR-004 Free-Tier) | teilweise — `schaltwerk` | `feature/schaltwerk` ✅ |
| Management Agent (ADR-005 Read-Only) | offen | *(geplant)* |

### Mittel
| Ziel | Status | Nebenstrang |
|---|---|---|
| Lesezeichen-Recherche-Pipeline | offen | *(geplant)* |
| Hardware & lokale Modelle (ADR-006) | **weitgehend realisiert** → abschließen | `feature/privategpt` ✅ |
| **NEU: Lokaler Sprach-Assistent mit RAG** (Diktat → Frage → Antwort vorlesen) | **realisiert** (Hotkey, qwen3:4b, bge-m3, Katja) | `feature/privategpt` ✅ |

### Niedrig
| Ziel | Status | Nebenstrang |
|---|---|---|
| Video-Pipeline | offen | *(geplant)* |

---

## 3. Brain-Struktur (gleiche Sortierung wie im Repo)

Damit „Brain" und Repo dieselbe Logik bekommen:

```
Brain/ (Zukunft, ersetzt bzw. speist archive/)
├── main                ← Übersicht, Glossar, ADRs, Chronologie
├── schaltwerk/         ← Ordner + Branch feature/schaltwerk
├── privategpt-sprachassistent/ ← Ordner + Branch feature/privategpt
└── bad-wolf/ · omniroute-hermes/ · management-agent/ · …  (je Ziel ein Ordner + Branch)
```

Konkret für den nächsten Schritt:
1. `archive/` (Brain-Doku) nach gleicher Regel in eigene Ordner je Thema gliedern
2. Jedes kritische/hohe Ziel bekommt einen eigenen Branch (auch wenn der Ordner
   noch leer ist) — damit die Regel ab sofort lückenlos gilt
3. Diese Plan-Datei wird danach in die Brain-Struktur übernommen

---

## 4. Offene Entscheidungen

- [ ] Kurzzeitziel-Schritte 1–5 bestätigen / priorisieren
- [ ] Autostart: dauerhaft aus (Status quo) oder reaktivieren?
- [ ] Welche Ziele bekommen als Nächstes einen eigenen Branch (Bad Wolf? OmniRoute/Hermes?)
- [ ] Soll die Brain-Doku (`archive/`) ins Repo wandern oder separat bleiben?

---

## 5. Hinweis — Lokale System-Abhängigkeiten

> Siehe **`DEPENDENCIES.md`** (Repo-Root) — vollständige Übersicht, wer was auf dieser
> Maschine liest/schreibt/überschreibt. Stand: 2026-08-17.

Kurzfassung für die weitere Arbeit:

- **Codex-chatgpt-web verwaltet den Kopf von `~/.codex/config.toml`** („Install models"
  überschreibt `model`/`openai_base_url`/fcc/features). Vor jedem Install-models-Klick
  oder App-Update: Config + `~/.codex-chatgpt-web/` sichern.
- **OmniRoute:** Server = Port 20128 (Arena + Claude-Pfad), CLI-Kontext „default" = 20129.
  Stale-Lock `~/.omniroute/server/.pid` blockiert Neustarts → bei toter PID entfernen.
- **Bridge 17841:** nur Responses-API, verlangt Bearer (Codex `auth.json`). Tests nur über
  den Codex CLI, nicht per curl auf `/chat/completions`.
- **Wiederherstellung:** Quarantäne-Ordner (`~/codex-chatgpt-web-quarantine-*`) nicht löschen
  — daraus lässt sich die ChatGPT-Login-Session zurückholen.
- Keine Secrets in Repo-Dateien; Credentials nur über Environment/Doppler referenzieren.
