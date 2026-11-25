"""One thin wrapper for Gemini, Groq and NVIDIA. All three expose an
OpenAI-compatible chat API, so switching provider is a configuration change.
If one provider is rate-limited or down, the next one is tried automatically."""
from __future__ import annotations

import json
import os
import time
import uuid

import requests

PROVIDERS = {
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "model": "gemini-flash-lite-latest"},
    "groq": {"base": "https://api.groq.com/openai/v1",
             "keys": ["GROQ_API_KEY"], "model": "openai/gpt-oss-120b"},
    "nvidia": {"base": "https://integrate.api.nvidia.com/v1",
               "keys": ["NVIDIA_API_KEY"], "model": "meta/llama-3.3-70b-instruct"},
}
_secrets: dict = {}


def _load_env():
    """Read KEY=value lines from a .env file next to this file (same format the team already uses)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path) and not os.environ.get("SAHELI_SKIP_DOTENV"):
        for line in open(path, encoding="utf-8-sig"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


_load_env()


class LLMUnavailable(Exception):
    pass


def configure(secrets) -> None:
    """Pass st.secrets (or any mapping) once at start-up."""
    global _secrets
    try:
        _secrets = {k: str(secrets[k]) for k in secrets.keys()}
    except Exception:
        _secrets = {}


def _get(name: str) -> str | None:
    return os.environ.get(name) or _secrets.get(name) or None


def _key(p: str) -> str | None:
    for n in PROVIDERS[p]["keys"]:
        if _get(n):
            return _get(n)
    return None


def model_for(p: str) -> str:
    return _get(f"{p.upper()}_MODEL") or PROVIDERS[p]["model"]


def available() -> list[str]:
    order = (_get("PROVIDER_ORDER") or "gemini,groq,nvidia").split(",")
    return [p.strip() for p in order if p.strip() in PROVIDERS and _key(p.strip())]


def status() -> dict:
    av = available()
    return {"mode": "ai" if av else "offline", "providers": av,
            "primary": f"{av[0]} · {model_for(av[0])}" if av else None,
            "voice": bool(_key("groq") or _key("gemini")),
            "google": bool(_key("gemini")), "tts": "gemini" if _key("gemini") else "gtts"}


def _normalize(msg: dict, provider: str, model: str, ms: int) -> dict:
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                      "name": fn.get("name"), "arguments": args})
    return {"content": (msg.get("content") or "").strip(), "tool_calls": calls,
            "provider": provider, "model": model, "ms": ms}


def assistant_message(resp: dict) -> dict:
    """Rebuild an OpenAI-style assistant message so the tool loop can continue."""
    m = {"role": "assistant", "content": resp["content"] or ""}
    if resp["tool_calls"]:
        m["tool_calls"] = [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                           for c in resp["tool_calls"]]
    return m


def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2,
         max_tokens: int = 900, json_mode: bool = False, timeout: int = 30) -> dict:
    providers = available()
    if not providers:
        raise LLMUnavailable("No API key configured")
    last_err = None
    for p in providers:
        cfg, model = PROVIDERS[p], model_for(p)
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode and not tools:
            body["response_format"] = {"type": "json_object"}
        for attempt in range(2):
            t0 = time.time()
            try:
                r = requests.post(f"{cfg['base']}/chat/completions",
                                  headers={"Authorization": f"Bearer {_key(p)}", "Content-Type": "application/json"},
                                  json=body, timeout=timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    last_err = f"{p}: HTTP {r.status_code}"
                    time.sleep(1.2 * (attempt + 1))
                    continue
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                return _normalize(msg, p, model, int((time.time() - t0) * 1000))
            except Exception as e:  # network, auth, bad JSON: try next provider
                last_err = f"{p}: {type(e).__name__}"
                break
    raise LLMUnavailable(last_err or "all providers failed")


def _gemini_stt(audio_bytes: bytes, mime: str) -> str:
    import base64
    body = {"contents": [{"role": "user", "parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio_bytes).decode()}},
        {"text": "Transcribe this audio exactly. Write Urdu in Urdu script. Output only the transcript."}]}],
        "generationConfig": {"temperature": 0}}
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model_for('gemini')}:generateContent",
                      headers={"x-goog-api-key": _key("gemini")}, json=body, timeout=45)
    r.raise_for_status()
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()


def _groq_stt(audio_bytes: bytes, filename: str, language: str, mime: str) -> str:
    r = requests.post(f"{PROVIDERS['groq']['base']}/audio/transcriptions",
                      headers={"Authorization": f"Bearer {_key('groq')}"},
                      files={"file": (filename, audio_bytes, mime)},
                      data={"model": _get("GROQ_STT_MODEL") or "whisper-large-v3", "language": language,
                            "response_format": "json", "temperature": "0"}, timeout=45)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def transcribe(audio_bytes: bytes, filename: str = "audio.wav", language: str = "ur", mime: str = "audio/wav") -> str:
    """Speech to text. Gemini audio understanding first (Google stack), Groq Whisper as the fallback.
    Set STT_ORDER=groq,gemini to flip the order."""
    order = [x.strip() for x in (_get("STT_ORDER") or "gemini,groq").split(",")]
    errors = []
    for name in order:
        if name not in ("gemini", "groq") or not _key(name):
            continue
        try:
            txt = _gemini_stt(audio_bytes, mime) if name == "gemini" else _groq_stt(audio_bytes, filename, language, mime)
            if txt:
                return txt
            errors.append(f"{name}: empty")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
    raise LLMUnavailable("; ".join(errors) or "Voice needs a GEMINI_API_KEY or GROQ_API_KEY")


def pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM (what Gemini TTS returns) in a WAV header so browsers can play it."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def tts(text: str, lang: str = "ur") -> bytes:
    """Text to speech with the Gemini TTS model. Returns WAV bytes. Raises LLMUnavailable on any failure
    so the caller can fall back to another voice."""
    import base64
    if not _key("gemini"):
        raise LLMUnavailable("Gemini TTS needs GEMINI_API_KEY")
    model = _get("GEMINI_TTS_MODEL") or "gemini-3.1-flash-tts-preview"
    voice = _get("GEMINI_TTS_VOICE") or "Kore"
    style = ("Read this in clear, warm, unhurried Urdu, like a kind neighbour helping with paperwork: "
             if lang == "ur" else "Read this in a clear, warm, unhurried voice: ")
    body = {"contents": [{"parts": [{"text": style + text[:600]}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    try:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": _key("gemini")}, json=body, timeout=45)
        r.raise_for_status()
        for part in r.json()["candidates"][0]["content"]["parts"]:
            data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
            if data:
                return pcm_to_wav(base64.b64decode(data))
    except Exception as e:
        raise LLMUnavailable(f"gemini tts: {type(e).__name__}") from e
    raise LLMUnavailable("gemini tts: no audio returned")
