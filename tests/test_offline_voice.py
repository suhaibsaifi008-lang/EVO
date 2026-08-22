import struct

import pytest

from core import stt


def _make_wav(samples, rate=16000, channels=1):
    data = b"".join(struct.pack("<h", s) for s in samples)
    if channels > 1:
        inter = []
        for i in range(0, len(samples), channels):
            for c in samples[i : i + channels]:
                inter.append(c)
        data = b"".join(struct.pack("<h", s) for s in inter)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = (b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 2, channels * 2, 16))
    chunk = b"data" + struct.pack("<I", len(data)) + data
    return header + fmt + chunk


def _silence(n=1600):
    return [0] * n


def test_wav_parser_rejects_garbage():
    with pytest.raises(ValueError):
        stt._pcm_from_wav(b"not a wav at all")


def test_wav_parser_reads_mono():
    wav = _make_wav([100, -100, 250], rate=16000, channels=1)
    rate, channels, pcm = stt._pcm_from_wav(wav)
    assert (rate, channels) == (16000, 1)
    assert len(pcm) == 6


def test_wav_parser_downmixes_stereo():
    raw = struct.pack("<hh", 10, 200)
    header = b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 8000, 32000, 4, 16)
    chunk = b"data" + struct.pack("<I", len(raw)) + raw
    rate, channels, pcm = stt._pcm_from_wav(header + fmt + chunk)
    assert channels == 2
    samples = list(__import__("array").array("h", pcm))
    assert samples == [10, 200]  # parser returns raw interleaved frames; transcribe downmixes


def test_transcribe_silence_returns_empty(monkeypatch):
    import sys
    import types

    class Rec:
        def __init__(self, model, rate):
            pass

        def SetWords(self, flag):
            pass

        def AcceptWaveform(self, chunk):
            return False

        def FinalResult(self):
            return '{"text": ""}'

    fake_vosk = types.ModuleType("vosk")
    fake_vosk.KaldiRecognizer = Rec
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    monkeypatch.setattr(stt, "_get_model", lambda: object())
    assert stt.transcribe_wav(_make_wav(_silence(1600))) == ""


class TestTranscribeEndpoint:
    def test_endpoint_roundtrip(self, monkeypatch):
        from fastapi.testclient import TestClient

        import main

        monkeypatch.setattr("core.stt.transcribe_wav", lambda data: "hello there")
        client = TestClient(main.app)
        resp = client.post("/api/transcribe", content=b"RIFF-fake-wav-bytes")
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello there"

    def test_endpoint_bad_audio(self):
        from fastapi.testclient import TestClient

        import main

        client = TestClient(main.app)
        resp = client.post("/api/transcribe", content=b"")
        assert resp.status_code == 400
