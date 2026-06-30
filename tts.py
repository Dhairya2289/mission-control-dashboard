"""
Mission Control — text-to-speech / read-aloud router (additive module).

Bridges the browser to the local Kokoro TTS synthesizer (CPU/ONNX). The frontend
POSTs text here; we subprocess the dedicated-venv speak.py, which renders a 24 kHz
WAV, and stream that WAV straight back. Nothing leaves the machine — TTS is fully
local (no API key, no network).

FAST PATH (warm microservice): a dedicated kokoro-tts.service (systemd --user,
127.0.0.1:51765) keeps the ONNX model resident in RAM and synthesizes in-process.
When it's healthy we FORWARD there via httpx — no per-call model reload (~1.3 s
saved every request). The contract + response headers here are unchanged.

SLOW PATH (fallback): if the warm service is down, we transparently fall back to
subprocess-calling the venv speak.py, which reloads the ~310 MB ONNX model per
call (~1-2 s). So read-aloud keeps working even if the warm unit isn't running.

Self-contained APIRouter, mounted by main.py with a single include line.
Routes:
  GET  /api/voice/voices  -> {"voices": [...], "default": "af_heart"}
  POST /api/voice/speak   -> audio/wav  (body: {"text": "...", "voice"?: "..."})
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

import config

router = APIRouter()

# ── Warm TTS microservice (fast path) ────────────────────────────────────────
# kokoro-tts.service loads the model once and synthesizes in-process. We forward
# to it when healthy and fall back to the subprocess speak.py path when not.
WARM_TTS_URL = os.environ.get("KOKORO_TTS_URL", "http://127.0.0.1:51765")
_WARM_HEALTH_TTL = 10.0         # seconds to cache a health probe result
_warm_state: dict = {"healthy": False, "checked_at": 0.0}


async def _warm_healthy() -> bool:
    """Is the warm TTS service up + model-ready? Cached for a few seconds so we
    don't probe on every request, but recover quickly once it (re)starts."""
    now = time.monotonic()
    if now - _warm_state["checked_at"] < _WARM_HEALTH_TTL:
        return _warm_state["healthy"]
    healthy = False
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{WARM_TTS_URL}/health")
            if r.status_code == 200:
                healthy = bool(r.json().get("ready"))
    except Exception:  # noqa: BLE001 — any failure == not healthy, use fallback
        healthy = False
    _warm_state["healthy"] = healthy
    _warm_state["checked_at"] = now
    return healthy

# Dedicated TTS venv + synthesizer (NOT the dashboard venv — speak.py imports
# kokoro-onnx/onnxruntime which live only in this isolated venv).
TTS_DIR = config.VOICE_TTS_DIR
TTS_PY = os.path.join(TTS_DIR, ".venv", "bin", "python")
TTS_SCRIPT = os.path.join(TTS_DIR, "speak.py")

DEFAULT_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
MAX_TEXT_CHARS = 4000           # read-aloud is for paragraphs, not books
SYNTH_TIMEOUT = 120.0           # CPU synth of a full 4k-char block can take a while

# Fallback voice list (used only if the live --list-voices probe fails); the
# real set is queried from the model so this never goes stale silently.
_FALLBACK_VOICES = ["af_heart", "af_sarah", "af_bella", "af_nicole", "am_adam", "am_michael"]


def _tts_available() -> bool:
    return os.path.exists(TTS_PY) and os.path.exists(TTS_SCRIPT)


@router.get("/api/voice/voices")
async def voice_voices() -> JSONResponse:
    """List available TTS voices.

    Fast path: ask the warm service (model already loaded, instant). Fallback:
    subprocess `speak.py --list-voices` (reloads the model). Same JSON shape.
    """
    # Fast path — warm service has the voice list in memory.
    if await _warm_healthy():
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{WARM_TTS_URL}/voices")
            if r.status_code == 200:
                data = r.json()
                voices = data.get("voices") or _FALLBACK_VOICES
                return JSONResponse({
                    "voices": voices,
                    "default": data.get("default", DEFAULT_VOICE),
                    "available": True,
                })
        except Exception:  # noqa: BLE001 — drop to subprocess path below
            pass

    if not _tts_available():
        # Degrade gracefully — surface defaults rather than 500ing the UI.
        return JSONResponse(
            {"voices": _FALLBACK_VOICES, "default": DEFAULT_VOICE, "available": False}
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            TTS_PY, TTS_SCRIPT, "--list-voices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except Exception:  # noqa: BLE001 — probe failure == use fallback
        return JSONResponse(
            {"voices": _FALLBACK_VOICES, "default": DEFAULT_VOICE, "available": True}
        )

    voices = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    if not voices:
        voices = _FALLBACK_VOICES
    return JSONResponse({"voices": voices, "default": DEFAULT_VOICE, "available": True})


@router.post("/api/voice/speak")
async def voice_speak(payload: dict):
    """Synthesize `text` to speech and return a WAV (audio/wav).

    Body: {"text": "...", "voice"?: "af_heart"}.

    Fast path: forward to the warm kokoro-tts service (model resident, no reload)
    and stream its WAV bytes straight back. Fallback: if the warm service is down,
    subprocess the venv speak.py (reloads the model) and stream the temp WAV. The
    external contract (audio/wav, same body) is identical either way.
    """
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]

    voice = str(payload.get("voice") or DEFAULT_VOICE).strip()
    # Defensive: keep voice to a plain identifier so it can't smuggle CLI args /
    # paths into the subprocess. (We use exec, not a shell, but be strict anyway.)
    if not voice.replace("_", "").isalnum():
        voice = DEFAULT_VOICE

    # ── Fast path: forward to the warm service ───────────────────────────────
    if await _warm_healthy():
        try:
            async with httpx.AsyncClient(timeout=SYNTH_TIMEOUT) as client:
                r = await client.post(
                    f"{WARM_TTS_URL}/speak",
                    json={"text": text, "voice": voice},
                )
            if r.status_code == 200 and r.content:
                return Response(
                    content=r.content,
                    media_type="audio/wav",
                    headers={"Content-Disposition": 'inline; filename="speak.wav"'},
                )
            # Non-200 from a "healthy" service: invalidate cache + fall through.
            _warm_state["checked_at"] = 0.0
        except Exception:  # noqa: BLE001 — warm path failed, use subprocess below
            _warm_state["checked_at"] = 0.0

    # ── Fallback: subprocess speak.py (reloads the model per call) ────────────
    if not _tts_available():
        raise HTTPException(
            status_code=503,
            detail="Local TTS not installed. Expected synthesizer at "
                   f"{TTS_SCRIPT} (run the kokoro-tts installer).",
        )

    fd, out_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
    os.close(fd)

    try:
        proc = await asyncio.create_subprocess_exec(
            TTS_PY, TTS_SCRIPT, "--out", out_path, "--voice", voice,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(input=text.encode("utf-8")), timeout=SYNTH_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        _cleanup(out_path)
        raise HTTPException(status_code=504, detail="TTS synthesis timed out") from e
    except Exception as e:  # noqa: BLE001
        _cleanup(out_path)
        raise HTTPException(status_code=503, detail=f"TTS synthesis failed to start: {e}") from e

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        msg = stderr.decode("utf-8", "replace")[-300:] if stderr else "unknown error"
        _cleanup(out_path)
        raise HTTPException(status_code=502, detail=f"TTS error: {msg}")

    # Stream the WAV; delete the temp file once the response is fully sent.
    return FileResponse(
        out_path,
        media_type="audio/wav",
        filename="speak.wav",
        background=BackgroundTask(_cleanup, out_path),
    )


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
