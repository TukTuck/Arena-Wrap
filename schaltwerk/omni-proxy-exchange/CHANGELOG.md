# Changelog

Alle wesentlichen Änderungen an diesem Projekt werden hier mit
Versionsnummern dokumentiert. Format orientiert sich an
[Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionierung
nach [SemVer](https://semver.org/lang/de/).

Versionierung:
- **Patch** (1.0.1): Fixes, die nichts kaputt machen können
- **Minor** (1.1.0): neue Funktionen, abwärtskompatibel
- **Major** (2.0.0): Verhalten ändert sich / Endpunkte brechen

Neue Änderungen gehören in den Block `[Unreleased]` und werden beim
nächsten Stand nach unten in eine Versionsnummer verschoben.

## [Unreleased]

## [1.0.0] – 2026-08-12

Erster dokumentierter Stand nach dem Umbau „Austausch + Zuordnung“.

### Hinzugefügt

- **„Provider zuordnen“** (`POST /api/assign-providers` + Button):
  ordnet die besten lebendigen `px-*`-Proxies allen installierten
  Providern zu (scope=provider, Scope-ID = Provider-ID aus
  `GET /api/providers`). Prüft zur Laufzeit, ob mehrere Proxies pro
  Scope (Pool) via API möglich sind; sonst gilt 1 Proxy pro Provider
  (Replace-Semantik). OmniRoute nutzt einen Proxy erst nach der
  Scope-Zuordnung — vorher füllte das Tool nur das Register.
- **Verifikation nach dem Zuordnen:** `/api/assign-providers` liest
  die tatsächlichen Zuweisungen aus OmniRoute zurück und loggt sie
  (Nachweis, dass die Proxies wirklich bei den Providern hängen).
- **`GET /api/job-status`:** Phase/Zähler/Fehler des letzten oder
  laufenden Austausch-Jobs (Diagnose).
- **Laufzeit-Logging:** jeder Austausch schreibt `[job]`-Zeilen mit
  Zeitstempel in die Server-Konsole.
- **UI-Reconnect:** reißt die SSE-Verbindung ab (z. B. Reload), holt
  die Oberfläche den Job-Status nach und zeigt, ob der Lauf
  weiterläuft, statt still zu warten.
- **Harte Deadline pro Einzel-Check** (max. 15 s bzw. 7×Timeout),
  damit kein hängender Proxy den Austausch blockiert.

### Behoben

- **`POST /api/exchange` ignorierte die Typ-Auswahl:** Harvest fiel
  immer auf `["http","https"]` zurück, die Kandidaten wurden nicht und
  der Upload nur unzureichend nach Typ gefiltert — auch wenn z. B. nur
  SOCKS5 angekreuzt war, wurden HTTP/HTTPS-Proxies geholt und
  geschrieben. Jetzt fließt die Auswahl durch Harvest, Kandidaten-Filter
  und Upload; „nur angekreuzte Typen holen, prüfen und hochladen“ gilt
  wirklich.
