"""Server-side offline speech-to-text (Vosk). Feeds the HUD mic fallback."""
import array
import json
import struct
import threading

from .config import DATA_DIR

MODELS_DIR = DATA_DIR / "models"
VOSK_DIR = MODELS_DIR / "vosk"

_lock = threading.Lock()
_model = None
_load_failed = ""


def _get_model():
    global _model, _load_failed
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:
            return _model
        try:
            import vosk

            vosk.SetLogLevel(-1)
            from .listener import _ensure_vosk

            model_dir = _ensure_vosk()
            if not model_dir:
                raise RuntimeError("local speech model unavailable")
            _model = vosk.Model(str(model_dir))
        except Exception as exc:
            _load_failed = str(exc)
            raise
    return _model


def last_error() -> str:
    return _load_failed


def available() -> bool:
    """True when offline transcription can run right now - never downloads."""
    try:
        import vosk  # noqa: F401
    except Exception:
        return False
    return VOSK_DIR.exists() and any(VOSK_DIR.iterdir())


def prewarm() -> bool:
    """Load (or download once) the offline model so the first mic press is instant."""
    try:
        _get_model()
        return True
    except Exception:
        return False


def _pcm_from_wav(data: bytes) -> tuple[int, int, bytes]:
    """Extract PCM payload, sample rate and channel count from RIFF/WAVE bytes."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos = 12
    rate = 16000
    channels = 1
    bits = 16
    fmt_seen = False
    pcm = b""
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        (chunk_size,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        body = data[pos + 8 : pos + 8 + chunk_size]
        if chunk_id == b"fmt ":
            audio_fmt, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            fmt_seen = True
            if audio_fmt not in (1, 3) or bits != 16:
                raise ValueError(f"unsupported WAV format (fmt={audio_fmt}, bits={bits})")
        elif chunk_id == b"data":
            pcm = body
        pos += 8 + chunk_size + (chunk_size & 1)
    if not fmt_seen or not pcm:
        raise ValueError("malformed WAV")
    return rate, channels, pcm


def transcribe_wav(data: bytes) -> str:
    """Transcribe 16-bit PCM WAV bytes to text, fully offline.

    A strict grammar recognizer (built from the user's apps/sites/commands)
    runs first - it can only output known phrases, so a hit is trustworthy
    and fixes the brand names the open model mangles. Free speech falls
    through to the general model + vocabulary corrector.
    """
    model = _get_model()
    from vosk import KaldiRecognizer

    rate, channels, pcm = _pcm_from_wav(data)
    if channels > 1:
        samples = array.array("h", pcm)
        mono = array.array("h", (
            samples[i] for i in range(0, len(samples), channels)
        ))
        pcm = mono.tobytes()
    frame_bytes = rate * 2 // 10  # 100 ms of mono 16-bit audio
    frames = [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]

    def run_rec(rec) -> str:
        for piece in frames:
            if piece:
                rec.AcceptWaveform(piece)
        return json.loads(rec.FinalResult()).get("text", "").strip()

    try:
        from .grammar import grammar_json

        g_text = run_rec(KaldiRecognizer(model, rate, grammar_json()))
        if g_text:
            return g_text
    except Exception:
        pass
    text = run_rec(KaldiRecognizer(model, rate))
    try:
        from .vocab import correct_terms

        text = correct_terms(text)
    except Exception:
        pass
    return text
