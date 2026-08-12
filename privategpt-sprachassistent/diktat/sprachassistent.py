"""
Sprachassistent: Diktat -> RAG-Frage an PrivateGPT -> Antwort vorlesen.

Ablauf:
    1. Sprich deine Frage (Enter = Start, Enter = Stop)
    2. Whisper transkribiert sie zu Text
    3. Die Frage wird per PrivateGPT an deine Dokumente geschickt
       (qwen3:4b + semantische Suche in der Collection test_de_lang)
    4. Die Antwort wird vorgelesen und in die Zwischenablage kopiert

Nutzung:
    Enter              -> Aufnahme STARTEN
    Enter              -> Aufnahme STOPPEN (Transkription + Antwort)
    q + Enter          -> Beenden

Optionen:
    --model base|small   Whisper-Modell (default: base)
    --no-tts             Antwort nicht vorlesen (nur anzeigen)
    --collection NAME    Collection (default: test_de_lang)
    --base-url URL       PrivateGPT-Adresse (default: http://localhost:8080)
"""

import json
import subprocess
import sys
import threading
import urllib.request

import numpy as np
import pyperclip
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
CHUNK_SECONDS = 1.0

TTS_SCRIPT = r"C:\Users\Hansi\Arena Wrap\privategpt-sprachassistent\sprechen.ps1"


def get_args():
    args = {
        "model": "base",
        "tts": True,
        "collection": "test_de_lang",
        "base_url": "http://localhost:8080",
    }
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--model" and i + 1 < len(argv):
            args["model"] = argv[i + 1]; i += 2
        elif a == "--collection" and i + 1 < len(argv):
            args["collection"] = argv[i + 1]; i += 2
        elif a == "--base-url" and i + 1 < len(argv):
            args["base_url"] = argv[i + 1]; i += 2
        elif a == "--no-tts":
            args["tts"] = False; i += 1
        else:
            i += 1
    return args


def record_until_stopped():
    input()
    global stop_recording
    stop_recording = True


def ask_rag(question, collection, base_url):
    """Sendet die Frage als RAG-Chat an PrivateGPT und liefert die finale Antwort."""
    payload = {
        "model": "qwen3:4b-gpu10",
        "stream": False,
        "tool_context": [
            {
                "type": "ingested_artifact",
                "context_filter": {"collection": collection},
            }
        ],
        "tools": [{"type": "semantic_search_v1", "name": "semantic_search"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Nutze deine Dokumentensuche und beantworte auf Deutsch: " + question,
                    }
                ],
            }
        ],
    }
    req = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        answer = json.loads(resp.read().decode("utf-8"))

    # Letzten Text-Block nehmen und den Denk-Teil (vor dem letzten </think>) entfernen
    texts = [b.get("text", "") for b in answer.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    text = texts[-1] if texts else "(keine Antwort erhalten)"
    idx = text.rfind("</think>")
    if idx != -1:
        text = text[idx + len("</think>"):]
    return text.strip()


def clean_for_speech(text):
    """Entfernt Markdown-Formatierung, damit das Vorlesen sauber klingt."""
    text = text.replace("*", "").replace("#", "").replace("`", "")
    text = text.replace("[1]", "").replace("**", "")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text


def speak(text):
    """Liest Text ueber die Windows-Sprachausgabe vor (nutzt sprechen.ps1)."""
    try:
        pyperclip.copy(text)
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", TTS_SCRIPT],
            timeout=600,
        )
        return True
    except Exception as e:
        print("(!) Vorlesen fehlgeschlagen:", e)
        return False


def main():
    args = get_args()
    print("Lade Whisper-Modell ({}) ...".format(args["model"]))
    model = WhisperModel(args["model"], device="cpu", compute_type="int8")
    print("Modell bereit. Collection: {}  |  TTS: {}".format(args["collection"], "an" if args["tts"] else "aus"))
    print("")
    print("  Enter = Aufnahme STARTEN / STOPPEN")
    print("  q     = Beenden")
    print("")

    global stop_recording

    while True:
        try:
            cmd = input("[bereit] Enter zum Starten, q zum Beenden: ").strip().lower()
        except EOFError:
            break
        if cmd == "q":
            break

        print("AUFNAHME ... (Enter zum Stoppen)")
        stop_recording = False
        t = threading.Thread(target=record_until_stopped, daemon=True)
        t.start()

        frames = []
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            while not stop_recording:
                data, _ = stream.read(int(SAMPLE_RATE * CHUNK_SECONDS))
                frames.append(data.copy())

        if not frames:
            print("Keine Aufnahme.")
            continue

        audio = np.concatenate(frames).flatten()
        print("Aufnahme beendet ({}s). Transkribiere ...".format(round(len(audio) / SAMPLE_RATE, 1)))

        segments, _ = model.transcribe(audio, language="de", vad_filter=True, beam_size=5)
        question = "".join(s.text for s in segments).strip()

        if not question:
            print("(nichts erkannt - nochmal versuchen)")
            continue

        print("FRAGE: " + question)
        print("Frage an PrivateGPT ...")

        try:
            answer = ask_rag(question, args["collection"], args["base_url"])
        except Exception as e:
            print("FEHLER bei der Anfrage:", e)
            print("Laeuft private-gpt? (start-private-gpt.ps1 -Action Start)")
            continue

        speech_text = clean_for_speech(answer)
        print("")
        print("=== ANTWORT ===")
        print(answer)
        print("===============")

        pyperclip.copy(answer)
        print("(Antwort in der Zwischenablage)")

        if args["tts"] and speech_text:
            print("Lese vor ...")
            speak(speech_text)
        print("")


if __name__ == "__main__":
    main()
