"""Voice layer: Gemini first, Groq as fallback, Gemini TTS returns playable WAV. No network (requests is mocked)."""
import base64
import io
import wave

import pytest

import llm


class Resp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code = payload or {}, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "STT_ORDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm, "_secrets", {})


def test_pcm_to_wav_is_valid():
    wav = llm.pcm_to_wav(b"\x00\x01" * 2400)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)


def test_tts_needs_gemini_key():
    with pytest.raises(llm.LLMUnavailable):
        llm.tts("salam")


def test_tts_returns_wav(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    pcm = base64.b64encode(b"\x00\x01" * 100).decode()
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["body"] = url, kw["json"]
        return Resp({"candidates": [{"content": {"parts": [{"inlineData": {"data": pcm}}]}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.tts("آپ کا حساب تیار ہے", "ur")
    assert out[:4] == b"RIFF"
    assert "tts" in seen["url"] and seen["body"]["generationConfig"]["responseModalities"] == ["AUDIO"]


def test_tts_failure_is_llmunavailable(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp(status=500))
    with pytest.raises(llm.LLMUnavailable):
        llm.tts("hello", "en")


def test_stt_prefers_gemini_then_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    calls = []

    def fake_post(url, **kw):
        calls.append("gemini" if "generativelanguage" in url else "groq")
        if "generativelanguage" in url:
            return Resp(status=429)
        return Resp({"text": "تیس فراک"})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.transcribe(b"abc") == "تیس فراک"
    assert calls == ["gemini", "groq"]


def test_stt_gemini_ok(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp(
        {"candidates": [{"content": {"parts": [{"text": "30 dresses"}]}}]}))
    assert llm.transcribe(b"abc") == "30 dresses"


def test_stt_without_keys_raises():
    with pytest.raises(llm.LLMUnavailable):
        llm.transcribe(b"abc")
