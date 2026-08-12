"""
Globaler Assistenten-Hotkey - laeuft im Hintergrund.

    Strg + Umschalt + D   Diktat starten / stoppen
    Strg + Umschalt + X   Beenden

Ablauf:
    1. Druck:  Aufnahme startet (sprich deine Frage)
    2. Druck:  Stopp + Transkription (Whisper, Deutsch)
    3. Die Frage wird an PrivateGPT geschickt (qwen3:4b-gpu10,
       Collection test_de_lang) - Antwort mit Quellen aus deinen Dokumenten
    4. Die Antwort wird automatisch mit der Katja-Stimme vorgelesen und
       in die Zwischenablage kopiert

Start:
    hotkey.bat        (oder: .venv\\Scripts\\python hotkey.py)

Optionen:
    --model base|small   Whisper-Modell (default: small)
    --collection NAME    PrivateGPT-Collection (default: test_de_lang)
    --base-url URL       PrivateGPT-Adresse (default: http://localhost:8080)
    --paste              Antwort zusaetzlich ins aktive Programm einfuegen
    --no-tts             Antwort nicht vorlesen (nur anzeigen/kopieren)
"""

import ctypes
import importlib.util
import os
import winsound
import sys
import threading
import time

import numpy as np
import pyperclip
import sounddevice as sd
from faster_whisper import WhisperModel
from pynput import keyboard

SAMPLE_RATE = 16000
CHUNK_SECONDS = 1.0

ASSISTANT_PATH = r"C:\Users\Hansi\Arena Wrap\privategpt-sprachassistent\diktat\sprachassistent.py"

# Virtuelle Tastencodes: 0x11 = Ctrl, 0x56 = V
user32 = ctypes.windll.user32

_assistant_module = None


def get_assistant():
    """Laedt die Hilfsfunktionen aus sprachassistent.py (ask_rag, speak, clean_for_speech)."""
    global _assistant_module
    if _assistant_module is None:
        spec = importlib.util.spec_from_file_location("sprachassistent", ASSISTANT_PATH)
        _assistant_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_assistant_module)
    return _assistant_module


SOUND_DIR = r"C:\Users\Hansi\Arena Wrap\privategpt-sprachassistent\diktat\sounds"


def beep(kind):
    """Weicher, leiser Signalton als Feedback (start/stop)."""
    try:
        winsound.PlaySound(
            os.path.join(SOUND_DIR, kind + ".wav"),
            winsound.SND_FILENAME | winsound.SND_ASYNC,
        )
    except Exception:
        pass


def paste_from_clipboard():
    """Sendet Strg+V an das aktive Programm (Text muss vorher in der Zwischenablage sein)."""
    time.sleep(0.2)
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl gedrueckt
    user32.keybd_event(0x56, 0, 0, 0)  # V gedrueckt
    user32.keybd_event(0x56, 0, 2, 0)  # V losgelassen
    user32.keybd_event(0x11, 0, 2, 0)  # Ctrl losgelassen


class Dictator:
    def __init__(self, model_name="small", collection="test_de_lang",
                 base_url="http://localhost:8080", tts=True, paste=False):
        self.model_name = model_name
        self.collection = collection
        self.base_url = base_url
        self.tts = tts
        self.paste = paste
        self.model = None
        self.recording = False
        self.stop_evt = threading.Event()
        self.frames = []
        self.rec_thread = None

    def _load_model(self):
        if self.model is None:
            print("[.] Lade Whisper-Modell ({}) ...".format(self.model_name))
            self.model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            print("[ok] Modell bereit.")

    def start(self):
        if self.recording:
            return
        self.recording = True
        self.stop_evt.clear()
        self.frames = []
        self.rec_thread = threading.Thread(target=self._record, daemon=True)
        self.rec_thread.start()
        beep("start")
        print("[Aufnahme] Sprich deine Frage ... (Strg+Umschalt+D zum Stoppen)")

    def _record(self):
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
                while not self.stop_evt.is_set():
                    data, _ = stream.read(int(SAMPLE_RATE * CHUNK_SECONDS))
                    self.frames.append(data.copy())
        except Exception as e:
            print("[!] Mikrofon-Fehler:", e)
            self.recording = False

    def stop_and_ask(self):
        """Stoppt die Aufnahme, transkribiert und sendet die Frage an PrivateGPT."""
        if not self.recording:
            return
        self.recording = False
        self.stop_evt.set()
        if self.rec_thread:
            self.rec_thread.join(timeout=10)
        beep("stop")
        if not self.frames:
            print("(keine Aufnahme)")
            return

        audio = np.concatenate(self.frames).flatten()
        secs = len(audio) / SAMPLE_RATE
        print("[.] Aufnahme beendet ({}s). Transkribiere ...".format(round(secs, 1)))
        self._load_model()

        segments, _ = self.model.transcribe(
            audio, language="de", vad_filter=True, beam_size=5
        )
        question = "".join(s.text for s in segments).strip()
        if not question:
            print("(nichts erkannt)")
            return

        print("[ok] FRAGE: " + question)
        print("[.] Frage an PrivateGPT ({} / {}) ...".format(self.base_url, self.collection))

        sa = get_assistant()
        try:
            answer = sa.ask_rag(question, self.collection, self.base_url)
        except Exception as e:
            print("[!] PrivateGPT-Fehler:", e)
            print("    Laeuft private-gpt? (start-private-gpt.ps1 -Action Start)")
            pyperclip.copy(question)
            return

        pyperclip.copy(answer)
        print("---")
        print(answer)
        print("---")
        print("[ok] Antwort in der Zwischenablage")

        if self.paste:
            print("[.] Fuege ein ...")
            paste_from_clipboard()

        if self.tts and answer:
            speech_text = sa.clean_for_speech(answer)
            if speech_text:
                print("[.] Lese Antwort vor (Katja) ...")
                sa.speak(speech_text)


def parse_args():
    args = {
        "model": "small",
        "collection": "test_de_lang",
        "base_url": "http://localhost:8080",
        "tts": True,
        "paste": False,
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
        elif a == "--paste":
            args["paste"] = True; i += 1
        elif a == "--no-tts":
            args["tts"] = False; i += 1
        else:
            i += 1
    return args


def main():
    args = parse_args()
    d = Dictator(
        model_name=args["model"],
        collection=args["collection"],
        base_url=args["base_url"],
        tts=args["tts"],
        paste=args["paste"],
    )
    print("=" * 52)
    print("  Assistenten-Hotkey (laeuft im Hintergrund)")
    print("  Strg+Umschalt+D  Frage diktieren -> Antwort vorlesen")
    print("  Strg+Umschalt+X  Beenden")
    print("=" * 52)
    print("Whisper: {} | Collection: {} | TTS: {}".format(
        args["model"], args["collection"], "an (Katja)" if args["tts"] else "aus"))

    state = {"recording": False}

    def toggle():
        if state["recording"]:
            state["recording"] = False
            d.stop_and_ask()
        else:
            state["recording"] = True
            d.start()

    hotkeys = keyboard.GlobalHotKeys(
        {
            "<ctrl>+<shift>+d": toggle,
            "<ctrl>+<shift>+x": lambda: sys.exit(0),
        }
    )
    hotkeys.run()


if __name__ == "__main__":
    main()
