import asyncio
import hashlib
import os
from pathlib import Path

from .config import DATA_DIR

TTS_DIR = DATA_DIR / "tts"
TTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE = os.environ.get("JARVIS_TTS_VOICE", "en-GB-RyanNeural")

CURATED_VOICES = {
    "en-GB-RyanNeural": "Ryan - British male (EVO default)",
    "en-GB-ThomasNeural": "Thomas - British male",
    "en-GB-SoniaNeural": "Sonia - British female",
    "en-US-GuyNeural": "Guy - American male",
    "en-US-AndrewNeural": "Andrew - American male, warm",
    "en-IN-PrabhatNeural": "Prabhat - Indian male",
    "en-AU-WilliamNeural": "William - Australian male",
}

TONES = {
    "normal": {"rate": "+0%", "pitch": "+0Hz"},
    "alert": {"rate": "+14%", "pitch": "+22Hz"},
    "calm": {"rate": "-7%", "pitch": "-5Hz"},
    "warm": {"rate": "+3%", "pitch": "+8Hz"},
}

ALERT_WORDS = ("warning", "failed", "critical", "error", "battery", "urgent", "immediately", "danger")


def detect_tone(text: str) -> str:
    lowered = (text or "").lower()
    if any(w in lowered for w in ALERT_WORDS):
        return "alert"
    return "normal"


def list_voices() -> dict:
    return dict(CURATED_VOICES)


def synthesize(text: str, voice: str = "", tone: str = "") -> Path:
    import edge_tts

    text = " ".join((text or "").split())[:1200]
    if not text:
        raise ValueError("empty text")
    voice = (voice or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    tone = tone if tone in TONES else detect_tone(text)
    preset = TONES[tone]
    key = hashlib.sha1(f"{voice}|{tone}|{text}".encode("utf-8")).hexdigest()[:20] + ".mp3"
    out = TTS_DIR / key
    if out.exists() and out.stat().st_size > 512:
        return out

    async def _generate(target: Path) -> None:
        com = edge_tts.Communicate(text, voice, rate=preset["rate"], pitch=preset["pitch"])
        await com.save(str(target))

    tmp = out.with_suffix(".part")
    asyncio.run(_generate(tmp))
    if tmp.stat().st_size < 256:
        raise RuntimeError("speech service returned no audio")
    tmp.replace(out)
    return out


def synthesize_offline(text: str, tone: str = "") -> Path:
    """Last-resort voice: Windows SAPI rendered to WAV (no internet needed)."""
    import subprocess

    text = " ".join((text or "").split())[:800]
    if not text:
        raise ValueError("empty text")
    key = hashlib.sha1(f"sapi|{tone}|{text}".encode("utf-8")).hexdigest()[:20] + ".wav"
    out = TTS_DIR / key
    if out.exists() and out.stat().st_size > 512:
        return out
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SetOutputToWaveFile('{out}');"
        f"$s.Speak('{safe}');"
        "$s.Dispose()"
    )
    tmp = out.with_suffix(".part")
    ps_tmp = ps.replace(str(out), str(tmp))
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_tmp],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=90,
        check=True,
    )
    if tmp.stat().st_size < 256:
        raise RuntimeError("SAPI produced no audio")
    tmp.replace(out)
    return out


def cleanup(max_files: int = 300) -> None:
    files = sorted(TTS_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    wavs = sorted(TTS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    for stale in (files[:-max_files] if len(files) > max_files else []) + (
        wavs[:-max_files] if len(wavs) > max_files else []
    ):
        try:
            stale.unlink()
        except OSError:
            pass
    # Orphaned partial downloads from failed syntheses.
    import time as _time

    cutoff = _time.time() - 3600
    for part in TTS_DIR.glob("*.part"):
        try:
            if part.stat().st_mtime < cutoff:
                part.unlink()
        except OSError:
            pass
