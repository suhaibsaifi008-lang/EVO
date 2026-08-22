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
# Primary + fallbacks. alphacephei removed small-en-us-0.22 (404), so the
# current small model is 0.15; bigger lgraph model as last resort.
VOSK_ZIP_URLS = [
    "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip",
]

# Wake phrases: comma-separated. Matched offline against live Vosk transcripts,
# so ANY phrase works (default: "wake up evo"). Set JARVIS_WAKE_PHRASES= (empty)
# to fall back to the pretrained openWakeWord "hey jarvis" audio model instead.
WAKE_PHRASES = [
    p.strip().lower()
    for p in os.environ.get("JARVIS_WAKE_PHRASES", "wake up evo,wake up e.v.o").split(",")
    if p.strip()
]
WAKE_FUZZY_THRESHOLD = 0.8

# Conversation session (ChatGPT-voice style): one wake phrase starts a live
# dialogue; every sentence is answered without repeating the wake word.
SESSION_IDLE_EXIT = 45.0  # seconds of silence before the session closes
EXIT_PHRASES = (
    "stop listening", "go to sleep", "go away", "that will be all",
    "that'll be all", "goodbye", "good bye", "thank you goodbye",
    "sleep now", "end session",
)

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
    last_exc = ""
    for url in VOSK_ZIP_URLS:
        print(f"[ear] downloading local speech model ({url.rsplit('/', 1)[-1]})...", flush=True)
        try:
            urllib.request.urlretrieve(url, zipped)
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
            try:
                zipped.unlink()
            except OSError:
                pass
            if VOSK_DIR.exists() and any(VOSK_DIR.iterdir()):
                return VOSK_DIR
        except Exception as exc:
            last_exc = str(exc)
            print(f"[ear] model download failed: {exc}", flush=True)
            try:
                zipped.unlink(missing_ok=True)
            except Exception:
                pass
    _write_status("ERROR: speech model unavailable - " + last_exc, 0)
    return None


def _rms(frame: bytes) -> float:
    import array

    samples = array.array("h", frame)
    if not samples:
        return 0.0
    total = sum(s * s for s in samples)
    return (total / len(samples)) ** 0.5


def normalize_text(text: str) -> str:
    import re as _re

    return _re.sub(r"\s+", " ", _re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def match_wake_phrase(text: str, phrases: list[str] | None = None) -> str | None:
    """Return the remainder of the utterance after a wake phrase, else None.

    Tolerant to mishearings ("wake up evil", "wakeup evo") via word-window and
    character-level similarity checks.
    """
    from difflib import SequenceMatcher

    t = normalize_text(text)
    if not t:
        return None
    words = t.split()
    joined = " ".join(words)
    compact = t.replace(" ", "")
    for phrase in (phrases if phrases is not None else WAKE_PHRASES):
        p = normalize_text(phrase)
        pw = p.split()
        if not pw:
            continue
        idx = joined.find(p)
        if idx >= 0:
            return joined[idx + len(p):].strip()
        # Compact containment catches merged speech like "wakeup evo".
        c_idx = compact.find(p.replace(" ", ""))
        if c_idx >= 0 and c_idx <= 2:  # only when the wake leads the utterance
            return ""
        n = len(pw)
        for i in range(0, max(1, len(words) - n + 1)):
            window = " ".join(words[i : i + n])
            if SequenceMatcher(None, window, p).ratio() >= WAKE_FUZZY_THRESHOLD:
                return " ".join(words[i + n:]).strip()
    # Character-level fuzzy scan for garbled transcriptions.
    for phrase in (phrases if phrases is not None else WAKE_PHRASES):
        p = normalize_text(phrase).replace(" ", "")
        if not p:
            continue
        pc = compact
        if len(pc) < max(4, len(p) - 3):
            continue
        step = 1
        for i in range(0, max(1, len(pc) - len(p) + 1), step):
            window = pc[i : i + len(p)]
            if abs(len(window) - len(p)) > 2:
                continue
            if SequenceMatcher(None, window, p).ratio() >= WAKE_FUZZY_THRESHOLD:
                tail_index = i + len(p)
                # Map back roughly to a word boundary for the remainder.
                consumed = len(" ".join(words)[: max(tail_index, 1)])
                rest_words = []
                seen = 0
                for w in words:
                    seen += len(w) + 1
                    if seen > tail_index:
                        rest_words.append(w)
                return " ".join(rest_words).strip()
    return None


def is_exit_phrase(text: str) -> bool:
    t = normalize_text(text)
    return any(p in t for p in EXIT_PHRASES)


class Ear:
    def __init__(self) -> None:
        self.audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self.oww = None
        self.vosk_model = None
        self.last_hit = 0.0
        self.wake_count = 0

    def load(self) -> tuple[bool, bool]:
        have_vosk = False
        try:
            import vosk  # noqa: F401

            model_dir = _ensure_vosk()
            if model_dir:
                self.vosk_model = vosk.Model(str(model_dir))
                have_vosk = True
        except Exception as exc:
            print(f"[ear] local transcription unavailable: {exc}", flush=True)
        if WAKE_PHRASES:
            if have_vosk:
                # Phrase mode uses Vosk transcripts; no openWakeWord needed.
                _write_status("wake-phrase (vosk)", 0)
                return False, True
            print("[ear] wake-phrase mode needs the local speech model - "
                  "falling back to openWakeWord.", flush=True)
        try:
            from openwakeword.model import Model

            try:
                self.oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            except Exception as exc:
                # Package model files can be missing on fresh installs.
                from openwakeword.utils import download_models

                download_models(["hey_jarvis"])
                self.oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception as exc:
            _write_status(f"ERROR: wake engine unavailable - {exc}", 0)
            raise
        _write_status("openwakeword (hey_jarvis)", 0)
        return True, have_vosk

    @property
    def phrase_mode(self) -> bool:
        return bool(WAKE_PHRASES) and self.vosk_model is not None and self.oww is None

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

    def _on_wake(self, command: str = "") -> None:
        """Common wake handling: chime, notify HUD, then run the command."""
        self.last_hit = time.time()
        self.wake_count += 1
        mode = "wake-phrase (vosk)" if self.phrase_mode else "openwakeword (hey_jarvis)"
        _write_status(mode, self.wake_count)
        print("[ear] wake phrase detected", flush=True)
        _chime()
        _notify_server_wake()
        text = (command or "").strip()
        if text:
            print(f"[ear] heard: {text}", flush=True)
            self._handle_exchange(text)
            return
        if self.vosk_model:
            command = self._record_command()
            if command:
                print(f"[ear] heard: {command}", flush=True)
                self._handle_exchange(command)
            else:
                _speak_reply("I did not catch that.")

    def _voice_session_loop(self) -> None:
        """ChatGPT-style voice: one wake phrase opens a conversation session.

        While active, EVERY sentence is answered - no wake word needed.
        Silence for SESSION_IDLE_EXIT seconds, or an exit phrase, closes the
        session and EVO goes back to sleep until woken again.
        """
        from vosk import KaldiRecognizer

        rec = KaldiRecognizer(self.vosk_model, SAMPLE_RATE)
        rec.SetWords(False)
        leftover = b""
        active = False
        last_activity = time.time()

        def sleep_mode() -> None:
            nonlocal active
            if active:
                print("[ear] conversation closed - sleeping", flush=True)
            active = False
            _write_status("wake-phrase (vosk)", self.wake_count)

        while True:
            try:
                frame = self.audio_q.get(timeout=1.0)
            except queue.Empty:
                if active and time.time() - last_activity > SESSION_IDLE_EXIT:
                    sleep_mode()
                    _speak_reply("Going back to standby.")
                continue
            data = leftover + frame
            usable = len(data) // (FRAME_SAMPLES * 2) * FRAME_SAMPLES * 2
            leftover = data[usable:]
            for i in range(0, usable, FRAME_SAMPLES * 2):
                chunk = data[i : i + FRAME_SAMPLES * 2]
                if _speaking.is_set() or time.time() - self.last_hit <= COOLDOWN_SECONDS:
                    continue
                if active and time.time() - last_activity > SESSION_IDLE_EXIT:
                    sleep_mode()
                    _speak_reply("Going back to standby.")
                    continue
                try:
                    is_final = rec.AcceptWaveform(chunk)
                except Exception:
                    continue
                heard = ""
                if is_final:
                    try:
                        heard = json.loads(rec.FinalResult()).get("text", "")
                    except Exception:
                        heard = ""
                if not heard:
                    try:
                        heard = json.loads(rec.PartialResult()).get("partial", "")
                    except Exception:
                        heard = ""

                # ---- SLEEPING: watch for the wake phrase (partials OK) ----
                if not active:
                    rest = match_wake_phrase(heard)
                    if rest is None:
                        continue
                    self.wake_count += 1
                    self.last_hit = time.time()
                    active = True
                    last_activity = time.time()
                    _write_status("conversation active", self.wake_count)
                    print("[ear] wake phrase detected - session open", flush=True)
                    _chime()
                    _notify_server_wake()
                    rec.Reset()
                    self._drain_audio()
                    if rest.strip():
                        if is_exit_phrase(rest):
                            sleep_mode()
                            continue
                        print(f"[ear] heard: {rest}", flush=True)
                        self._handle_exchange(rest)
                        last_activity = time.time()
                        self._drain_audio()
                    else:
                        _speak_reply("I'm listening.")
                        last_activity = time.time()
                        self._drain_audio()
                    break

                # ---- ACTIVE: answer every finished sentence ----
                if not is_final or not heard.strip():
                    continue
                if is_exit_phrase(heard):
                    rec.Reset()
                    self._drain_audio()
                    sleep_mode()
                    _speak_reply("Very good. Say wake up evo when you need me.")
                    break
                rec.Reset()
                self._drain_audio()
                print(f"[ear] heard: {heard}", flush=True)
                self._handle_exchange(heard.strip())
                last_activity = time.time()
                self._drain_audio()
                break

    def _oww_loop(self, have_vosk: bool) -> None:
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
                    self.oww.reset()
                    self._drain_audio()
                    self._on_wake()
                    break

    def _drain_audio(self) -> None:
        try:
            while True:
                self.audio_q.get_nowait()
        except queue.Empty:
            pass

    def run(self) -> None:
        import sounddevice as sd

        oww_ok, have_vosk = self.load()
        if self.phrase_mode:
            mode = f"wake phrase '{WAKE_PHRASES[0]}'"
        else:
            mode = ("full-duplex voice" if have_vosk else "wake-only (no local STT)")
        hint = f"Say '{WAKE_PHRASES[0]}'." if WAKE_PHRASES else "Say 'Hey Jarvis'."
        print(f"[ear] online - {mode}. {hint}", flush=True)

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
            if self.phrase_mode:
                self._voice_session_loop()
            else:
                self._oww_loop(have_vosk)


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


def _write_status(mode: str, wakes: int) -> None:
    """Heartbeat so diagnostics can prove which ear version is live."""
    try:
        (DATA_DIR / "ear_status.json").write_text(
            json.dumps({
                "ts": time.time(),
                "mode": mode,
                "wake_phrases": WAKE_PHRASES,
                "wakes": wakes,
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> None:
    import os

    if os.environ.get("JARVIS_EAR_DISABLE") == "1":
        return
    from .singleinstance import hold_single_instance

    if not hold_single_instance("ear.lock", "EVO_EAR_MUTEX"):
        print("[ear] could not acquire single-instance slot - exiting.", flush=True)
        return
    backoff = 3
    try:
        while True:
            try:
                run_once()
                backoff = 3
            except KeyboardInterrupt:
                return
            except Exception as exc:
                _write_status(f"ERROR: {exc}", 0)
                print(f"[ear] {exc} — retrying in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        try:
            (DATA_DIR / "ear.lock").unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
