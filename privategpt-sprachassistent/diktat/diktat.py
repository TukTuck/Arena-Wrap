"""
Diktat - Spracherkennung (Sprechen -> Text), komplett lokal.

Nutzung:
    Enter       -> Aufnahme STARTEN
    Enter       -> Aufnahme STOPPEN und transkribieren
    q + Enter   -> Beenden

Das Ergebnis erscheint in der Konsole und wird in die Zwischenablage kopiert.

Optionen:
    --model base|small   Whisper-Modell (default: base, besser: small)
    --language de        Sprache (default: de)
"""

import sys
import threading

import numpy as np
import sounddevice as sd
import pyperclip
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
CHUNK_SECONDS = 1.0


def get_args():
    model = "base"
    language = "de"
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--model" and i + 1 < len(argv):
            model = argv[i + 1]
            i += 2
        elif argv[i] == "--language" and i + 1 < len(argv):
            language = argv[i + 1]
            i += 2
        else:
            i += 1
    return model, language


def record_until_stopped():
    """Laeuft im Thread und wartet auf Enter als Stopp-Signal."""
    input()
    global stop_recording
    stop_recording = True


def main():
    model_name, language = get_args()

    print("Lade Whisper-Modell ({}), kann beim ersten Start etwas dauern ...".format(model_name))
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    print("Modell bereit. Sprache: {}".format(language))
    print("")
    print("  Enter = Aufnahme STARTEN")
    print("  Enter = Aufnahme STOPPEN")
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
        seconds = len(audio) / SAMPLE_RATE
        print("Aufnahme beendet ({}s). Transkribiere ...".format(round(seconds, 1)))

        segments, _info = model.transcribe(
            audio,
            language=language,
            vad_filter=True,
            beam_size=5,
        )
        text = "".join(seg.text for seg in segments).strip()

        print("")
        print("=== TEXT ===")
        print(text if text else "(nichts erkannt)")
        print("===========")
        if text:
            try:
                pyperclip.copy(text)
                print("(in die Zwischenablage kopiert - Strg+V zum Einfuegen)")
            except Exception:
                print("(Zwischenablage nicht verfuegbar - Text oben kopieren)")
        print("")


if __name__ == "__main__":
    main()
