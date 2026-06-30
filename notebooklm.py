"""
Mission Control — NotebookLM router (additive module).

Replaces the former Open Notebook proxy. Talks to Google NotebookLM through the
unofficial `notebooklm-py` CLI (v0.7.x), installed in its OWN virtualenv at
`~/.notebooklm-venv` and symlinked to `~/bin/notebooklm`. The dashboard service
venv is never touched — every call shells out to the CLI binary via
`asyncio.create_subprocess_exec` (list-form argv → no shell injection).

Design contract:
  · Notebook context is bound per-call with the `NOTEBOOKLM_NOTEBOOK` env var
    (the CLI honours it everywhere) — NOT the global `use` state file, so
    concurrent requests never race.
  · Auth is interactive (Google sign-in writes
    ~/.notebooklm/profiles/default/storage_state.json). When the CLI is missing
    or unauthenticated, every endpoint returns HTTP 200 with
    {"ok": false, "reason": "cli-missing" | "auth-missing" | "timeout" |
     "parse-error" | "cli-error"} so the SPA can render a clean empty state.
    Only malformed client input yields 4xx.
  · Generation is long-running and rate-limit prone → always fired with
    --no-wait; the frontend polls `artifact list` / `artifact poll`.
  · Downloads use a tiny helper (`nlm_download.py`) run with the notebooklm venv
    python, since the CLI no longer exposes a download command.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

import config

router = APIRouter()

# ---------------------------------------------------------------------------
# Paths / binaries (resolved lazily; import must never crash)
# ---------------------------------------------------------------------------
HOME = Path.home()
NLM_HOME = HOME / ".notebooklm"
STORAGE_STATE = NLM_HOME / "profiles" / "default" / "storage_state.json"
VENV_PY = HOME / ".notebooklm-venv" / "bin" / "python"
DOWNLOAD_HELPER = Path(__file__).resolve().parent / "nlm_download.py"
DOWNLOAD_DIR = config.NLM_DOWNLOAD_DIR

# Scholar research bridge ("Send to NotebookLM") — read-only access to the
# research store owned by main.py. We only read the row to resolve a filename.
RESEARCH_DB = config.RESEARCH_DB
RESEARCH_DIR = config.RESEARCH_DIR


def _resolve_bin() -> str | None:
    for cand in (HOME / "bin" / "notebooklm", HOME / ".notebooklm-venv" / "bin" / "notebooklm"):
        if cand.exists():
            return str(cand)
    found = shutil.which("notebooklm")
    return found


NLM_BIN: str | None = _resolve_bin()

# Timeouts (seconds)
QUICK = 25.0      # auth/list/status — fast metadata calls
DEFAULT = 70.0    # create / source list / metadata
ADD = 120.0       # source add (URL fetch happens server-side at Google)
ASK = 200.0       # chat answer (vector search + LLM)
GEN = 90.0        # generate is --no-wait, returns a task id quickly

# Input caps
MAX_ARG_LEN = 4000
MAX_TITLE = 300
MAX_QUESTION = 4000
MAX_URL = 2000

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")        # notebook/artifact ids
_LANG_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

# Whitelisted generators + their allowed extra flags (value-validated below).
_GEN_TYPES = {
    "audio": {"format": {"deep-dive", "brief", "critique", "debate"},
              "length": {"short", "default", "long"}},
    "video": {},
    "report": {"format": {"briefing-doc", "study-guide", "blog-post", "custom"}},
    "quiz": {"difficulty": {"easy", "medium", "hard"},
             "quantity": {"fewer", "standard", "more"}},
    "flashcards": {"difficulty": {"easy", "medium", "hard"},
                   "quantity": {"fewer", "standard", "more"}},
    "infographic": {"orientation": {"landscape", "portrait", "square"},
                    "detail": {"concise", "standard", "detailed"}},
    "mind-map": {},
    "data-table": {},
    "slide-deck": {"format": {"detailed", "presenter"},
                   "length": {"default", "short"}},
}

# Download type -> (file extension, allowed --format values)
_DL_EXT = {
    "audio": ("mp3", set()),
    "video": ("mp4", set()),
    "infographic": ("png", set()),
    "report": ("md", set()),
    "mind-map": ("json", set()),
    "data-table": ("csv", set()),
    "slide-deck": ("pdf", {"pdf", "pptx"}),
    "quiz": ("json", {"json", "md", "html"}),
    "flashcards": ("json", {"json", "md", "html"}),
}

# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _env(notebook_id: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(HOME)
    extra = f"{HOME}/bin:{HOME}/.notebooklm-venv/bin"
    env["PATH"] = extra + ":" + env.get("PATH", "")
    if notebook_id:
        env["NOTEBOOKLM_NOTEBOOK"] = notebook_id
    return env


def _validate_args(args: list[str]) -> None:
    for a in args:
        if not isinstance(a, str):
            raise HTTPException(status_code=400, detail="non-string CLI argument")
        if len(a) > MAX_ARG_LEN:
            raise HTTPException(status_code=400, detail="CLI argument too long")


async def _run(args: list[str], *, timeout: float, notebook_id: str | None = None) -> tuple[int, str, str]:
    """Run the notebooklm CLI. Returns (returncode, stdout, stderr).
    returncode sentinels: -1 cli-missing, -2 timeout, -3 spawn-failure."""
    if not NLM_BIN:
        return (-1, "", "notebooklm CLI not installed")
    _validate_args(args)
    argv = [NLM_BIN, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(notebook_id),
            cwd=str(HOME),
        )
    except Exception as e:  # noqa: BLE001
        return (-3, "", f"spawn failed: {e}")
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return (-2, "", f"timed out after {timeout:.0f}s")
    return (proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace"))


def _parse_json(raw: str) -> tuple[bool, Any]:
    raw = (raw or "").strip()
    if not raw:
        return (False, None)
    try:
        return (True, json.loads(raw))
    except Exception:  # noqa: BLE001
        # CLI sometimes prints a human line around the JSON — slice to the
        # outermost bracket pair and retry.
        for open_c, close_c in (("{", "}"), ("[", "]")):
            i, j = raw.find(open_c), raw.rfind(close_c)
            if i != -1 and j > i:
                try:
                    return (True, json.loads(raw[i:j + 1]))
                except Exception:  # noqa: BLE001
                    continue
        return (False, raw[:2000])


def _fail(reason: str, **extra: Any) -> JSONResponse:
    body = {"ok": False, "reason": reason}
    body.update(extra)
    return JSONResponse(status_code=200, content=body)


def _check_id(value: str, what: str) -> str:
    if not value or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"invalid {what}")
    return value


# ---------------------------------------------------------------------------
# Auth status (cached)
# ---------------------------------------------------------------------------
_auth_cache: dict[str, Any] = {"t": 0.0, "auth": False}


async def _is_authed() -> bool:
    if not STORAGE_STATE.exists():
        _auth_cache.update(t=time.monotonic(), auth=False)
        return False
    now = time.monotonic()
    if (now - _auth_cache["t"]) < 60 and _auth_cache["t"]:
        return bool(_auth_cache["auth"])
    rc, _out, _err = await _run(["auth", "check"], timeout=QUICK)
    ok = rc == 0
    _auth_cache.update(t=now, auth=ok)
    return ok


async def _guard() -> str | None:
    """Return a failure reason ("cli-missing"/"auth-missing") if the CLI is
    missing or unauthenticated, else None (caller proceeds)."""
    if not NLM_BIN:
        return "cli-missing"
    if not await _is_authed():
        return "auth-missing"
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/api/nlm/health")
async def nlm_health() -> dict[str, Any]:
    cli = NLM_BIN is not None
    auth = await _is_authed() if cli else False
    version = None
    if cli:
        rc, out, _err = await _run(["--version"], timeout=QUICK)
        if rc == 0:
            version = out.strip() or None
    return {
        "ok": bool(cli and auth),
        "cli": cli,
        "auth": auth,
        "bin": NLM_BIN,
        "storage": str(STORAGE_STATE) if auth else None,
        "version": version,
    }


@router.get("/api/nlm/notebooks")
async def nlm_list_notebooks():
    g = await _guard()
    if g:
        return _fail(g, notebooks=[])
    rc, out, err = await _run(["list", "--json"], timeout=DEFAULT)
    if rc != 0:
        return _fail("cli-error", notebooks=[], detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", notebooks=[], raw=data)
    notebooks = data.get("notebooks", data) if isinstance(data, dict) else data
    return {"ok": True, "notebooks": notebooks or []}


@router.post("/api/nlm/notebooks")
async def nlm_create_notebook(payload: dict[str, Any]):
    g = await _guard()
    if g:
        return _fail(g)
    title = (payload or {}).get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    if len(title) > MAX_TITLE:
        raise HTTPException(status_code=400, detail="title too long")
    rc, out, err = await _run(["create", title, "--json"], timeout=DEFAULT)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", raw=data)
    return {"ok": True, "notebook": data}


@router.get("/api/nlm/notebooks/{notebook_id}/metadata")
async def nlm_metadata(notebook_id: str):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g)
    rc, out, err = await _run(["metadata", "--json"], timeout=DEFAULT, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", raw=data)
    return {"ok": True, "metadata": data}


@router.get("/api/nlm/notebooks/{notebook_id}/sources")
async def nlm_list_sources(notebook_id: str):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g, sources=[])
    rc, out, err = await _run(["source", "list", "--json"], timeout=DEFAULT, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", sources=[], detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", sources=[], raw=data)
    sources = data.get("sources", data) if isinstance(data, dict) else data
    return {"ok": True, "sources": sources or []}


@router.post("/api/nlm/notebooks/{notebook_id}/sources")
async def nlm_add_source(notebook_id: str, payload: dict[str, Any]):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g)
    payload = payload or {}
    kind = (payload.get("kind") or "url").strip()
    value = (payload.get("value") or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="value required")
    if kind == "url":
        if len(value) > MAX_URL or not re.match(r"^https?://", value, re.I):
            raise HTTPException(status_code=400, detail="value must be an http(s) URL")
        rc, out, err = await _run(["source", "add", value, "--type", "url"],
                                  timeout=ADD, notebook_id=notebook_id)
    elif kind == "youtube":
        if len(value) > MAX_URL or not re.match(r"^https?://", value, re.I):
            raise HTTPException(status_code=400, detail="value must be a URL")
        rc, out, err = await _run(["source", "add", value, "--type", "youtube"],
                                  timeout=ADD, notebook_id=notebook_id)
    elif kind == "text":
        if len(value) > MAX_ARG_LEN:
            raise HTTPException(status_code=400, detail="text too long")
        title = (payload.get("title") or "").strip()[:MAX_TITLE]
        args = ["source", "add", value, "--type", "text"]
        if title:
            args += ["--title", title]
        rc, out, err = await _run(args, timeout=ADD, notebook_id=notebook_id)
    elif kind == "research":
        if len(value) > MAX_QUESTION:
            raise HTTPException(status_code=400, detail="query too long")
        mode = (payload.get("mode") or "fast").strip()
        if mode not in {"fast", "deep"}:
            mode = "fast"
        args = ["source", "add-research", value, "--mode", mode, "--no-wait"]
        if payload.get("import_all"):
            args.append("--import-all")
        rc, out, err = await _run(args, timeout=ADD, notebook_id=notebook_id)
    else:
        raise HTTPException(status_code=400, detail=f"unknown source kind: {kind}")
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    return {"ok": True, "result": data if ok else (out or "").strip()[:500], "kind": kind}


@router.post("/api/nlm/notebooks/{notebook_id}/sources/from-research")
async def nlm_add_research_source(notebook_id: str, payload: dict[str, Any]):
    """Bridge the Scholar Research tab into NotebookLM: upload a finished research
    Markdown doc into the given notebook as a file source. Reads research.db
    read-only to resolve the filename, then confines the path to RESEARCH_DIR."""
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g)
    raw_id = (payload or {}).get("research_id")
    try:
        rid = int(raw_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="research_id required")
    if not RESEARCH_DB.exists():
        return _fail("research-missing", detail="no research database")
    try:
        con = sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT title, filename, status FROM research WHERE id = ?", (rid,)
        ).fetchone()
        con.close()
    except Exception as e:  # noqa: BLE001
        return _fail("research-missing", detail=str(e)[:200])
    if not row:
        raise HTTPException(status_code=404, detail="research not found")
    filename = (row["filename"] or "").strip()
    if not filename:
        return _fail("doc-missing", detail="research has no saved file")
    # Confine to RESEARCH_DIR: reject traversal and symlink escape.
    orig = RESEARCH_DIR / filename
    if orig.is_symlink():
        return _fail("doc-missing", detail="symlinked research file rejected")
    path = orig.resolve()
    base = RESEARCH_DIR.resolve()
    if base != path.parent and base not in path.parents:
        raise HTTPException(status_code=400, detail="invalid research path")
    if not path.exists() or not path.is_file():
        return _fail("doc-missing", detail="research file not found on disk")
    title = (row["title"] or "Research")[:MAX_TITLE]
    rc, out, err = await _run(
        ["source", "add", str(path), "--type", "file", "--title", title],
        timeout=ADD, notebook_id=notebook_id,
    )
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    return {"ok": True, "result": data if ok else (out or "").strip()[:500], "title": title}


@router.post("/api/nlm/notebooks/{notebook_id}/ask")
async def nlm_ask(notebook_id: str, payload: dict[str, Any]):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g)
    payload = payload or {}
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    if len(question) > MAX_QUESTION:
        raise HTTPException(status_code=400, detail="question too long")
    args = ["ask", question, "--json"]
    for sid in (payload.get("sources") or [])[:20]:
        if isinstance(sid, str) and _ID_RE.match(sid):
            args += ["-s", sid]
    rc, out, err = await _run(args, timeout=ASK, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        # ask without --json shape sometimes prints plain text — still useful
        return {"ok": True, "answer": (out or "").strip(), "citations": [], "raw": True}
    answer = ""
    citations: list[Any] = []
    if isinstance(data, dict):
        answer = data.get("answer") or data.get("response") or data.get("text") or ""
        citations = data.get("references") or data.get("citations") or data.get("sources") or []
    else:
        answer = str(data)
    return {"ok": True, "answer": answer, "citations": citations, "data": data}


@router.post("/api/nlm/notebooks/{notebook_id}/generate")
async def nlm_generate(notebook_id: str, payload: dict[str, Any]):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g)
    payload = payload or {}
    art_type = (payload.get("type") or "").strip()
    if art_type not in _GEN_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported artifact type: {art_type}")
    description = (payload.get("description") or "").strip()
    if len(description) > MAX_QUESTION:
        raise HTTPException(status_code=400, detail="description too long")
    args = ["generate", art_type]
    if description:
        args.append(description)
    allowed = _GEN_TYPES[art_type]
    options = payload.get("options") or {}
    if isinstance(options, dict):
        for key, allowed_vals in allowed.items():
            val = options.get(key)
            if val and isinstance(val, str) and val in allowed_vals:
                args += [f"--{key}", val]
    for sid in (payload.get("sources") or [])[:20]:
        if isinstance(sid, str) and _ID_RE.match(sid):
            args += ["-s", sid]
    args += ["--no-wait", "--json", "--retry", "1"]
    rc, out, err = await _run(args, timeout=GEN, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", raw=data)
    return {"ok": True, "artifact": data, "type": art_type}


@router.get("/api/nlm/notebooks/{notebook_id}/artifacts")
async def nlm_list_artifacts(notebook_id: str):
    _check_id(notebook_id, "notebook id")
    g = await _guard()
    if g:
        return _fail(g, artifacts=[])
    rc, out, err = await _run(["artifact", "list", "--json"], timeout=DEFAULT, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", artifacts=[], detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", artifacts=[], raw=data)
    artifacts = data.get("artifacts", data) if isinstance(data, dict) else data
    return {"ok": True, "artifacts": artifacts or []}


@router.get("/api/nlm/notebooks/{notebook_id}/artifacts/{artifact_id}")
async def nlm_get_artifact(notebook_id: str, artifact_id: str):
    _check_id(notebook_id, "notebook id")
    _check_id(artifact_id, "artifact id")
    g = await _guard()
    if g:
        return _fail(g)
    rc, out, err = await _run(["artifact", "get", artifact_id, "--json"],
                              timeout=DEFAULT, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", raw=data)
    return {"ok": True, "artifact": data}


@router.get("/api/nlm/notebooks/{notebook_id}/poll/{task_id}")
async def nlm_poll(notebook_id: str, task_id: str):
    _check_id(notebook_id, "notebook id")
    _check_id(task_id, "task id")
    g = await _guard()
    if g:
        return _fail(g)
    rc, out, err = await _run(["artifact", "poll", task_id, "--json"],
                              timeout=QUICK, notebook_id=notebook_id)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:500])
    ok, data = _parse_json(out)
    if not ok:
        return _fail("parse-error", raw=data)
    return {"ok": True, "status": data}


@router.get("/api/nlm/notebooks/{notebook_id}/artifacts/{artifact_id}/download")
async def nlm_download(notebook_id: str, artifact_id: str, type: str = "", format: str = ""):
    _check_id(notebook_id, "notebook id")
    _check_id(artifact_id, "artifact id")
    g = await _guard()
    if g:
        return _fail(g)
    art_type = (type or "").strip()
    if art_type not in _DL_EXT:
        raise HTTPException(status_code=400, detail=f"unsupported download type: {art_type}")
    ext, allowed_fmts = _DL_EXT[art_type]
    fmt = (format or "").strip()
    if fmt:
        if fmt not in allowed_fmts:
            raise HTTPException(status_code=400, detail=f"format not allowed for {art_type}")
        ext = fmt
    if not VENV_PY.exists() or not DOWNLOAD_HELPER.exists():
        return _fail("cli-missing", detail="download helper unavailable")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = DOWNLOAD_DIR / notebook_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{artifact_id}.{ext}"
    argv = [str(VENV_PY), str(DOWNLOAD_HELPER), notebook_id, artifact_id, art_type, str(out_path)]
    if fmt:
        argv.append(fmt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env(notebook_id),
            cwd=str(HOME),
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except asyncio.TimeoutError:
        return _fail("timeout", detail="download timed out")
    except Exception as e:  # noqa: BLE001
        return _fail("cli-error", detail=str(e)[:300])
    ok, data = _parse_json(out_b.decode("utf-8", "replace"))
    if not ok or not isinstance(data, dict) or not data.get("ok"):
        reason = (data.get("error") if isinstance(data, dict) else None) or err_b.decode("utf-8", "replace")[:300]
        return _fail("download-failed", detail=reason or "download failed")
    if not out_path.exists():
        return _fail("download-failed", detail="file not produced")
    media = {
        "mp3": "audio/mpeg", "mp4": "video/mp4", "png": "image/png",
        "md": "text/markdown", "json": "application/json", "csv": "text/csv",
        "pdf": "application/pdf", "html": "text/html",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(ext, "application/octet-stream")
    return FileResponse(str(out_path), filename=out_path.name, media_type=media)


@router.get("/api/nlm/languages")
async def nlm_languages():
    g = await _guard()
    if g:
        return _fail(g, languages=[], current="en")
    rc, out, _err = await _run(["language", "list", "--json"], timeout=QUICK)
    languages: Any = []
    if rc == 0:
        ok, data = _parse_json(out)
        if ok:
            languages = data.get("languages", data) if isinstance(data, dict) else data
    rc2, out2, _e2 = await _run(["language", "get"], timeout=QUICK)
    current = "en"
    if rc2 == 0 and out2.strip() and "not set" not in out2.lower():
        # e.g. "Language: ja" → take the last bare token if it looks like a code
        tok = out2.strip().split()[-1].strip(".'\")")
        if _LANG_RE.match(tok):
            current = tok
    return {"ok": True, "languages": languages or [], "current": current}


@router.post("/api/nlm/languages")
async def nlm_set_language(payload: dict[str, Any]):
    g = await _guard()
    if g:
        return _fail(g)
    code = ((payload or {}).get("code") or "").strip()
    if not code or not _LANG_RE.match(code):
        raise HTTPException(status_code=400, detail="invalid language code")
    rc, out, err = await _run(["language", "set", code], timeout=QUICK)
    if rc != 0:
        return _fail("cli-error", detail=(err or out)[:300])
    return {"ok": True, "current": code}
