# Schaltwerk — OmniRoute Proxy-Austausch (Windows 10)

Holt freie Proxies, prüft sie selbst (HTTPS) und schreibt nur die
funktionierenden als **manuelle** Proxies nach OmniRoute.
Die 1proxy-/Free-Kategorie wird nicht beschrieben (sonst Credits).

## Auf Windows starten

1. [Python 3.11 oder neuer](https://www.python.org/downloads/windows/) installieren  
   Beim Setup **„Add python.exe to PATH“** anhaken.
2. Diesen Ordner nach Windows kopieren, z. B.  
   `C:\Users\<du>\Desktop\omni-proxy-exchange`
3. **`STARTEN.bat` doppelklicken**  
   Das installiert die Pakete, startet den Server und öffnet den Browser  
   unter http://127.0.0.1:8765
4. OmniRoute muss parallel laufen (Standard: http://127.0.0.1:20128).
5. In der Oberfläche: URL + Management-API-Key (manage-Scope) → **Verbinden**.

Fenster mit der schwarzen Konsole **offen lassen**. Beenden mit Strg+C.

Windows-Firewall: bei Nachfrage für private Netze erlauben.  
Der Server hört nur auf `127.0.0.1` (dieser PC), nicht ins LAN.

## Ablauf

- **Bestand laden** — eigene manuelle Proxies / Austausch-Pool / 1proxy (nur lesen)
- **Listen holen** + **Kandidaten prüfen** — oder direkt **Austausch starten**
- Tote Einträge mit `notes=proxy-exchange` werden entfernt
- Lebendige kommen per `POST /api/v1/management/proxies` als normale Registry-Proxies
- **Provider zuordnen** — legt die besten lebendigen `px-*`-Proxies in die
  installierten Provider-Packs (Scope = Provider). Erst damit nutzt OmniRoute
  sie tatsächlich für Requests.

Eigene manuelle Proxies ohne dieses Tag bleiben unangetastet.

Die Häkchen bei „OmniRoute-Typ“ steuern Harvest, Prüfung und Upload
**gemeinsam** — nur angekreuzte Typen werden geholt, geprüft und geschrieben.

## Dateien

| Datei | Zweck |
| --- | --- |
| `STARTEN.bat` | Windows-Start |
| `server.py` | Backend (Windows + Linux) |
| `static\index.html` | Oberfläche |
| `requirements.txt` | Python-Pakete |
| `CHANGELOG.md` | Versionshistorie der Änderungen |

Eine fertige `.exe` kann hier nicht gebaut werden (dieser Rechner ist Linux).
Auf deinem Windows-PC reicht die `.bat`.

## Änderungen

Die versionierte Änderungshistorie steht in [CHANGELOG.md](CHANGELOG.md).
Neue Änderungen dort eintragen — nicht hier im README.
