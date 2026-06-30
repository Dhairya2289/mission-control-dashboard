"""
Mission Control — automation hooks (additive module).

Small, self-contained endpoints used by the host-side automation services
under ~/voice/automation/:

  · hypr-autopomodoro  — already uses the existing
        POST /api/pomodoro-today/increment   (defined in main.py)
  · vault-reindex      — needs a re-index trigger for the study vault, which
        the dashboard did not previously expose. This module adds it.

Routes:
  GET  /api/automation/health            -> {"ok": true, "subjects_dir": "...", "exists": bool}
  POST /api/automation/reindex-subjects  -> re-scan SUBJECTS_DIR (read-only),
                                            return fresh subject/asset counts

Everything here is READ-ONLY against the filesystem — it never writes to the
vault, the DBs, or ~/.hermes. The "re-index" is a fresh walk of SUBJECTS_DIR
whose counts the frontend can consume; there is no persistent index to mutate,
so re-scanning on demand is the correct, side-effect-free operation.

Self-contained APIRouter, mounted by main.py with a single include line.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter()

# Mirror main.py's SUBJECTS_DIR (resolved centrally in config.py).
SUBJECTS_DIR = config.SUBJECTS_DIR

# File-type buckets mirror main.py's asset-count walk.
_NOTE_EXTS = {".md", ".markdown", ".txt"}
_PDF_EXTS = {".pdf"}
_QUIZ_HINT = "quiz"  # filenames containing this are counted as quizzes


def _scan_subjects() -> dict[str, object]:
    """Walk SUBJECTS_DIR read-only and tally subjects/notes/pdfs/quizzes.

    Degrades gracefully: a missing directory yields zero counts and
    exists=False rather than raising.
    """
    out: dict[str, object] = {
        "subjects_dir": str(SUBJECTS_DIR),
        "exists": SUBJECTS_DIR.is_dir(),
        "subjects": 0,
        "note_files": 0,
        "pdf_files": 0,
        "quizzes": 0,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
    if not SUBJECTS_DIR.is_dir():
        return out

    try:
        subject_dirs = [p for p in SUBJECTS_DIR.iterdir() if p.is_dir()]
    except OSError:
        return out
    out["subjects"] = len(subject_dirs)

    notes = pdfs = quizzes = 0
    for subj in subject_dirs:
        try:
            for f in subj.rglob("*"):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                name = f.name.lower()
                if ext in _NOTE_EXTS:
                    notes += 1
                    if _QUIZ_HINT in name:
                        quizzes += 1
                elif ext in _PDF_EXTS:
                    pdfs += 1
        except OSError:
            # Unreadable subtree — skip, keep going.
            continue
    out["note_files"] = notes
    out["pdf_files"] = pdfs
    out["quizzes"] = quizzes
    return out


@router.get("/api/automation/health")
async def automation_health() -> JSONResponse:
    """Liveness + whether the study vault directory currently exists."""
    return JSONResponse(
        {
            "ok": True,
            "subjects_dir": str(SUBJECTS_DIR),
            "exists": SUBJECTS_DIR.is_dir(),
        }
    )


@router.post("/api/automation/reindex-subjects")
async def reindex_subjects() -> JSONResponse:
    """Re-scan the study vault and return fresh counts.

    Called by the vault-reindex watcher after the subjects tree settles.
    Read-only: it performs no writes and has no persistent index to update;
    the freshly computed counts ARE the index for the frontend to read.
    """
    result = _scan_subjects()
    return JSONResponse({"ok": True, **result})
