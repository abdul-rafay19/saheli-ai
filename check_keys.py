"""Run:  python check_keys.py   -> shows which providers have a key, sends one test message,
and tests Gemini text-to-speech (the voice replies)."""
import llm
try:
    import streamlit as st
    llm.configure(st.secrets)
except Exception:
    pass
print("Providers with keys:", llm.available() or "none")
for p in llm.available():
    print(f"  {p}: model {llm.model_for(p)}")
try:
    r = llm.chat([{"role": "system", "content": "Reply with one word."}, {"role": "user", "content": "Say OK"}], max_tokens=20)
    print("OK via", r["provider"], "-", r["content"])
except llm.LLMUnavailable as e:
    print("FAILED:", e, "\nSee README > Troubleshooting")
if llm.status()["google"]:
    try:
        wav = llm.tts("Assalam o Alaikum", "en")
        print(f"Gemini TTS OK ({len(wav)//1024} KB of audio)")
    except llm.LLMUnavailable as e:
        print("Gemini TTS FAILED:", e, "(voice replies will fall back to gTTS; set GEMINI_TTS_MODEL if the model name changed)")
