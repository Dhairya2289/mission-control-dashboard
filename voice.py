"""
Mission Control — voice input router (additive module).

Bridges the browser to the local whisper.cpp STT service (whisper-stt.service,
127.0.0.1:51764). The dashboard frontend records a short clip with MediaRecorder
and POSTs it here; we forward it to whisper-server's /inference endpoint and
return the transcript. Nothing leaves the machine — STT is fully local.

Self-contained APIRouter, mounted by main.py with a single include line.
Routes:
  GET  /api/voice/health      -> {"available": bool, "port": int}
  POST /api/voice/transcribe  -> {"ok": true, "text": "..."}
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter()

# whisper-stt.service — localhost only, model kept warm in RAM.
STT_PORT = int(os.environ.get("WHISPER_STT_PORT", "51764"))
STT_URL = f"http://127.0.0.1:{STT_PORT}/inference"
STT_BASE = f"http://127.0.0.1:{STT_PORT}/"

# Guard: a voice command is short. Reject anything implausibly large.
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB (~ a few minutes of opus)


@router.get("/api/voice/health")
async def voice_health() -> JSONResponse:
    """Report whether the local STT service is reachable."""
    available = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(STT_BASE)
            available = resp.status_code < 500
    except Exception:  # noqa: BLE001 — unreachable == unavailable
        available = False
    return JSONResponse({"available": available, "port": STT_PORT})


@router.post("/api/voice/transcribe")
async def voice_transcribe(audio: UploadFile = File(..., description="Recorded audio clip")) -> JSONResponse:
    """Forward a recorded clip to whisper-server and return the transcript.

    whisper-server runs with --convert, so any container ffmpeg understands
    (webm/opus from MediaRecorder, ogg, wav, mp3…) is accepted.
    """
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio clip too large")

    filename = audio.filename or "clip.webm"
    content_type = audio.content_type or "audio/webm"

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                STT_URL,
                files={"file": (filename, data, content_type)},
                data={
                    "response_format": "json",
                    "temperature": "0.0",
                    "no_timestamps": "true",
                    # 0 = full window. base.en is fast enough that we don't trim
                    # the encoder, so longer clips aren't cut off. Set
                    # WHISPER_STT_DASH_AC=768 to trim if you switch to a heavy model.
                    "audio_ctx": os.environ.get("WHISPER_STT_DASH_AC", "0"),
                },
            )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="Local STT service unavailable. Check: systemctl --user status whisper-stt",
        ) from e

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"STT error {resp.status_code}: {resp.text[:200]}")

    try:
        text = (resp.json().get("text") or "").strip()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="unexpected STT response shape") from e

    # whisper emits these for silence/non-speech; surface as empty so the UI can hint.
    if text in ("[BLANK_AUDIO]", "(silence)", "[silence]", "[ Silence ]"):
        text = ""

    return JSONResponse({"ok": True, "text": text})


# Non-ASCII (Devanagari etc.) means there's something to translate; pure-ASCII
# text is already English/romanized and is returned untouched.
def _looks_translatable(text: str) -> bool:
    return any(ord(ch) > 0x7F for ch in text)


@router.post("/api/voice/translate")
async def voice_translate(payload: dict) -> JSONResponse:
    """Translate text to English via the configured LLM (TokenRouter/MiniMax).

    Used by the 'translate to English' dictation mode: whisper.cpp (esp.
    large-v3-turbo) transcribes Hindi/Hinglish faithfully, then this turns it
    into clean English. Pure-English input is returned unchanged (no LLM call).
    """
    import asyncio

    text = str(payload.get("text", "")).strip()
    if not text:
        return JSONResponse({"ok": True, "text": ""})
    if not _looks_translatable(text):
        # already Latin script — assume English/romanized, don't burn an LLM call
        return JSONResponse({"ok": True, "text": text, "translated": False})

    try:
        from tools import _llm_chat
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"LLM not available: {e}") from e

    sys_prompt = (
        "You are a translation engine. Translate the user's message into natural, "
        "fluent English. The input may be Hindi, Hinglish (Hindi written in Latin "
        "or Devanagari mixed with English), or already English. If it is already "
        "English, return it unchanged. Output ONLY the English translation — no "
        "quotes, no notes, no original text, no explanations."
    )
    try:
        english = await asyncio.to_thread(
            _llm_chat,
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}],
            temperature=0.2,
            max_tokens=1200,
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"translation failed: {e}") from e

    return JSONResponse({"ok": True, "text": english.strip(), "translated": True})
