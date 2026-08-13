# PROMPT: Hermes OS — Minimal (Tray-Agent)

Baue in **einem Durchgang** ein minimales, funktionierendes „Hermes OS":
einen lokalen Agenten, der im **Windows-System-Tray** lebt, Fragen
entgegennimmt und sie mit dem **lokalen Ollama-Modell** beantwortet.
Minimal, ganz minimal — Hauptsache es funktioniert. Keine Spielereien.

## Umgebung (Fakten, nicht ändern)

- Windows 10/11, Python 3.11 vorhanden
- venv: `C:\Users\Hansi\diktat\.venv` (dort installieren)
- Ollama läuft lokal auf `http://localhost:11434`
- Modell: `qwen3:4b` (spricht Deutsch) — Fallback `qwen3:4b-gpu10`
- Alles lokal, kein Internet nötig

## Aufgabe

Erstelle **EINE** Python-Datei `hermes.py`:

1. **System-Tray-Icon** (`pystray` + `Pillow`; einfaches Symbol, z. B. Buchstabe „H")
2. Menüpunkt **„Hermes fragen…"**:
   - kleines Eingabefeld (`tkinter.simpledialog`) für die Frage
   - Sende sie an Ollama: `POST http://localhost:11434/api/chat`
     - `model: qwen3:4b`
     - System-Prompt: *„Du bist Hermes. Antworte auf Deutsch, kurz und präzise."*
     - `stream: false`
   - Antwort in einem **tkinter-Fenster mit scrollbarer Textbox** anzeigen
     **und automatisch in die Zwischenablage kopieren**
3. Menüpunkt **„Beenden"**
4. **Keine weiteren Features.** Keine Konfig-Datei, keine TTS, kein RAG.
   Konstanten (`OLLAMA_URL`, `MODEL`) oben im File.

## Grenzen

- Eine Datei, so wenige Zeilen wie möglich (Ziel: < 150)
- Nur nötige Abhängigkeiten: `pystray`, `Pillow` (tkinter ist eingebaut)
- Keine Logging-Frameworks, keine CLI-Parser
- Fehler (z. B. Ollama nicht erreichbar) → verständliche Meldung im Dialog,
  **kein Absturz**

## Abnahme (unbedingt selbst testen)

1. `C:\Users\Hansi\diktat\.venv\Scripts\pip install pystray pillow`
2. `C:\Users\Hansi\diktat\.venv\Scripts\python hermes.py` → Tray-Icon erscheint
3. „Hermes fragen…" → „Was ist 2+2?" → Antwort „4" erscheint + ist im Clipboard
4. Ollama stoppen → Frage stellen → saubere Fehlermeldung, kein Crash

**Los. Baue es, teste es und präsentiere das fertige `hermes.py`.**
