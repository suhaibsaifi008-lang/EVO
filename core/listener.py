import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import DATA_DIR, HOST, PORT

SERVER_URL = os.environ.get("JARVIS_SERVER_URL", f"http://{HOST}:{PORT}").rstrip("/")
MIC_DEVICE = os.environ.get("JARVIS_MIC_INDEX", "")
WAKE_THRESHOLD = float(os.environ.get("JARVIS_WAKE_THRESHOLD", "0.5"))
COOLDOWN_SECONDS = 3.5
MAX_COMMAND_SECONDS = 9
SILENCE_FRAMES_TO_END = 14
FRAME_SAMPLES = 1280
SAMPLE_RATE = 16000

MODELS_DIR = DATA_DIR / "models"
VOSK_DIR = MODELS_DIR / "vosk"
VOSK_ZIP_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.22.zip"

_speaking = threading.Event()


def _chime() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def _post(path: str, payload: dict, timeout: int = 90) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{SERVER_URL}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _notify_server_wake() -> None:
    _post("/api/wake", {}, timeout=5)


def _announce(event: dict) -> None:
    _post("/api/announce", event, timeout=5)


def _mci_play(path: Path) -> None:
    winmm = ctypes.windll.winmm
    alias = f"evo{int(time.time()*1000)}"
    winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
    winmm.mciSendStringW(f"close {alias}", None, 0, None)


def _sapi_speak(text: str) -> None:
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.Speak('{safe[:600]}')"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)


def _speak_reply(text: str) -> None:
    _speaking.set()
    try:
        played = False
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from core.tts import synthesize

            path = synthesize(text[:800])
            _mci_play(path)
            played = True
        except Exception:
            played = False
        if not played:
            _sapi_speak(text)
    finally:
        time.sleep(0.25)
        _speaking.clear()


def _ensure_vosk() -> Path | None:
    if VOSK_DIR.exists():
        try:
            if any(VOSK_DIR.iterdir()):
                return VOSK_DIR
            VOSK_DIR.rmdir()  # empty leftover from a failed extraction
        except OSError:
            pass
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    zipped = MODELS_DIR / "vosk.zip"
    print("[ear] downloading local speech model (~40MB, one time)...", flush=True)
    try:
        urllib.request.urlretrieve(VOSK_ZIP_URL, zipped)
        with zipfile.ZipFile(zipped) as zf:
            inner = zf.namelist()[0].split("/")[0]
            zf.extractall(MODELS_DIR)
        extracted = MODELS_DIR / inner
        if extracted.exists():
            if VOSK_DIR.exists():
                for child in extracted.iterdir():
                    child.replace(VOSK_DIR / child.name)
                extracted.rmdir()
            else:
                extracted.rename(VOSK_DIR)
        zipped.unlink()
        return VOSK_DIR if VOSK_DIR.exists() and any(VOSK_DIR.iterdir()) else None
    except Exception as exc:
        print(f"[ear] vosk model download failed: {exc}", flush=True)
        try:
            zipped.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def _rms(frame: bytes) -> float:
    import array

    samples = array.array("h", frame)
    if not samples:
        return 0.0
    total = sum(s * s for s in samples)
    return (total / len(samples)) ** 0.5


class Ear:
    def __init__(self) -> None:
        self.audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self.oww = None
        self.vosk_model = None
        self.last_hit = 0.0

    def load(self) -> tuple[bool, bool]:
        from openwakeword.model import Model

        self.oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        have_vosk = False
        try:
            import vosk  # noqa: F401

            model_dir = _ensure_vosk()
            if model_dir:
                self.vosk_model = vosk.Model(str(model_dir))
                have_vosk = True
        except Exception as exc:
            print(f"[ear] local transcription unavailable: {exc}", flush=True)
        return True, have_vosk

    def _feed_oww(self, frame: bytes) -> bool:
        import numpy as np

        samples = np.frombuffer(frame, dtype=np.int16)
        try:
            scores = self.oww.predict(samples)
        except Exception:
            return False
        score = max(scores.values()) if scores else 0.0
        return score >= WAKE_THRESHOLD

    def _record_command(self) -> str:
        if not self.vosk_model:
            return ""
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self.vosk_model, SAMPLE_RATE)
        rec.SetWords(False)
        deadline = time.time() + MAX_COMMAND_SECONDS
        speech_seen = False
        quiet_frames = 0
        final_text = ""

        while time.time() < deadline:
            try:
                frame = self.audio_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                is_final = rec.AcceptWaveform(frame)
            except Exception:
                continue
            if is_final:
                piece = json.loads(rec.Result()).get("text", "")
                if piece.strip():
                    final_text = (final_text + " " + piece).strip()
                    speech_seen = True
                    quiet_frames = 0
            level = _rms(frame)
            if level > 420:
                speech_seen = True
                quiet_frames = 0
            elif speech_seen:
                quiet_frames += 1
                if quiet_frames >= SILENCE_FRAMES_TO_END:
                    break
        tail = json.loads(rec.FinalResult()).get("text", "")
        if tail.strip():
            final_text = (final_text + " " + tail).strip()
        return final_text.strip()

    def _handle_exchange(self, text: str) -> None:
        result = _post("/api/chat", {"text": text}, timeout=120)
        reply = (result or {}).get("reply") or "I could not reach my core just now."
        _announce({"type": "voice_exchange", "kind": "voice", "spoken": True,
                   "user_text": text, "text": reply})
        _speak_reply(reply)

    def run(self) -> None:
        import sounddevice as sd

        have_vosk = self.load()
        mode = "full-duplex voice" if have_vosk else "wake-only (no local STT)"
        print(f"[ear] online - {mode}. Say 'Hey Jarvis'.", flush=True)

        device = int(MIC_DEVICE) if MIC_DEVICE.isdigit() else None
        kwargs = {}
        if device is not None:
            kwargs["device"] = device

        def callback(indata, frames, time_info, status):
            if not _speaking.is_set():
                try:
                    self.audio_q.put_nowait(bytes(indata))
                except queue.Full:
                    pass

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES,
                               dtype="int16", channels=1, callback=callback, **kwargs):
            leftover = b""
            while True:
                try:
                    frame = self.audio_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                data = leftover + frame
                usable = len(data) // (FRAME_SAMPLES * 2) * FRAME_SAMPLES * 2
                leftover = data[usable:]
                for i in range(0, usable, FRAME_SAMPLES * 2):
                    chunk = data[i : i + FRAME_SAMPLES * 2]
                    now = time.time()
                    if now - self.last_hit <= COOLDOWN_SECONDS or _speaking.is_set():
                        continue
                    if self._feed_oww(chunk):
                        self.last_hit = time.time()
                        self.oww.reset()
                        print("[ear] wake word detected", flush=True)
                        _chime()
                        _notify_server_wake()
                        if self.vosk_model:
                            command = self._record_command()
                            if command:
                                print(f"[ear] heard: {command}", flush=True)
                                self._handle_exchange(command)
                            else:
                                _speak_reply("I did not catch that.")
                        break


def run_once() -> None:
    """Run one blocking ear session. Kept separate so main() can retry it."""
    Ear().run()


def run_once_compat() -> None:
    """Legacy entry point kept for compatibility."""
    run_once()


def _pid_alive(pid: int) -> bool:
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:
        return True


def main() -> None:
    import os

    if os.environ.get("JARVIS_EAR_DISABLE") == "1":
        return
    lock = DATA_DIR / "ear.lock"
    if lock.exists():
        try:
            import json as _json

            info = _json.loads(lock.read_text() or "{}")
            stale = time.time() - float(info.get("ts", 0)) >= 86400
            if not stale and _pid_alive(int(info.get("pid", 0))):
                print("[ear] another ear instance is already running - exiting.", flush=True)
                return
        except Exception:
            pass
    lock.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    backoff = 3
    try:
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                print(f"[ear] {exc} — retrying in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
