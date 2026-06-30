"""
Mission Control — Anki .apkg export router (additive module).

Streams a downloadable Anki deck (.apkg) built from the dashboard's
flashcards. Generation runs in a SEPARATE venv (genanki lives only there,
never in the FastAPI venv) — this router subprocesses that venv's python
running ~/voice/anki/export.py, which reads the flashcards
DB + on-disk decks (READ-ONLY) and writes a .apkg to a temp path. We then
stream the file back and clean it up afterwards.

Self-contained APIRouter, mounted by main.py with a single include line.
Routes:
  GET /api/anki/health  -> {"available": bool, "venv": "...", "notes": int|None}
  GET /api/anki/export  -> FileResponse (application/octet-stream, .apkg)
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import config

router = APIRouter()

# Dedicated venv + standalone exporter (genanki installed ONLY in this venv).
ANKI_VENV_PY = config.ANKI_VENV_PY
ANKI_EXPORT_SCRIPT = config.ANKI_EXPORT_SCRIPT

# Generation should be quick (small decks); cap so a wedged subprocess can't hang.
EXPORT_TIMEOUT_S = 60.0


async def _run_export(out_path: str) -> dict:
    """Run export.py in its venv; return its parsed JSON summary.

    Raises HTTPException on any failure (missing venv, timeout, bad exit,
    unparseable output).
    """
    if not os.path.exists(ANKI_VENV_PY):
        raise HTTPException(
            status_code=503,
            detail=(
                "Anki export venv missing. Create it: "
                f"python3 -m venv {config.VOICE_DIR}/anki/.venv && "
                f"{config.VOICE_DIR}/anki/.venv/bin/pip install genanki"
            ),
        )
    if not os.path.exists(ANKI_EXPORT_SCRIPT):
        raise HTTPException(status_code=503, detail="export.py not found")

    try:
        proc = await asyncio.create_subprocess_exec(
            ANKI_VENV_PY,
            ANKI_EXPORT_SCRIPT,
            out_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=EXPORT_TIMEOUT_S
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise HTTPException(status_code=504, detail="anki export timed out") from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"anki export failed to start: {e}") from e

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace")[:400]
        raise HTTPException(status_code=500, detail=f"anki export error: {err or 'nonzero exit'}")

    out = (stdout or b"").decode("utf-8", "replace").strip()
    # export.py prints a single JSON line; tolerate trailing noise by taking last line.
    last = out.splitlines()[-1] if out else ""
    try:
        summary = json.loads(last)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="anki export gave no JSON summary") from e
    if not summary.get("ok"):
        raise HTTPException(status_code=500, detail=f"anki export: {summary.get('error', 'unknown')}")
    return summary


@router.get("/api/anki/health")
async def anki_health() -> JSONResponse:
    """Report whether the export venv exists and how many notes would export."""
    available = os.path.exists(ANKI_VENV_PY) and os.path.exists(ANKI_EXPORT_SCRIPT)
    notes: int | None = None
    if available:
        # Dry build into a throwaway temp to report the note count (cheap).
        fd, tmp = tempfile.mkstemp(prefix="anki_health_", suffix=".apkg")
        os.close(fd)
        try:
            summary = await _run_export(tmp)
            notes = int(summary.get("notes", 0))
        except HTTPException:
            available = False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return JSONResponse(
        {"available": available, "venv": ANKI_VENV_PY, "notes": notes}
    )


@router.get("/api/anki/export")
async def anki_export() -> FileResponse:
    """Generate and stream a .apkg of all flashcards (one deck per subject).

    Returns a downloadable Anki package. The temp file is removed after the
    response is sent (BackgroundTask). Works even when there are zero cards —
    you get a valid, empty, openable .apkg.
    """
    fd, out_path = tempfile.mkstemp(prefix="mission-control-flashcards_", suffix=".apkg")
    os.close(fd)

    try:
        summary = await _run_export(out_path)
    except Exception:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="anki export produced no file")

    stamp = time.strftime("%Y%m%d")
    download_name = f"mission-control-flashcards-{stamp}.apkg"

    def _cleanup(path: str = out_path) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    return FileResponse(
        out_path,
        media_type="application/octet-stream",
        filename=download_name,
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "X-Anki-Notes": str(summary.get("notes", 0)),
            "X-Anki-Decks": str(summary.get("decks", 0)),
        },
        background=BackgroundTask(_cleanup),
    )
