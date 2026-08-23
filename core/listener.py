import ctypes
import json
import os
import re
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
COOLDOWN_SECONDS = 2.0
MAX_COMMAND_SECONDS = 9
SILENCE_FRAMES_TO_END = 14
FRAME_SAMPLES = 1280
SAMPLE_RATE = 16000

MODELS_DIR = DATA_DIR / "models"
VOSK_DIR = MODELS_DIR / "vosk"
# SPEED RULE, learned the hard way: the 124MB lgraph model transcribes ~10x
# slower than realtime on typical CPUs, so the ear ends up listening to the
# past. The small model is realtime and accurate ENOUGH once boosted by the
# grammar rescuer + vocab corrector. Opt into lgraph with JARVIS_VOSK_MODEL=big.
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
BARGE_IN_RMS = 1100       # sustained mic level during playback that cuts EVO off
QUIET_RMS = 320           # below this, the mic is considered silent
EARLY_COMMIT_S = 0.7      # stable partial + quiet mic => send immediately
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


_mci_alias = {"name": None}


def _mci_start(path: Path) -> str | None:
    """Start non-blocking playback; returns the alias or None."""
    try:
        winmm = ctypes.windll.winmm
        alias = f"evo{int(time.time()*1000)}"
        if winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}', None, 0, None) != 0:
            return None
        if winmm.mciSendStringW(f"play {alias}", None, 0, None) != 0:
            winmm.mciSendStringW(f"close {alias}", None, 0, None)
            return None
        _mci_alias["name"] = alias
        return alias
    except Exception:
        return None


def _mci_playing(alias: str) -> bool:
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.winmm.mciSendStringW(f"status {alias} mode", buf, 64, None)
    return buf.value.strip().lower() == "playing"


def _mci_close(alias: str) -> None:
    try:
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW(f"stop {alias}", None, 0, None)
        winmm.mciSendStringW(f"close {alias}", None, 0, None)
    except Exception:
        pass
    if _mci_alias.get("name") == alias:
        _mci_alias["name"] = None


_sapi_proc = {"proc": None}


def _sapi_speak(text: str, stop_event: "threading.Event | None" = None) -> None:
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.Speak('{safe[:600]}')"
    )
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _sapi_proc["proc"] = proc
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            proc.kill()
            break
        time.sleep(0.12)


def _abort_playback() -> None:
    """Immediately silence whatever EVO is saying (barge-in)."""
    alias = _mci_alias.get("name")
    if alias:
        _mci_close(alias)
    proc = _sapi_proc.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


def _speak_reply(text: str, stop_event: "threading.Event | None" = None,
                 done_event: "threading.Event | None" = None) -> None:
    """Speak text in this thread, interruptible via stop_event."""
    _speaking.set()
    try:
        path = None
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from core.tts import synthesize_best

            path = synthesize_best(text[:800])
        except Exception:
            path = None
        if path is not None:
            alias = _mci_start(path)
            if alias:
                while _mci_playing(alias):
                    if stop_event is not None and stop_event.is_set():
                        break
                    time.sleep(0.12)
                _mci_close(alias)
            else:
                _sapi_speak(text, stop_event)
        else:
            _sapi_speak(text, stop_event)
    finally:
        time.sleep(0.2)
        _speaking.clear()
        if done_event is not None:
            done_event.set()


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


def _sentence_chunks(text: str, max_len: int = 260) -> list[str]:
    """Split a reply into speakable sentence chunks for pipelined TTS."""
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return [text] if text else []
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""
    for part in parts:
        if cur and len(cur) + 1 + len(part) > max_len:
            chunks.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    if cur:
        chunks.append(cur)
    return chunks or [text]


class Ear:
    def __init__(self) -> None:
        self.audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self.oww = None
        self.vosk_model = None
        self.last_hit = 0.0
        self.wake_count = 0
        self._gen = 0                      # exchange generation (barge-in cancel)
        self.stop_tts = threading.Event()  # set to silence playback instantly
        self.live_voice = False            # set in load(): Gemini Live available?

    def say(self, text: str, blocking: bool = False) -> None:
        """Speak text. Non-blocking by default; always interruptible.

        Long replies are spoken as pipelined sentence chunks: the next chunk
        is synthesized while the previous one plays, cutting time-to-voice.
        """
        self.stop_tts.clear()
        chunks = _sentence_chunks(text)
        if len(chunks) <= 1:
            if blocking:
                _speak_reply(text, self.stop_tts)
            else:
                threading.Thread(
                    target=_speak_reply, args=(text, self.stop_tts), daemon=True, name="evo-speak"
                ).start()
            return

        def pipeline() -> None:
            from .tts import synthesize
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pre:
                nxt = pre.submit(self._synth_safe, chunks[0])
                for i, chunk in enumerate(chunks):
                    if self.stop_tts.is_set():
                        return
                    cur_path = nxt.result()
                    if i + 1 < len(chunks):
                        nxt = pre.submit(self._synth_safe, chunks[i + 1])
                    if cur_path is None:
                        _speak_reply(chunks[i], self.stop_tts)
                        continue
                    _speaking.set()
                    try:
                        alias = _mci_start(cur_path)
                        if not alias:
                            _sapi_speak(chunk, self.stop_tts)
                            continue
                        while _mci_playing(alias):
                            if self.stop_tts.is_set():
                                break
                            time.sleep(0.12)
                        _mci_close(alias)
                    finally:
                        _speaking.clear()

        threading.Thread(target=pipeline, daemon=True, name="evo-speak-pipe").start()

    @staticmethod
    def _synth_safe(text: str):
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from core.tts import synthesize_best

            return synthesize_best(text[:400])
        except Exception:
            return None

    def barge_in(self) -> None:
        """User started talking: kill audio and invalidate pending replies."""
        self._gen += 1
        self.stop_tts.set()
        _abort_playback()
        _speaking.clear()

    def load(self) -> tuple[bool, bool]:
        have_vosk = False
        try:
            import vosk

            vosk.SetLogLevel(-1)
            model_dir = _ensure_vosk()
            if model_dir:
                self.vosk_model = vosk.Model(str(model_dir))
                have_vosk = True
        except Exception as exc:
            print(f"[ear] local transcription unavailable: {exc}", flush=True)
        try:
            from . import gemini_live

            self.live_voice = gemini_live.live_enabled()
        except Exception:
            self.live_voice = False
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

    def _fetch_reply(self, text: str) -> str | None:
        for attempt in (0, 1):  # one retry so a blip never eats a question
            result = _post("/api/chat", {"text": text}, timeout=90)
            if result and str(result.get("reply", "")).strip():
                return str(result["reply"]).strip()
            time.sleep(0.6)
        return None

    def _start_exchange(self, text: str) -> None:
        """Send text to the brain and speak the reply without blocking audio.

        A newer utterance (higher generation) silently cancels this one -
        that is what makes interruption feel instant.
        """
        self._gen += 1
        gen = self._gen

        def worker() -> None:
            reply = self._fetch_reply(text)
            if gen != self._gen:
                return  # superseded by a newer utterance / barge-in
            if reply is None:
                _announce({"type": "voice_exchange", "kind": "voice",
                           "spoken": False, "user_text": text,
                           "text": "Core unreachable just now."})
                reply = "My core seems unreachable right now."
            else:
                _announce({"type": "voice_exchange", "kind": "voice",
                           "spoken": True, "user_text": text, "text": reply})
            self.say(reply)

        threading.Thread(target=worker, daemon=True, name="evo-exchange").start()

    def _handle_exchange(self, text: str) -> None:
        self._start_exchange(text)

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
                self.say("I did not catch that.")

    def _run_live_session(self) -> None:
        """Duplex Gemini Live conversation. Blocks until the session ends
        (exit phrase, idle timeout, or error) then returns to Vosk watching."""
        from . import gemini_live as gl

        sess = gl.LiveVoiceSession(self.audio_q)
        speaker = gl.Speaker()
        sess.on_play = speaker.play
        sess.on_interrupt = speaker.interrupt
        ended = threading.Event()

        def handle_turn(user: str, reply: str) -> None:
            if user or reply:
                _announce({"type": "voice_exchange", "kind": "voice", "spoken": True,
                           "user_text": user[:400], "text": (reply or "")[:900]})
                print(f"[ear] live turn: {user[:60]!r} -> {len(reply)} chars", flush=True)

        sess.on_turn = handle_turn
        sess.on_exit = ended.set
        if not sess.start():
            _write_status(f"ERROR: {sess.last_error}", self.wake_count)
            print(f"[ear] {sess.last_error} - falling back to local voice", flush=True)
            self.say("I could not reach Gemini, so I am staying offline.")
            return

        from .grammar import grammar_json

        grec = None
        try:
            from vosk import KaldiRecognizer

            grec = KaldiRecognizer(self.vosk_model, SAMPLE_RATE, grammar_json())
            grec.SetWords(False)
        except Exception:
            pass

        speaker.start()
        _write_status("conversation active (Gemini Live)", self.wake_count)
        print("[ear] Gemini Live session open", flush=True)
        self._drain_audio()
        last_voice = time.time()
        recent_commands: dict[str, float] = {}
        try:
            while not ended.is_set() and not sess.stopped:
                try:
                    frame = self.audio_q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last_voice > SESSION_IDLE_EXIT:
                        print("[ear] live session idle - closing", flush=True)
                        break
                    continue
                sess.feed(frame)
                if _rms(frame) > 500:
                    last_voice = time.time()
                # Offline command sniffer stays armed during live talk.
                if grec is not None:
                    try:
                        if grec.AcceptWaveform(frame):
                            cand = json.loads(grec.FinalResult()).get("text", "").strip()
                        else:
                            cand = ""
                        if cand and not is_exit_phrase(cand):
                            now = time.time()
                            if now - recent_commands.get(cand, 0) > 5.0:
                                recent_commands[cand] = now
                                print(f"[ear] live command: {cand}", flush=True)
                                speaker.interrupt()
                                self.barge_in()
                                self._start_exchange(cand)
                        elif cand and is_exit_phrase(cand):
                            sess.stop()
                            break
                    except Exception:
                        pass
        finally:
            sess.stop()
            ended.wait(timeout=8)
            speaker.interrupt()
            speaker.stop()
            self.barge_in()
            self._drain_audio()
        _write_status("wake-phrase (vosk)", self.wake_count)
        print("[ear] back to wake listening", flush=True)

    def _voice_session_loop(self) -> None:
        """ChatGPT-style full-duplex voice.

        - One wake phrase opens a session; every sentence is answered.
        - EVO KEEPS LISTENING WHILE TALKING: sustained speech during playback
          triggers barge-in (audio stops instantly, pending reply cancelled,
          your interrupting sentence becomes the next command).
        - Exchanges run on worker threads so audio never stalls.
        """
        from vosk import KaldiRecognizer

        from .grammar import grammar_json, trust_grammar

        rec = KaldiRecognizer(self.vosk_model, SAMPLE_RATE)
        rec.SetWords(False)
        try:
            # Strict command rescuer: only outputs known phrases, so any hit
            # is trustworthy even when the general model garbles brand names.
            grec = KaldiRecognizer(self.vosk_model, SAMPLE_RATE, grammar_json())
            grec.SetWords(False)
        except Exception:
            grec = None
        leftover = b""
        active = False
        last_activity = time.time()
        barge_hits = 0
        last_partial_key = ""
        last_partial_change = 0.0
        noise_floor = QUIET_RMS * 0.5

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
                    self.say("Going back to standby.")
                continue
            data = leftover + frame
            usable = len(data) // (FRAME_SAMPLES * 2) * FRAME_SAMPLES * 2
            leftover = data[usable:]
            for i in range(0, usable, FRAME_SAMPLES * 2):
                chunk = data[i : i + FRAME_SAMPLES * 2]

                # ---- BARGE-IN WINDOW: keep listening while talking ----
                if _speaking.is_set():
                    if _rms(chunk) > BARGE_IN_RMS:
                        barge_hits += 1
                    else:
                        barge_hits = max(0, barge_hits - 1)
                    if barge_hits >= 5:  # ~0.5s of sustained speech
                        barge_hits = 0
                        self.barge_in()
                        rec.Reset()
                        self._drain_audio()
                        last_activity = time.time()
                        time.sleep(0.35)  # let the speaker echo tail pass
                    continue  # never feed playback echo into Vosk

                barge_hits = 0
                if active and time.time() - last_activity > SESSION_IDLE_EXIT:
                    sleep_mode()
                    self.say("Going back to standby.")
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
                if heard:
                    heard = _correct(heard)

                # Grammar rescuer runs in parallel while ACTIVE.
                grammar_text = ""
                if active and grec is not None:
                    try:
                        g_final = grec.AcceptWaveform(chunk)
                        if g_final:
                            grammar_text = json.loads(grec.FinalResult()).get("text", "")
                        if not grammar_text:
                            grammar_text = json.loads(grec.PartialResult()).get("partial", "")
                    except Exception:
                        pass

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

                    # Gemini Live takes conversation; Vosk stays the wake word
                    # and offline fallback.
                    if self.live_voice:
                        if rest.strip() and not is_exit_phrase(rest):
                            print(f"[ear] heard: {rest}", flush=True)
                            self._start_exchange(rest)
                        try:
                            self._run_live_session()
                        except Exception as exc:
                            print(f"[ear] live session failed: {exc}", flush=True)
                        break
                    if rest.strip():
                        if is_exit_phrase(rest):
                            sleep_mode()
                            continue
                        print(f"[ear] heard: {rest}", flush=True)
                        self._start_exchange(rest)
                    else:
                        self.say("I'm listening.")
                    break

                # ---- ACTIVE: answer every finished sentence ----
                # Early commit: act on a stable partial after a beat of quiet
                # mic instead of waiting for Vosk's full finalization - this
                # is what makes replies feel instant.
                now_level = _rms(chunk)
                noise_floor = 0.9 * noise_floor + 0.1 * now_level
                quiet = now_level < max(QUIET_RMS, noise_floor * 2.2)
                if not is_final and heard.strip():
                    partial_key = normalize_text(heard)[:80]
                    if partial_key == last_partial_key:
                        if (
                            quiet
                            and time.time() - last_partial_change >= EARLY_COMMIT_S
                            and time.time() - last_activity > 0.8
                        ):
                            is_final = True  # commit this partial as final
                    else:
                        last_partial_key = partial_key
                        last_partial_change = time.time()
                if not is_final or not heard.strip():
                    continue
                last_partial_key = ""
                last_activity = time.time()

                # Grammar rescuer has the final word on commands - but only
                # when the utterance is command-shaped; freeform conversation
                # must never be forced through a strict phrase list.
                if grammar_text and trust_grammar(grammar_text, heard):
                    command_text = grammar_text
                else:
                    command_text = heard.strip()
                if is_exit_phrase(command_text) or is_exit_phrase(heard):
                    self.barge_in()
                    rec.Reset()
                    if grec is not None:
                        grec.Reset()
                    self._drain_audio()
                    sleep_mode()
                    self.say("Very good. Say wake up evo when you need me.")
                    break
                rec.Reset()
                if grec is not None:
                    grec.Reset()
                self._drain_audio()
                print(f"[ear] heard: {command_text}", flush=True)
                self._start_exchange(command_text)
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
        if self.live_voice:
            mode = f"wake phrase '{WAKE_PHRASES[0]}' + Gemini Live"
        elif self.phrase_mode:
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
            # Audio ALWAYS flows - even while EVO speaks - so interruption works.
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


def _correct(text: str) -> str:
    """Vocabulary-aware fixup of raw ASR text (apps/sites/command words)."""
    try:
        from .vocab import correct_terms

        return correct_terms(text)
    except Exception:
        return text


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
