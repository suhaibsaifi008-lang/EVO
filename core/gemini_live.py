"""Gemini Live voice bridge — true duplex conversation for EVO.

Streams mic audio to Google's Live API over WebSocket and plays the spoken
reply in realtime (native server-side barge-in). The local Vosk wake word
still opens/closes sessions, a grammar sniffer still catches instant offline
commands, and everything falls back to the pure-local session if Gemini is
unavailable.

Requires: GEMINI_API_KEY (free at aistudio.google.com) + `google-genai`.
"""
import asyncio
import queue
import threading
import time

from . import config

DEFAULT_MODEL = "gemini-2.0-flash-live-001"
INPUT_RATE = 16000   # pcm16 mono we send
OUTPUT_RATE = 24000  # pcm16 mono gemini returns

PERSONA = (
    "You are EVO, a warm, sharp personal assistant speaking aloud with the user. "
    "Keep spoken replies natural and concise (1-4 sentences unless depth is asked). "
    "Match the user's tone. Never mention that you are an AI model or read out URLs."
)

_EXIT_WORDS = ("stop listening", "go to sleep", "goodbye", "end session", "that will be all")


def gemini_key() -> str:
    import os

    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("JARVIS_GEMINI_KEY")
        or ""
    ).strip()


def live_enabled() -> bool:
    import os

    if os.environ.get("JARVIS_VOICE_ENGINE", "auto").strip().lower() == "vosk":
        return False
    return bool(gemini_key())


def _model() -> str:
    import os

    return os.environ.get("JARVIS_GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def is_exit_text(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _EXIT_WORDS)


class LiveVoiceSession:
    """One duplex conversation with Gemini Live.

    Usage (from the ear thread):
        sess = LiveVoiceSession(mic_queue)
        sess.start()          # spawns the network loop
        sess.feed(chunk)      # push 16k pcm16 frames from the mic callback
        ...                   # replies play automatically via callbacks
        sess.stop()

    Callbacks (set before start):
        on_play(bytes)        -> raw pcm24k reply audio to play
        on_interrupt()        -> server detected user speech: cut playback
        on_turn(user, reply)  -> finished exchange (transcripts), for HUD
        on_exit()             -> user said goodbye / asked to end
    """

    def __init__(self, mic_queue: "queue.Queue[bytes]") -> None:
        self.mic_q = mic_queue
        self.on_play = lambda b: None
        self.on_interrupt = lambda: None
        self.on_turn = lambda user, reply: None
        on_exit = lambda: None
        self.on_exit = on_exit
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        try:
            from google.genai import types  # noqa: F401  (fail fast if missing)

            self._client = _make_client()
        except Exception as exc:
            self.last_error = f"gemini unavailable: {exc}"
            return False
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="evo-gemini-live")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---- internals -------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        """Called from the audio callback thread; never blocks long."""
        try:
            self._audio_in.put_nowait(chunk)
        except AttributeError:
            pass
        except queue.Full:
            pass

    async def _session_loop(self) -> None:
        from google.genai import types

        client = self._client
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=PERSONA,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                )
            ),
            input_audio_transcription=types.AudioTranscription(),
            output_audio_transcription=types.AudioTranscription(),
        )
        async with client.aio.live.connect(model=_model(), config=cfg) as session:
            input_buf = b""
            out_text = ""
            in_text = ""
            last_feed = time.time()

            async def sender() -> None:
                nonlocal input_buf, last_feed
                while True:
                    try:
                        chunk = await asyncio.to_thread(self._audio_in.get, True, 0.25)
                    except Exception:
                        chunk = None
                    if chunk is not None:
                        input_buf += chunk
                        last_feed = time.time()
                    if len(input_buf) >= INPUT_RATE * 2 * 0.06 or (
                        input_buf and time.time() - last_feed > 0.15
                    ):
                        payload, input_buf = input_buf[: INPUT_RATE * 2 * 2], input_buf[INPUT_RATE * 2 * 2:]
                        await session.send(
                            input=types.Blob(data=payload, mime_type=f"audio/pcm;rate={INPUT_RATE}")
                        )
                    else:
                        await asyncio.sleep(0.02)

            sender_task = asyncio.create_task(sender())
            try:
                async for msg in session.receive():
                    if self._stop.is_set():
                        break
                    sc = getattr(msg, "server_content", None)
                    if sc is not None and getattr(sc, "interrupted", False):
                        self.on_interrupt()
                    data = getattr(msg, "data", None)
                    if data:
                        self.on_play(data)
                    it = getattr(sc, "input_transcription", None) if sc else None
                    if it is not None and getattr(it, "text", ""):
                        in_text += it.text
                        if is_exit_text(in_text):
                            break
                    ot = getattr(sc, "output_transcription", None) if sc else None
                    if ot is not None and getattr(ot, "text", ""):
                        out_text += ot.text
                    tc = getattr(sc, "turn_complete", False) if sc else False
                    if tc:
                        if out_text.strip() or in_text.strip():
                            self.on_turn(in_text.strip(), out_text.strip())
                        in_text, out_text = "", ""
            finally:
                sender_task.cancel()

    def _run_loop(self) -> None:
        self._audio_in: "queue.Queue[bytes]" = queue.Queue(maxsize=200)
        try:
            asyncio.run(self._session_loop())
        except Exception as exc:
            self.last_error = str(exc)[:200]
        finally:
            self.on_exit()


def _make_client():
    from google import genai

    return genai.Client(api_key=gemini_key())


class Speaker:
    """Plays Gemini's 24kHz PCM through the speakers; interruptible."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = None

    def start(self) -> None:
        try:
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=OUTPUT_RATE, channels=1, dtype="int16",
                callback=self._cb, blocksize=960,
            )
            self._stream.start()
        except Exception:
            self._stream = None

    def _cb(self, outdata, frames, time_info, status) -> None:
        need = frames * 2
        with self._lock:
            take = bytes(self._buf[:need])
            del self._buf[:need]
        if len(take) < need:
            take += b"\x00" * (need - len(take))
        outdata[:] = take

    def play(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > OUTPUT_RATE * 2 * 20:  # >20s backlog: drop tail
                del self._buf[:- OUTPUT_RATE * 2]

    def interrupt(self) -> None:
        with self._lock:
            self._buf.clear()

    def stop(self) -> None:
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
