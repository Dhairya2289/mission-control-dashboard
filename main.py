"""
Mission Control Dashboard — FastAPI backend.

Private service. Binds 127.0.0.1:51763 only. Reach from a laptop via SSH tunnel.
The dashboard reads study data and can trigger agents, so the port stays closed
to the network on purpose.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config

# ---------------------------------------------------------------------------
# Paths — all resolved in config.py from environment variables (see .env.example).
# Names kept identical to their historical values so the rest of this module is
# unchanged; only the source of truth moved.
# ---------------------------------------------------------------------------
HERMES_PYTHON = config.HERMES_PYTHON
HERMES_HOME_BILL = config.HERMES_HOME
PROFILES_DIR = config.PROFILES_DIR
AGENT_LOG_DB = config.AGENT_LOG_DB
SUBJECTS_DIR = config.SUBJECTS_DIR
RESEARCH_DB = config.RESEARCH_DB
QUIZ_DB = config.QUIZ_DB
FLASHCARD_DB = config.FLASHCARD_DB
PRODUCTIVITY_DB = config.PRODUCTIVITY_DB
CHAT_DB = config.CHAT_DB
RESEARCH_DIR = config.RESEARCH_DIR
PLANNING_DIR = config.PLANNING_DIR
PLANNING_DIR.mkdir(parents=True, exist_ok=True)

DASHBOARD_DIR = Path(__file__).resolve().parent
STATIC_DIR = DASHBOARD_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# Obsidian vault — point to the canonical vault on this machine. We surface notes
# through the dashboard so the user can browse, search, and read without booting
# the Obsidian desktop app.
# Obsidian vault surfaced through the Obsidian / Brain tabs. Override anywhere
# with $OBSIDIAN_VAULT (see config.py).
OBSIDIAN_VAULT = config.OBSIDIAN_VAULT

PIPELINE_SCRIPT = config.PIPELINE_SCRIPT

# Design reference: canonical visual source of truth for the whole project.
# Every later visual build (page, card, modal, chart, nav, etc.) must study
# this template first and match its design language before touching code.
DESIGN_REF_DIR = DASHBOARD_DIR / "design-reference"
DESIGN_REF_TEMPLATE = DESIGN_REF_DIR / "template.html"
DESIGN_REF_ARCHIVE = DESIGN_REF_DIR / "archive"
DESIGN_REF_SCREENSHOTS = DESIGN_REF_DIR / "screenshots"
DESIGN_REF_PAGE = STATIC_DIR / "design-reference.html"

# Extension policy for uploads.
HTML_EXTS = {".html", ".htm"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file

# Profile avatar — one user-uploaded photo, served as a static asset and shown
# in place of the "D" initial in the app bar.
PROFILE_DIR = STATIC_DIR / "profile"

# Ensure storage exists at import time so endpoints don't race on first upload.
for _d in (DESIGN_REF_DIR, DESIGN_REF_ARCHIVE, DESIGN_REF_SCREENSHOTS, PROFILE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

VERSION = "1.7.0"

# Canonical per-agent identity. Used by every overview panel + the rest of the app.
AGENT_REGISTRY: list[dict[str, Any]] = [
    # Volt palette — each agent carries a companion hue with intent:
    # bill=lime (brand/coordinator hub) · vault=cyan (info) · scholar=emerald (growth)
    # quizmaster=violet (category) · planner=amber (due) · dev=coral (infra/alert)
    {"key": "bill",       "name": "Bill",       "icon": "◎", "emoji": "🧭", "color": "#c8ff00", "role": "Coordinator"},
    {"key": "vault",      "name": "Vault",      "icon": "◆", "emoji": "📁", "color": "#36d6e7", "role": "File Librarian"},
    {"key": "scholar",    "name": "Scholar",    "icon": "✧", "emoji": "📚", "color": "#2be08a", "role": "Notes & Research"},
    {"key": "quizmaster", "name": "Quizmaster", "icon": "◈", "emoji": "🎯", "color": "#b08cff", "role": "Quiz & Flashcards"},
    {"key": "planner",    "name": "Planner",    "icon": "◌", "emoji": "📅", "color": "#ffc24b", "role": "Schedule Manager"},
    {"key": "dev",        "name": "Dev",        "icon": "✦", "emoji": "🛠", "color": "#ff6b81", "role": "Infrastructure"},
]
AGENT_BY_KEY = {a["key"]: a for a in AGENT_REGISTRY}

# Bill is the coordinator and lives at the top-level Hermes home, not under profiles/.
KNOWN_AGENTS = {"bill", "vault", "scholar", "quizmaster", "planner", "dev"}


def _inject_telegram_env(env: dict[str, str]) -> None:
    """Read Telegram credentials from ~/.hermes/.env if missing from current env."""
    env_file = config.ENV_FILE
    if not env_file.is_file():
        return
    needed = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"}
    # Only read file if any needed key is missing
    if all(env.get(k) for k in needed):
        return
    try:
        text = env_file.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in needed and not env.get(key):
            env[key] = val.strip()


# Keys the spawned hermes_cli.main subprocess needs to reach its LLM provider.
# The dashboard runs under systemd with a stripped env, so we explicitly
# hydrate these from the per-profile .env file before exec.
_AGENT_REQUIRED_KEYS = (
    "KIRO_GATEWAY_API_KEY",
    "KIMCHI_API_KEY",
    "KIMCHI_BASE_URL",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def _hydrate_agent_env(env: dict[str, str], profile_dir: Path) -> None:
    """Make sure the spawned agent has everything it needs from its profile's .env.

    Reads ``<profile_dir>/.env`` and overlays any missing required keys into
    ``env``. This is what fixes the "no final response was produced" failure
    where the model had no API key.
    """
    env_file = profile_dir / ".env"
    if not env_file.is_file():
        # Fall back to global .env if profile .env is missing
        env_file = config.ENV_FILE
    if not env_file.is_file():
        return
    if all(env.get(k) for k in _AGENT_REQUIRED_KEYS):
        return
    try:
        text = env_file.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in _AGENT_REQUIRED_KEYS and not env.get(key):
            env[key] = val.strip().strip('"').strip("'")



def _db(path, *, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a dashboard-owned SQLite DB with a busy_timeout set.

    All these DBs live under HERMES_HOME and have a single writer (this web
    server), but FastAPI serves requests concurrently, so two handlers can hit
    the same DB at once. ``busy_timeout`` makes the loser wait-and-retry instead
    of raising "database is locked". ``timeout`` is the connect-level lock wait;
    the PRAGMA is the per-statement wait — set both. Journal mode is left at the
    default rollback journal (ntfs3-safe; never WAL here — see memory.py).
    """
    conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_quiz_db() -> None:
    """Ensure quiz_attempts table exists."""
    conn = _db(QUIZ_DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            filename TEXT NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            percentage REAL NOT NULL,
            time_seconds INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _init_flashcard_db() -> None:
    conn = _db(FLASHCARD_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS flashcard_decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

_init_quiz_db()
_init_flashcard_db()

def _init_tasks_db() -> None:
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            subject TEXT,
            status TEXT NOT NULL DEFAULT 'todo',
            position REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

_init_tasks_db()

def _init_stickies_db() -> None:
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS stickies (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT 'amber',
            position REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

def _init_pomodoro_db() -> None:
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS pomodoro (
            day TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
_init_stickies_db()
_init_pomodoro_db()

# Chat database
def _init_chat_db() -> None:
    conn = _db(CHAT_DB)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS ix_chat_agent ON chat_messages(agent, created_at)")
    conn.commit()
    conn.close()

_init_chat_db()

# Agent Discord channel IDs.
#
# Populated from environment so install-specific channel snowflakes never sit in
# the repo. Format: MC_DISCORD_CHANNELS="vault:1234,scholar:5678,…". Leave unset
# (the default) to skip Discord routing — chat still works in-app.
def _parse_agent_channels(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece or ":" not in piece:
            continue
        k, _, v = piece.partition(":")
        if k.strip() and v.strip().isdigit():
            out[k.strip()] = v.strip()
    return out


AGENT_DISCORD_CHANNELS: dict[str, str] = _parse_agent_channels(
    os.environ.get("MC_DISCORD_CHANNELS", "")
)

# In-memory tracking of which agents have a background turn running.
_chat_running: dict[str, bool] = {}
_chat_running_lock = asyncio.Lock()


async def _set_chat_running(agent: str, running: bool) -> None:
    async with _chat_running_lock:
        _chat_running[agent] = running


def _is_chat_running(agent: str) -> bool:
    return _chat_running.get(agent, False)


# ---------------------------------------------------------------------------
# Mirroring helpers — post as the bot so the gateway ignores the message.
# ---------------------------------------------------------------------------
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _read_discord_token() -> str:
    env_file = config.ENV_FILE
    if not env_file.is_file():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _read_telegram_creds() -> tuple[str, str]:
    env_file = config.ENV_FILE
    if not env_file.is_file():
        return "", ""
    token = ""
    chat = ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("TELEGRAM_HOME_CHANNEL="):
                chat = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return token, chat


async def _mirror_to_discord(channel_id: str, text: str) -> bool:
    token = _read_discord_token()
    if not token or not channel_id:
        return False
    payload = json.dumps({"content": text[:1999]}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": _BROWSER_UA,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception:
        return False


async def _mirror_to_telegram(chat_id: str, text: str) -> bool:
    token, _ = _read_telegram_creds()
    if not token or not chat_id:
        return False
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4095]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _BROWSER_UA},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception:
        return False


async def _mirror_message(agent: str, role: str, text: str) -> None:
    label = "👤" if role == "user" else "🤖"
    mirror_text = f"{label} **{agent.capitalize()}**\n{text}"
    if agent == "bill":
        _, chat_id = _read_telegram_creds()
        if chat_id:
            await _mirror_to_telegram(chat_id, mirror_text)
    else:
        channel_id = AGENT_DISCORD_CHANNELS.get(agent, "")
        if channel_id:
            await _mirror_to_discord(channel_id, mirror_text)


def _get_chat_history(agent: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _db(CHAT_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, agent, role, text, created_at FROM chat_messages WHERE agent = ? ORDER BY id DESC LIMIT ?",
        (agent, limit),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    rows.reverse()
    return rows


def _trim_chat_history(agent: str, keep: int = 200) -> None:
    conn = _db(CHAT_DB)
    c = conn.cursor()
    c.execute(
        "DELETE FROM chat_messages WHERE agent = ? AND id NOT IN (SELECT id FROM chat_messages WHERE agent = ? ORDER BY id DESC LIMIT ?)",
        (agent, agent, keep),
    )
    conn.commit()
    conn.close()


def _store_chat_message(agent: str, role: str, text: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    conn = _db(CHAT_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "INSERT INTO chat_messages (agent, role, text, created_at) VALUES (?, ?, ?, ?) RETURNING id",
        (agent, role, text, now),
    )
    row_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    _trim_chat_history(agent)
    return {"id": row_id, "agent": agent, "role": role, "text": text, "created_at": now}


async def _run_agent_chat_turn(agent: str, user_text: str) -> None:
    """Background task: runs the agent with full conversation context, stores the reply, and mirrors it."""
    await _set_chat_running(agent, True)
    try:
        # Build context from chat history (last 30 messages)
        history = _get_chat_history(agent, limit=30)
        context_lines = []
        for msg in history:
            sender = config.USER_NAME if msg["role"] == "user" else agent.capitalize()
            context_lines.append(f"{sender}: {msg['text']}")

        system_prefix = (
            f"You are {agent.capitalize()}, a specialist agent in {config.USER_NAME}'s AI Student Companion team. "
            f"Respond directly and helpfully to the user's message. Keep responses concise but complete. "
            f"Do not use filler phrases like 'Great question' or 'Certainly'.\n\n"
            f"Conversation context (most recent first):\n"
            + "\n".join(context_lines) + "\n\n"
            + f"Now respond to the latest message as {agent.capitalize()}."
        )

        if agent == "bill":
            hermes_home = HERMES_HOME_BILL
        else:
            hermes_home = PROFILES_DIR / agent
        if not hermes_home.is_dir():
            _store_chat_message(agent, "assistant", f"[error] HERMES_HOME for '{agent}' not found.")
            return

        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
        _inject_telegram_env(env)
        _hydrate_agent_env(env, hermes_home if agent != "bill" else HERMES_HOME_BILL)
        env.setdefault("HERMES_INFERENCE_PROVIDER", "custom:tokenrouter")
        env.setdefault("HERMES_INFERENCE_MODEL", "MiniMax-M3")

        cmd = [
            str(HERMES_PYTHON),
            "-m", "hermes_cli.main",
            "-z", system_prefix,
            "--provider", "custom:tokenrouter",
            "--model", "MiniMax-M3",
            "--yolo", "--accept-hooks",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=600.0)
        except asyncio.TimeoutError:
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
            reply_text = "[timeout] The agent took too long to respond. Try again or simplify your request."
            _store_chat_message(agent, "assistant", reply_text)
            await _mirror_message(agent, "assistant", reply_text)
            return

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Extract the agent's reply from stdout (last substantial paragraph)
        reply_text = stdout.strip()
        if not reply_text:
            reply_text = stderr.strip() or "[error] The agent produced no output."
        if len(reply_text) > 8000:
            reply_text = reply_text[:7997] + "..."

        _store_chat_message(agent, "assistant", reply_text)
        await _mirror_message(agent, "assistant", reply_text)
    except Exception as exc:
        err = f"[error] Agent turn failed: {exc}"
        _store_chat_message(agent, "assistant", err)
        await _mirror_message(agent, "assistant", err)
    finally:
        await _set_chat_running(agent, False)


def _update_research_status(id: int, status: str) -> None:
    """Flip a research row to a new status in research.db."""
    conn = _db(RESEARCH_DB)
    c = conn.cursor()
    c.execute("UPDATE research SET status = ? WHERE id = ?", (status, id))
    conn.commit()
    conn.close()


app = FastAPI(title="Mission Control", version=VERSION)


# ---------------------------------------------------------------------------
# Security: same-origin enforcement (CSRF / cross-site WebSocket defense).
#
# The systemd unit binds this service to the Tailscale interface, so it is
# reachable by every device on the tailnet — and, more importantly, a malicious
# web page open in the user's browser could try to drive state-changing
# requests against it (CSRF) or open the /ws/terminal shell socket cross-site
# (CSWSH → a remote shell). Browsers attach an Origin header to cross-site
# requests and to every WebSocket handshake; a same-origin request from our own
# frontend carries Origin whose host:port equals the Host header. We refuse any
# state-changing HTTP request whose Origin is present and does not match Host.
#
# Requests with no Origin (curl, the headless localhost automation services such
# as hypr-autopomodoro / vault-reindex) are allowed — they are not browser-driven
# CSRF vectors. GET/HEAD/OPTIONS are reads and are left untouched; cross-origin
# *reads* are already unreadable to an attacker because we send no CORS headers.
# The /ws/terminal socket enforces its own same-origin check in terminal.py,
# because HTTP middleware does not see the WebSocket scope.
# ---------------------------------------------------------------------------
from urllib.parse import urlparse as _urlparse

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def _enforce_same_origin(request: Request, call_next):
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        if origin and _urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse(
                {"detail": "cross-origin request refused"},
                status_code=403,
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------
@app.get("/api/agents")
async def list_agents() -> JSONResponse:
    """Return agent list with Bill first."""
    return JSONResponse({"agents": AGENT_REGISTRY})


@app.get("/api/chat/{agent}")
async def get_chat_history_endpoint(agent: str) -> JSONResponse:
    agent = agent.strip().lower()
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"unknown agent '{agent}'")
    history = _get_chat_history(agent, limit=200)
    return JSONResponse({"agent": agent, "history": history, "is_running": _is_chat_running(agent)})


@app.post("/api/chat/{agent}")
async def send_chat_message(agent: str, request: Request) -> JSONResponse:
    agent = agent.strip().lower()
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"unknown agent '{agent}'")
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    # Store user message
    msg = _store_chat_message(agent, "user", text)

    # Mirror user message immediately
    asyncio.create_task(_mirror_message(agent, "user", text))

    # Launch background turn if not already running
    if not _is_chat_running(agent):
        asyncio.create_task(_run_agent_chat_turn(agent, text))

    return JSONResponse({"ok": True, "message": msg, "is_running": True})


@app.post("/api/chat/{agent}/reset")
async def reset_chat(agent: str) -> JSONResponse:
    agent = agent.strip().lower()
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"unknown agent '{agent}'")
    conn = _db(CHAT_DB)
    c = conn.cursor()
    c.execute("DELETE FROM chat_messages WHERE agent = ?", (agent,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted": deleted})


@app.get("/")
async def root_index() -> FileResponse:
    if not INDEX_FILE.is_file():
        raise HTTPException(status_code=404, detail=f"index not found: {INDEX_FILE}")
    return FileResponse(INDEX_FILE)


# ---------------------------------------------------------------------------
# The one helper every feature funnels through.
# ---------------------------------------------------------------------------
async def run_agent_oneshot(
    agent: str,
    task: str,
    *,
    timeout: float | None = 600.0,
) -> dict[str, Any]:
    """Launch a one-shot Hermes process for `agent` and have it execute `task`.

    Sets HERMES_HOME to the agent's profile (or to ~/.hermes for Bill) and
    AGENT_LOG_DB to the shared activity database, then runs:

        <hermes_python> -m hermes_cli.main -z <task> --yolo --accept-hooks

    Returns stdout, stderr, return code, duration, and the resolved env paths.
    """
    agent = agent.strip().lower()
    if agent not in KNOWN_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown agent '{agent}'. expected one of: {sorted(KNOWN_AGENTS)}",
        )
    if not task or not task.strip():
        raise HTTPException(status_code=400, detail="task text is required")

    if agent == "bill":
        hermes_home = HERMES_HOME_BILL
    else:
        hermes_home = PROFILES_DIR / agent
    if not hermes_home.is_dir():
        raise HTTPException(
            status_code=500,
            detail=f"HERMES_HOME for '{agent}' does not exist: {hermes_home}",
        )
    if not HERMES_PYTHON.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"hermes python not found: {HERMES_PYTHON}",
        )

    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)

    cmd = [
        str(HERMES_PYTHON),
        "-m", "hermes_cli.main",
        "-z", task,
        "--yolo",
        "--accept-hooks",
    ]

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        stdout_bytes, stderr_bytes = await proc.communicate()
        timed_out = True

    duration = round(time.monotonic() - started, 3)
    return {
        "agent": agent,
        "task": task,
        "returncode": proc.returncode,
        "duration_s": duration,
        "timed_out": timed_out,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "env": {
            "HERMES_HOME": env["HERMES_HOME"],
            "AGENT_LOG_DB": env["AGENT_LOG_DB"],
        },
        "cmd": cmd,
    }


# ---------------------------------------------------------------------------
# Starter endpoints.
# ---------------------------------------------------------------------------


BUILD_FILE = config.BUILD_FILE


def _current_version() -> str:
    try:
        build = int(BUILD_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        build = 1
    return f"{VERSION}-b{build}"


@app.get("/api/version")
async def version() -> dict[str, str]:
    return {"service": "mission-control", "version": _current_version()}


@app.get("/api/subjects")
async def list_subjects() -> dict[str, Any]:
    """List subject folders under SUBJECTS_DIR. Returns [] if directory is empty."""
    if not SUBJECTS_DIR.is_dir():
        return {"path": str(SUBJECTS_DIR), "subjects": [], "exists": False}
    subjects = sorted(p.name for p in SUBJECTS_DIR.iterdir() if p.is_dir())
    return {"path": str(SUBJECTS_DIR), "subjects": subjects, "exists": True}


@app.post("/api/subjects/upload")
async def subject_upload(
    file: UploadFile = File(..., description="Study document to process"),
    subject: str = Form(..., description="Subject folder name (existing or new)"),
) -> JSONResponse:
    """Save a study document and launch Bill's pipeline in the background.

    Creates the subject folder + subdirectories (uploads, notes, quizzes,
    flashcards, planning) if missing, streams the file to uploads/,
    then launches pipeline.py and returns immediately with a job id.
    """
    subject = subject.strip()
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    safe = re.sub(r"[^A-Za-z0-9_\- ]+", "", subject).strip() or "untitled"
    subject_dir = SUBJECTS_DIR / safe

    # Ensure folder tree
    uploads_dir = subject_dir / "uploads"
    notes_dir = subject_dir / "notes"
    quizzes_dir = subject_dir / "quizzes"
    flashcards_dir = subject_dir / "flashcards"
    planning_dir = subject_dir / "planning"
    for d in (uploads_dir, notes_dir, quizzes_dir, flashcards_dir, planning_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Stream file to disk
    original = file.filename or "upload"
    ext = Path(original).suffix
    safe_name = _safe_basename(original)
    if not safe_name.lower().endswith(ext.lower()):
        safe_name += ext
    # If name collides, suffix with timestamp
    dst = uploads_dir / safe_name
    if dst.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dst = uploads_dir / f"{dst.stem}-{ts}{dst.suffix}"
    await _save_upload_streaming(file, dst)

    # Launch pipeline in background (fire-and-forget)
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME_BILL)
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
    # Telegram creds may not be in the systemd env; read from ~/.hermes/.env if missing.
    _inject_telegram_env(env)
    proc = await asyncio.create_subprocess_exec(
        str(HERMES_PYTHON),
        str(PIPELINE_SCRIPT),
        "--subject", safe,
        "--file", str(dst),
        "--timeout", "600",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    return JSONResponse({
        "ok": True,
        "job_id": f"{safe}_{int(time.time())}",
        "subject": safe,
        "file": {
            "name": safe_name,
            "path": str(dst),
            "size_bytes": dst.stat().st_size,
        },
        "pipeline_pid": proc.pid,
        "message": "Bill is now coordinating the agents. Updates will arrive on Telegram.",
    })


# ---------------------------------------------------------------------------
# Research workspace
# ---------------------------------------------------------------------------
@app.post("/api/research")
async def create_research(payload: dict[str, Any]) -> JSONResponse:
    """Store a research request and trigger Scholar in the background."""
    title = str(payload.get("title", "")).strip()
    query = str(payload.get("query", "")).strip()
    if not title or not query:
        raise HTTPException(status_code=400, detail="title and query are required")

    safe_title = _SAFE_NAME_RE.sub("_", title).strip("._-") or "research"
    filename = f"{safe_title}_{int(time.time())}.md"
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

    conn = _db(RESEARCH_DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO research (title, query, filename, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, query, filename, "researching", datetime.now(timezone.utc).isoformat()),
    )
    research_id = c.lastrowid
    conn.commit()
    conn.close()

    # Trigger Scholar in background (fire-and-forget)
    scholar_profile = PROFILES_DIR / "scholar"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(scholar_profile)
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
    _inject_telegram_env(env)
    _hydrate_agent_env(env, scholar_profile)
    # Since Scholar's profile config defaults to `custom:tokenrouter` / `MiniMax-M3`,
    # the dashboard passes `--provider custom:tokenrouter` on the CLI so the oneshot
    # runner does NOT auto-detect the model name and route it to the wrong
    # provider. The env var is a backup for subprocesses that
    # read the environment.
    env.setdefault("HERMES_INFERENCE_PROVIDER", "custom:tokenrouter")
    env.setdefault("HERMES_INFERENCE_MODEL", "MiniMax-M3")
    provider_flag = "custom:tokenrouter"
    model_flag = "MiniMax-M3"
    task = (
        f"Research the following topic thoroughly and save structured findings as a Markdown file.\n"
        f"Topic: {query}\n"
        f"Save the findings to: {RESEARCH_DIR / filename}\n"
        f"Use clear headings, bullet points, and include sources where possible. "
        f"Organize with an executive summary, key findings, and detailed sections. "
        f"Make it comprehensive but concise."
    )
    # Per-run log so the user (or Dev) can see what the agent actually did.
    log_dir = RESEARCH_DIR / ".logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"research_{research_id}.log"
    log_fp = open(log_path, "ab", buffering=0)

    async def _run_scholar() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(HERMES_PYTHON), "-m", "hermes_cli.main",
                "-z", task,
                "--provider", provider_flag,
                "--model", model_flag,
                "--yolo", "--accept-hooks",
                env=env,
                stdout=log_fp,
                stderr=log_fp,
            )
            rc = await proc.wait()
            log_fp.write(f"\n[research {research_id}] scholar exited rc={rc}\n".encode())
        except Exception as e:  # never let the background task crash silently
            log_fp.write(f"\n[research {research_id}] spawn failed: {e!r}\n".encode())
        finally:
            log_fp.close()

    asyncio.create_task(_run_scholar())

    return JSONResponse({"ok": True, "id": research_id, "status": "researching"})


@app.get("/api/research")
async def list_research() -> JSONResponse:
    """List all research requests. Status flips to 'complete' when the file exists on disk."""
    conn = _db(RESEARCH_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, title, query, filename, status, created_at FROM research ORDER BY created_at DESC"
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()

    for row in rows:
        if row["status"] == "researching":
            filepath = RESEARCH_DIR / row["filename"]
            if filepath.is_file():
                row["status"] = "complete"
                _update_research_status(row["id"], "complete")
        row["ready"] = row["status"] == "complete"

    return JSONResponse({"items": rows})


@app.get("/api/research/{research_id}")
async def get_research(research_id: int) -> JSONResponse:
    """Return a single research item with its Markdown content if complete."""
    conn = _db(RESEARCH_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, title, query, filename, status, created_at FROM research WHERE id = ?",
        (research_id,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="research not found")

    data = dict(row)
    filepath = RESEARCH_DIR / data["filename"]
    data["ready"] = filepath.is_file()
    if data["status"] == "researching" and data["ready"]:
        data["status"] = "complete"
        _update_research_status(research_id, "complete")

    content = ""
    if data["ready"]:
        try:
            content = filepath.read_text(encoding="utf-8")
        except Exception:
            content = ""
    data["content"] = content
    return JSONResponse(data)


@app.post("/api/research/{research_id}/quiz")
async def research_quiz(research_id: int) -> JSONResponse:
    """Hand a finished research file to Quizmaster to build a quiz."""
    conn = _db(RESEARCH_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT id, title, filename, status FROM research WHERE id = ?", (research_id,)
    )
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="research not found")

    data = dict(row)
    filepath = RESEARCH_DIR / data["filename"]
    if not filepath.is_file():
        raise HTTPException(status_code=400, detail="research file not ready yet")

    # Trigger Quizmaster in background
    env = os.environ.copy()
    env["HERMES_HOME"] = str(PROFILES_DIR / "quizmaster")
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
    _inject_telegram_env(env)
    task = (
        f"Generate a quiz from the research document at {filepath}.\n"
        f"Title: {data['title']}\n"
        f"Save the quiz as a Markdown file in {RESEARCH_DIR} with filename "
        f"'{data['filename'].replace('.md', '')}_quiz.md'.\n"
        f"Include 10-15 multiple-choice questions with an answer key."
    )
    asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(HERMES_PYTHON), "-m", "hermes_cli.main",
            "-z", task, "--yolo", "--accept-hooks",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )

    return JSONResponse(
        {"ok": True, "message": "Quizmaster is generating a quiz from this research."}
    )


# ---------------------------------------------------------------------------
# Notes Library — list & read Markdown notes per subject
# ---------------------------------------------------------------------------
_NOTE_EXTS = {".md", ".markdown", ".txt"}


def _safe_subject(name: str) -> Path:
    """Resolve `<SUBJECTS_DIR>/<name>` while blocking traversal."""
    safe = _SAFE_NAME_RE.sub("_", name).strip("._-") or "_"
    target = (SUBJECTS_DIR / safe).resolve()
    if not str(target).startswith(str(SUBJECTS_DIR.resolve())):
        raise HTTPException(status_code=400, detail="invalid subject")
    return target


@app.get("/api/notes/{subject}")
async def list_notes(subject: str) -> JSONResponse:
    """List Markdown notes for a subject, newest first."""
    subj_dir = _safe_subject(subject)
    notes_dir = subj_dir / "notes"
    if not notes_dir.is_dir():
        return JSONResponse({"subject": subject, "items": []})

    items: list[dict[str, Any]] = []
    for p in notes_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _NOTE_EXTS:
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        items.append({
            "id": str(p.relative_to(notes_dir)),
            "name": p.name,
            "relative_path": str(p.relative_to(subj_dir)),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "modified_ts": stat.st_mtime,
        })
    items.sort(key=lambda i: i["modified_ts"], reverse=True)
    for i in items:
        i.pop("modified_ts", None)
    return JSONResponse({"subject": subject, "items": items})


@app.get("/api/notes/{subject}/{path:path}")
async def get_note(subject: str, path: str) -> JSONResponse:
    """Return the Markdown content of one note file."""
    subj_dir = _safe_subject(subject)
    notes_dir = subj_dir / "notes"
    target = (notes_dir / path).resolve()
    if not str(target).startswith(str(notes_dir.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="note not found")
    if target.suffix.lower() not in _NOTE_EXTS:
        raise HTTPException(status_code=415, detail="unsupported note type")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}") from e
    stat = target.stat()
    return JSONResponse({
        "subject": subject,
        "id": path,
        "name": target.name,
        "relative_path": str(target.relative_to(subj_dir)),
        "content": content,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Schedule — merge global planning files + per-subject consolidated planning
# ---------------------------------------------------------------------------
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Map keywords found in section headings → bucket. Anything unrecognised
# (e.g. "Office Hours") is silently dropped per spec.
_SECTION_BUCKETS: list[tuple[str, str]] = [
    ("weekly", "weekly_schedule"),
    ("timetable", "weekly_schedule"),
    ("schedule", "weekly_schedule"),
    ("class", "weekly_schedule"),
    ("deadline", "deadlines"),
    ("assignment", "deadlines"),
    ("due", "deadlines"),
    ("exam", "exams"),
    ("test", "exams"),
    ("quiz", "exams"),
]

# Global planning files the user edits manually one row at a time.
_GLOBAL_FILES = {
    "weekly_schedule": PLANNING_DIR / "weekly_schedule.md",
    "deadlines": PLANNING_DIR / "deadlines.md",
    "exams": PLANNING_DIR / "exams.md",
}


def _parse_md_table(lines: list[str]) -> list[dict[str, str]]:
    """Parse the first GitHub-flavoured Markdown table found in `lines`.

    Returns rows as {header_lower_snake: cell} dicts. Handles pipe-delimited
    tables with the standard header / divider / body shape and tolerates leading
    or trailing pipes plus extra whitespace.
    """
    rows: list[dict[str, str]] = []
    headers: list[str] | None = None
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if "|" in ln and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[i + 1]):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            headers = [re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_") or f"col{idx}"
                       for idx, c in enumerate(cells)]
            i += 2
            while i < len(lines) and "|" in lines[i].strip():
                row_cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if any(row_cells):
                    row = {headers[k] if k < len(headers) else f"col{k}": row_cells[k]
                           for k in range(len(row_cells))}
                    rows.append(row)
                i += 1
            break
        i += 1
    return rows


def _normalise_row(bucket: str, row: dict[str, str]) -> dict[str, str]:
    """Map varied column names to the schema the frontend renders."""
    def pick(*keys: str) -> str:
        for k in keys:
            if k in row and row[k]:
                return row[k]
        return ""
    base = {
        "weekday": pick("weekday", "day"),
        "time": pick("time", "slot", "when"),
        "course": pick("course", "class", "subject"),
        "location": pick("location", "room", "where"),
        "date": pick("date", "due_date", "on"),
        "task": pick("task", "assignment", "title", "what"),
        "exam": pick("exam", "test", "title"),
        "status": pick("status", "state"),
        "notes": pick("notes", "note", "details"),
    }
    return {k: v for k, v in base.items() if v}


def _classify_section(heading: str) -> str | None:
    h = heading.lower()
    for keyword, bucket in _SECTION_BUCKETS:
        if keyword in h:
            return bucket
    return None


def _split_consolidated(text: str) -> dict[str, list[dict[str, str]]]:
    """Split a consolidated planning file into buckets by section heading."""
    out: dict[str, list[dict[str, str]]] = {
        "weekly_schedule": [], "deadlines": [], "exams": []
    }
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for raw in text.splitlines():
        m = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", raw)
        if m:
            if current_heading or current_lines:
                sections.append((current_heading, current_lines))
            current_heading = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(raw)
    if current_heading or current_lines:
        sections.append((current_heading, current_lines))

    for heading, lns in sections:
        bucket = _classify_section(heading)
        if not bucket:
            continue
        for row in _parse_md_table(lns):
            norm = _normalise_row(bucket, row)
            if norm:
                out[bucket].append(norm)
    return out


def _read_global_bucket(bucket: str) -> list[dict[str, str]]:
    p = _GLOBAL_FILES[bucket]
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    rows: list[dict[str, str]] = []
    for row in _parse_md_table(text.splitlines()):
        norm = _normalise_row(bucket, row)
        if norm:
            rows.append(norm)
    return rows


def _dedup(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple] = set()
    out: list[dict[str, str]] = []
    for r in rows:
        key = tuple(sorted((k, v) for k, v in r.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


@app.get("/api/schedule")
async def get_schedule() -> JSONResponse:
    """Merge global planning files + per-subject consolidated files."""
    buckets: dict[str, list[dict[str, str]]] = {
        "weekly_schedule": [], "deadlines": [], "exams": []
    }

    # 1) Global manually-edited files
    for b in buckets:
        buckets[b].extend(_read_global_bucket(b))

    # 2) Per-subject consolidated planning files from the upload pipeline
    if SUBJECTS_DIR.is_dir():
        for subj in SUBJECTS_DIR.iterdir():
            planning = subj / "planning"
            if not planning.is_dir():
                continue
            for f in planning.iterdir():
                if not f.is_file() or f.suffix.lower() not in _NOTE_EXTS:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                split = _split_consolidated(text)
                for b in buckets:
                    for row in split[b]:
                        if "course" not in row:
                            row["course"] = subj.name
                        buckets[b].extend([row])

    for b in buckets:
        buckets[b] = _dedup(buckets[b])

    today_weekday = _WEEKDAYS[datetime.now().weekday()]
    todays_classes = [
        r for r in buckets["weekly_schedule"]
        if r.get("weekday", "").strip().lower().startswith(today_weekday.lower()[:3])
    ]

    return JSONResponse({
        "today_weekday": today_weekday,
        "todays_classes": todays_classes,
        "weekly_schedule": buckets["weekly_schedule"],
        "deadlines": buckets["deadlines"],
        "exams": buckets["exams"],
        "sources": {
            "global_dir": str(PLANNING_DIR),
            "subjects_dir": str(SUBJECTS_DIR),
        },
    })


# ---------------------------------------------------------------------------
# Lecture Notes — list, serve, and overwrite uploaded PDFs
# ---------------------------------------------------------------------------
# A PDF id is "<subject>::<relative_path>" so the api is independent of
# filesystem layout but still resolves to a real, traversal-safe path.

def _parse_pdf_id(pdf_id: str) -> tuple[str, str]:
    if "::" not in pdf_id:
        raise HTTPException(status_code=400, detail="pdf id must be <subject>::<path>")
    subject, _, rel = pdf_id.partition("::")
    return subject, rel


def _resolve_pdf_path(subject: str, rel_path: str) -> Path:
    subj_dir = _safe_subject(subject)
    target = (subj_dir / rel_path).resolve()
    if not str(target).startswith(str(subj_dir.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="pdf not found")
    if target.suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="not a pdf")
    return target


def _walk_pdfs() -> list[dict[str, Any]]:
    """Recursively list every PDF under SUBJECTS_DIR."""
    if not SUBJECTS_DIR.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for subj in sorted(SUBJECTS_DIR.iterdir()):
        if not subj.is_dir():
            continue
        for p in subj.rglob("*.pdf"):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            rel = p.relative_to(subj).as_posix()
            items.append({
                "id": f"{subj.name}::{rel}",
                "name": p.name,
                "subject": subj.name,
                "relative_path": rel,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "modified_ts": stat.st_mtime,
            })
    items.sort(key=lambda i: i["modified_ts"], reverse=True)
    for i in items:
        i.pop("modified_ts", None)
    return items


@app.get("/api/lectures")
async def list_lectures() -> JSONResponse:
    """List every uploaded PDF across all subjects, newest first."""
    return JSONResponse({"items": _walk_pdfs()})


@app.get("/api/lectures/{pdf_id:path}")
async def get_lecture(pdf_id: str) -> FileResponse:
    """Serve a PDF inline so PDF.js can render it."""
    subject, rel = _parse_pdf_id(pdf_id)
    target = _resolve_pdf_path(subject, rel)
    return FileResponse(
        target,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{target.name}"'},
    )


@app.put("/api/lectures/{pdf_id:path}")
async def put_lecture(pdf_id: str, request: Request) -> JSONResponse:
    """Overwrite a PDF with the request body bytes.

    Body must be raw application/pdf. Saves are atomic (write to .tmp, fsync,
    replace) so the dashboard never exposes a half-written file.
    """
    subject, rel = _parse_pdf_id(pdf_id)
    target = _resolve_pdf_path(subject, rel)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    # Reject obviously-wrong content: must start with %PDF-
    if not body.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="body is not a PDF")
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"save failed: {e}") from e
    stat = target.stat()
    return JSONResponse({
        "ok": True,
        "id": pdf_id,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Overview aggregation
# ---------------------------------------------------------------------------
def _parse_iso(ts: str) -> datetime | None:
    """Best-effort ISO-8601 parser for SQLite text timestamps."""
    if not ts:
        return None
    s = ts.strip()
    # SQLite often stores 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DDTHH:MM:SS[.ffffff][+HH:MM]'
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Strip stray fractional precision or tz oddities
        try:
            dt = datetime.fromisoformat(s.split("+")[0].split(".")[0])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _count_subject_assets() -> dict[str, int]:
    """Walk SUBJECTS_DIR and count subjects, note files, and quiz files."""
    out = {"subjects": 0, "note_files": 0, "quizzes": 0}
    if not SUBJECTS_DIR.is_dir():
        return out

    subjects = [p for p in SUBJECTS_DIR.iterdir() if p.is_dir()]
    out["subjects"] = len(subjects)

    note_count = 0
    quiz_count = 0
    for subj in subjects:
        for path in subj.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            ext = path.suffix.lower()
            if ext in {".md", ".markdown", ".txt"} and "quiz" not in name:
                note_count += 1
            if ("quiz" in name) and ext in {".md", ".markdown", ".json", ".yaml", ".yml"}:
                quiz_count += 1
    out["note_files"] = note_count
    out["quizzes"] = quiz_count
    return out


def _query_logs() -> list[tuple[str, str, str, str, str]]:
    """Read agent_logs as tuples (agent_name, task_description, model_used, status, created_at)."""
    if not AGENT_LOG_DB.is_file():
        return []
    con = sqlite3.connect(f"file:{AGENT_LOG_DB}?mode=ro", uri=True)
    try:
        con.row_factory = None
        rows = con.execute(
            "select agent_name, task_description, model_used, status, created_at "
            "from agent_logs order by created_at desc"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return rows


@app.get("/api/overview")
async def overview() -> dict[str, Any]:
    """Aggregate dashboard overview: totals, agent breakdown, daily activity (30d),
    per-agent hourly heatmap (7d), recent activity feed, calendar dots."""
    now = datetime.now(timezone.utc)
    today_local = datetime.now().date()
    rows = _query_logs()
    asset_counts = _count_subject_assets()

    # ---------- agent breakdown ----------
    by_agent: Counter[str] = Counter()
    for agent_name, _task, _model, _status, _ts in rows:
        key = (agent_name or "").strip().lower()
        if key in AGENT_BY_KEY:
            by_agent[key] += 1
    total_tasks = sum(by_agent.values())

    breakdown: list[dict[str, Any]] = []
    for a in AGENT_REGISTRY:
        c = by_agent.get(a["key"], 0)
        pct = round((c / total_tasks) * 100.0, 1) if total_tasks else 0.0
        breakdown.append({**a, "count": c, "percentage": pct})

    # ---------- daily activity (last 30 days, local-date keys) ----------
    daily_counts: dict[str, int] = {}
    for i in range(29, -1, -1):
        d = (today_local - timedelta(days=i)).isoformat()
        daily_counts[d] = 0
    for _agent, _task, _model, _status, ts in rows:
        dt = _parse_iso(ts)
        if not dt:
            continue
        local_d = dt.astimezone().date().isoformat()
        if local_d in daily_counts:
            daily_counts[local_d] += 1
    daily_activity = [{"date": d, "count": c} for d, c in daily_counts.items()]

    # ---------- 7d heatmap: per-agent x hour-of-day ----------
    seven_days_ago = now - timedelta(days=7)
    hour_counts: dict[str, list[int]] = {a["key"]: [0] * 24 for a in AGENT_REGISTRY}
    for agent_name, _task, _model, _status, ts in rows:
        key = (agent_name or "").strip().lower()
        if key not in hour_counts:
            continue
        dt = _parse_iso(ts)
        if not dt or dt < seven_days_ago:
            continue
        local_hour = dt.astimezone().hour
        hour_counts[key][local_hour] += 1

    heatmap_max = max((max(v) for v in hour_counts.values()), default=0)
    heatmap = {
        "max_count": heatmap_max,
        "agents": [
            {
                **a,
                "hours": [
                    {"hour": h, "count": hour_counts[a["key"]][h]}
                    for h in range(24)
                ],
            }
            for a in AGENT_REGISTRY
        ],
    }

    # ---------- recent activity feed (top 20) ----------
    recent = []
    for agent_name, task, model, status, ts in rows[:20]:
        key = (agent_name or "").strip().lower()
        meta = AGENT_BY_KEY.get(key)
        recent.append({
            "agent": key,
            "agent_name": meta["name"] if meta else (agent_name or "unknown"),
            "icon": meta["icon"] if meta else "•",
            "color": meta["color"] if meta else "#8891aa",
            "task": task or "",
            "status": status or "",
            "model": model or "",
            "created_at": ts,
        })

    # ---------- calendar (current local month) with per-day colour dots ----------
    first_day = today_local.replace(day=1)
    last_day = (first_day.replace(year=first_day.year + (first_day.month // 12),
                                  month=(first_day.month % 12) + 1) - timedelta(days=1))
    days_in_month = last_day.day
    first_weekday = first_day.weekday()  # Monday=0
    cal_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for agent_name, _task, _model, _status, ts in rows:
        dt = _parse_iso(ts)
        if not dt:
            continue
        ld = dt.astimezone().date()
        if ld.year == first_day.year and ld.month == first_day.month:
            key = (agent_name or "").strip().lower()
            if key in AGENT_BY_KEY:
                cal_counts[ld.isoformat()][key] += 1
    calendar_days = []
    for day in range(1, days_in_month + 1):
        d = first_day.replace(day=day)
        iso = d.isoformat()
        ck = cal_counts.get(iso, Counter())
        # top 3 colours by count
        top = [AGENT_BY_KEY[k]["color"] for k, _ in ck.most_common(3)]
        calendar_days.append({
            "date": iso,
            "day": day,
            "count": sum(ck.values()),
            "colors": top,
            "today": (d == today_local),
        })

    # ---------- streak: consecutive days back from today with >=1 log ----------
    streak = 0
    for entry in reversed(daily_activity):
        if entry["count"] > 0:
            streak += 1
        else:
            if streak > 0:
                break
    # if today has 0 yet, fall back to longest tail ending today-1
    if streak == 0 and daily_activity and daily_activity[-1]["count"] == 0:
        for entry in reversed(daily_activity[:-1]):
            if entry["count"] > 0:
                streak += 1
            else:
                if streak > 0:
                    break

    # ---------- hero ----------
    hour = datetime.now().hour
    if hour < 5:
        greet = "Working late"
    elif hour < 12:
        greet = "Good morning"
    elif hour < 17:
        greet = "Good afternoon"
    elif hour < 21:
        greet = "Good evening"
    else:
        greet = "Good night"

    # tasks today (local date) for hero KPI
    tasks_today = next((d["count"] for d in daily_activity if d["date"] == today_local.isoformat()), 0)

    return {
        "generated_at": now.isoformat(),
        "today": today_local.isoformat(),
        "totals": {
            "subjects": asset_counts["subjects"],
            "note_files": asset_counts["note_files"],
            "quizzes": asset_counts["quizzes"],
            "agent_tasks": total_tasks,
        },
        "hero": {
            "greeting": greet,
            "name": config.USER_NAME,
            "date": datetime.now().strftime("%A, %B %-d, %Y"),
            "tasks_today": tasks_today,
            "subjects": asset_counts["subjects"],
            "day_streak": streak,
        },
        "agent_breakdown": breakdown,
        "recent_activity": recent,
        "daily_activity": daily_activity,
        "heatmap": heatmap,
        "calendar": {
            "month": first_day.strftime("%B %Y"),
            "first_weekday": first_weekday,
            "days_in_month": days_in_month,
            "days": calendar_days,
        },
        "agent_registry": AGENT_REGISTRY,
    }


# ---------------------------------------------------------------------------
# Agents analytics
# ---------------------------------------------------------------------------
@app.get("/api/agents/analytics")
async def agents_analytics() -> dict[str, Any]:
    """Per-agent analytics + summary, all derived from agent_logs.

    For each of the six agents: totals (tasks, completed, failed),
    success_percentage, tasks_today, tasks_this_week, last_active_time,
    current_task, last_model_used, 7-day sparkline, and 8 recent rows.
    Plus an overall summary, task_statistics, distribution, model_usage,
    and a merged recent_activity feed.
    """
    now = datetime.now(timezone.utc)
    today_local = datetime.now().date()
    week_ago = now - timedelta(days=7)

    rows = _query_logs()

    # bucket rows by agent (preserving DESC time order from the query)
    by_agent: dict[str, list[tuple[str, str, str, str, str]]] = {a["key"]: [] for a in AGENT_REGISTRY}
    merged: list[dict[str, Any]] = []
    for agent_name, task, model, status, ts in rows:
        key = (agent_name or "").strip().lower()
        if key not in by_agent:
            continue
        by_agent[key].append((agent_name, task, model, status, ts))
        meta = AGENT_BY_KEY[key]
        merged.append({
            "id": "",
            "agent": key,
            "agent_name": meta["name"],
            "role": meta["role"],
            "emoji": meta["emoji"],
            "icon": meta["icon"],
            "color": meta["color"],
            "task": task or "",
            "status": status or "",
            "model": model or "",
            "created_at": ts,
        })

    agents_payload: list[dict[str, Any]] = []
    distribution: list[dict[str, Any]] = []
    model_usage: list[dict[str, Any]] = []
    summary_online = summary_standby = summary_issues = 0

    for meta in AGENT_REGISTRY:
        agent_rows = by_agent[meta["key"]]
        completed = sum(1 for r in agent_rows if (r[3] or "").lower() == "completed")
        failed    = sum(1 for r in agent_rows if (r[3] or "").lower() == "failed")
        total     = len(agent_rows)
        success_pct = round((completed / total) * 100.0, 1) if total else 0.0

        tasks_today = 0
        tasks_this_week = 0
        last_dt: datetime | None = None
        for _name, _task, _model, _status, ts in agent_rows:
            dt = _parse_iso(ts)
            if not dt:
                continue
            if dt > week_ago:
                tasks_this_week += 1
            if dt.astimezone().date() == today_local:
                tasks_today += 1
            if last_dt is None or dt > last_dt:
                last_dt = dt

        # 7-day sparkline (oldest -> newest)
        spark = []
        for i in range(6, -1, -1):
            d = (today_local - timedelta(days=i))
            c = sum(1 for r in agent_rows
                    if (_parse_iso(r[4]) or now).astimezone().date() == d)
            spark.append({"date": d.isoformat(), "count": c})

        # status: live (active in last 24h), standby (active in last 7d), idle (older / never)
        if failed > 0 and total > 0 and (failed / total) >= 0.25:
            status_label = "issue"
        elif last_dt and (now - last_dt) < timedelta(hours=24):
            status_label = "live"
        elif last_dt and (now - last_dt) < timedelta(days=7):
            status_label = "standby"
        else:
            status_label = "idle"

        if status_label == "live":      summary_online  += 1
        elif status_label == "standby": summary_standby += 1
        elif status_label == "issue":   summary_issues  += 1

        # current task: most recent description; if status_label says standby/idle, surface that
        current_task = (agent_rows[0][1] if agent_rows else "")
        if status_label == "standby":
            current_task = "Standing by"
        elif status_label == "idle":
            current_task = "Idle"

        last_model = next((r[2] for r in agent_rows if r[2]), "")

        recent_rows = []
        for r in agent_rows[:8]:
            recent_rows.append({
                "id": "",
                "agent": meta["key"],
                "agent_name": meta["name"],
                "role": meta["role"],
                "emoji": meta["emoji"],
                "icon": meta["icon"],
                "color": meta["color"],
                "task": r[1] or "",
                "status": r[3] or "",
                "model": r[2] or "",
                "created_at": r[4],
            })

        agents_payload.append({
            **meta,
            "status": status_label,
            "totals": {"tasks": total, "completed": completed, "failed": failed},
            "success_percentage": success_pct,
            "tasks_today": tasks_today,
            "tasks_this_week": tasks_this_week,
            "last_active_time": last_dt.isoformat() if last_dt else None,
            "current_task": current_task,
            "last_model_used": last_model,
            "sparkline": spark,
            "recent_rows": recent_rows,
        })
        distribution.append({
            "agent": meta["key"], "name": meta["name"], "color": meta["color"],
            "count": total,
        })
        model_usage.append({
            "agent": meta["key"], "name": meta["name"], "color": meta["color"],
            "model": last_model, "tasks": total, "success_percentage": success_pct,
        })

    total_tasks = sum(d["count"] for d in distribution)
    completed_total = sum(a["totals"]["completed"] for a in agents_payload)
    failed_total    = sum(a["totals"]["failed"] for a in agents_payload)
    success_rate    = round((completed_total / total_tasks) * 100.0, 1) if total_tasks else 0.0

    # most active this week
    most_active_payload = {"agent": "", "name": "None", "count": 0, "color": "#8891aa"}
    for a in agents_payload:
        if a["tasks_this_week"] > most_active_payload["count"]:
            most_active_payload = {
                "agent": a["key"], "name": a["name"],
                "count": a["tasks_this_week"], "color": a["color"],
            }

    return {
        "generated_at": now.isoformat(),
        "agents": agents_payload,
        "summary": {
            "online":      summary_online,
            "standby":     summary_standby,
            "issues":      summary_issues,
            "total_tasks": total_tasks,
            "agent_count": len(AGENT_REGISTRY),
        },
        "task_statistics": {
            "tasks_today":     sum(a["tasks_today"] for a in agents_payload),
            "tasks_this_week": sum(a["tasks_this_week"] for a in agents_payload),
            "most_active":     most_active_payload,
            "success_rate":    success_rate,
            "completed":       completed_total,
            "failed":          failed_total,
            "distribution":    distribution,
        },
        "recent_activity": merged[:30],
        "model_usage":     model_usage,
        "agent_registry":  AGENT_REGISTRY,
    }


# ---------------------------------------------------------------------------
@app.post("/api/agents/run")
async def run_agent(payload: dict[str, Any]) -> JSONResponse:
    """Trigger a one-shot agent run. Body: {"agent": "...", "task": "...", "timeout": 600}."""
    agent = str(payload.get("agent", "")).strip().lower()
    task = str(payload.get("task", "")).strip()
    timeout_raw = payload.get("timeout", 600)
    try:
        timeout = float(timeout_raw) if timeout_raw is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be a number")
    result = await run_agent_oneshot(agent, task, timeout=timeout)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Design reference — canonical visual source of truth.
# ---------------------------------------------------------------------------
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_basename(name: str) -> str:
    """Strip any directory component and reduce to [A-Za-z0-9._-]."""
    base = Path(name).name  # drops any path component, blocks traversal
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._-") or "file"
    # cap length to avoid filesystem oddities
    return cleaned[:160]


def _file_info(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


async def _save_upload_streaming(src: UploadFile, dst: Path) -> int:
    """Stream an UploadFile to disk in chunks, enforcing MAX_UPLOAD_BYTES. Returns bytes written."""
    written = 0
    async with aiofiles.open(dst, "wb") as out:
        while True:
            chunk = await src.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                await out.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"file '{src.filename}' exceeds {MAX_UPLOAD_BYTES} bytes",
                )
            await out.write(chunk)
    return written


@app.get("/api/design-reference")
async def design_reference_state() -> dict[str, Any]:
    """Report what is currently stored in the design-reference folder."""
    template = _file_info(DESIGN_REF_TEMPLATE) if DESIGN_REF_TEMPLATE.is_file() else None
    screenshots = [
        _file_info(p) for p in sorted(DESIGN_REF_SCREENSHOTS.iterdir())
        if p.is_file()
    ] if DESIGN_REF_SCREENSHOTS.is_dir() else []
    archive = [
        _file_info(p) for p in sorted(DESIGN_REF_ARCHIVE.iterdir(), reverse=True)
        if p.is_file()
    ] if DESIGN_REF_ARCHIVE.is_dir() else []
    return {
        "root": str(DESIGN_REF_DIR),
        "template": template,
        "screenshots": screenshots,
        "archive": archive,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "accepted": {
            "template": sorted(HTML_EXTS),
            "screenshots": sorted(IMAGE_EXTS),
        },
    }


@app.post("/api/design-reference/upload")
async def design_reference_upload(
    files: list[UploadFile] = File(..., description="HTML template and/or reference screenshots"),
) -> JSONResponse:
    """Accept one or more files. .html/.htm replace the canonical template
    (previous version is archived). Images go into screenshots/."""
    if not files:
        raise HTTPException(status_code=400, detail="no files provided")

    saved_template: dict[str, Any] | None = None
    saved_screenshots: list[dict[str, Any]] = []
    archived: dict[str, Any] | None = None
    rejected: list[dict[str, str]] = []

    for upload in files:
        original = upload.filename or ""
        ext = Path(original).suffix.lower()
        safe = _safe_basename(original)

        if ext in HTML_EXTS:
            # archive previous template if present, then write new one
            if DESIGN_REF_TEMPLATE.is_file():
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                archive_path = DESIGN_REF_ARCHIVE / f"template-{ts}.html"
                DESIGN_REF_TEMPLATE.replace(archive_path)
                archived = _file_info(archive_path)
            await _save_upload_streaming(upload, DESIGN_REF_TEMPLATE)
            saved_template = {**_file_info(DESIGN_REF_TEMPLATE), "uploaded_as": safe}

        elif ext in IMAGE_EXTS:
            dst = DESIGN_REF_SCREENSHOTS / safe
            # if name collides, suffix with timestamp instead of clobbering
            if dst.exists():
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                dst = DESIGN_REF_SCREENSHOTS / f"{dst.stem}-{ts}{dst.suffix}"
            await _save_upload_streaming(upload, dst)
            saved_screenshots.append(_file_info(dst))

        else:
            rejected.append({"name": original, "reason": f"extension '{ext}' not accepted"})

    if not saved_template and not saved_screenshots:
        raise HTTPException(
            status_code=400,
            detail={"message": "no accepted files in upload", "rejected": rejected},
        )

    return JSONResponse({
        "template": saved_template,
        "archived_previous": archived,
        "screenshots": saved_screenshots,
        "rejected": rejected,
    })


@app.get("/api/design-reference/template")
async def design_reference_template_view() -> FileResponse:
    if not DESIGN_REF_TEMPLATE.is_file():
        raise HTTPException(status_code=404, detail="no template uploaded yet")
    return FileResponse(DESIGN_REF_TEMPLATE, media_type="text/html")


@app.get("/api/design-reference/template/download")
async def design_reference_template_download() -> FileResponse:
    if not DESIGN_REF_TEMPLATE.is_file():
        raise HTTPException(status_code=404, detail="no template uploaded yet")
    return FileResponse(
        DESIGN_REF_TEMPLATE,
        media_type="application/octet-stream",
        filename="template.html",
    )


@app.get("/api/design-reference/screenshots/{name}")
async def design_reference_screenshot(name: str, download: bool = False) -> FileResponse:
    safe = _safe_basename(name)
    target = DESIGN_REF_SCREENSHOTS / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"screenshot not found: {safe}")
    if download:
        return FileResponse(target, media_type="application/octet-stream", filename=safe)
    return FileResponse(target)


@app.delete("/api/design-reference/screenshots/{name}")
async def design_reference_screenshot_delete(name: str) -> dict[str, Any]:
    safe = _safe_basename(name)
    target = DESIGN_REF_SCREENSHOTS / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"screenshot not found: {safe}")
    target.unlink()
    return {"deleted": safe}


@app.get("/design-reference")
async def design_reference_page() -> FileResponse:
    if not DESIGN_REF_PAGE.is_file():
        raise HTTPException(status_code=404, detail=f"page not found: {DESIGN_REF_PAGE}")
    return FileResponse(DESIGN_REF_PAGE)



# ---------------------------------------------------------------------------
# Quiz workspace
# ---------------------------------------------------------------------------

@app.post("/api/subjects/{subject}/quiz/generate")
async def generate_subject_quiz(subject: str) -> JSONResponse:
    """Trigger Quizmaster to generate a quiz from a subject's notes."""
    subject_dir = _safe_subject(subject)
    notes_dir = subject_dir / "notes"
    quizzes_dir = subject_dir / "quizzes"
    quizzes_dir.mkdir(parents=True, exist_ok=True)

    # Collect all note text
    notes_text = ""
    if notes_dir.is_dir():
        for p in sorted(notes_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in _NOTE_EXTS:
                try:
                    notes_text += f"\n--- {p.relative_to(notes_dir)} ---\n"
                    notes_text += p.read_text(encoding="utf-8", errors="replace") + "\n"
                except OSError:
                    continue

    if not notes_text.strip():
        raise HTTPException(status_code=400, detail="No notes found for this subject.")

    # Build the prompt for Quizmaster
    quiz_filename = f"{subject}_quiz_{int(time.time())}.md"
    quiz_path = quizzes_dir / quiz_filename
    task = (
        f"Generate a quiz based on the following study notes.\n"
        f"Subject: {subject}\n"
        f"Save the quiz as a Markdown file at: {quiz_path}\n\n"
        f"Instructions:\n"
        f"- Include 8-15 questions.\n"
        f"- Each question must be one of: multiple_choice or true_false.\n"
        f"- For multiple choice, provide exactly 4 options labeled A, B, C, D.\n"
        f"- For true/false, the options are True and False.\n"
        f"- Mark the correct answer clearly under each question.\n"
        f"- Use the following format for each question block:\n"
        f"  ## Question N\n"
        f"  **Type:** multiple_choice | true_false\n"
        f"  <question text>\n"
        f"  A. <option>\n  B. <option>\n  C. <option>\n  D. <option>\n"
        f"  **Correct:** <A/B/C/D or True/False>\n"
        f"  **Explanation:** <brief explanation>\n\n"
        f"Here are the notes:\n"
        f"{notes_text[:8000]}"
    )

    env = os.environ.copy()
    env["HERMES_HOME"] = str(PROFILES_DIR / "quizmaster")
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
    _inject_telegram_env(env)
    _hydrate_agent_env(env, PROFILES_DIR / "quizmaster")
    env.setdefault("HERMES_INFERENCE_PROVIDER", "custom:tokenrouter")
    env.setdefault("HERMES_INFERENCE_MODEL", "MiniMax-M3")

    if not HERMES_PYTHON.is_file():
        raise HTTPException(status_code=503, detail=f"hermes python not found: {HERMES_PYTHON}")

    _gen = asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(HERMES_PYTHON), "-m", "hermes_cli.main",
            "-z", task,
            "--provider", "custom:tokenrouter",
            "--model", "MiniMax-M3",
            "--yolo", "--accept-hooks",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    _gen.add_done_callback(lambda f: f.exception() and print(f"[quiz-gen] spawn failed: {f.exception()}"))

    return JSONResponse({
        "ok": True,
        "message": f"Quizmaster is generating a quiz for {subject}. It will appear in the quiz list shortly.",
        "filename": quiz_filename,
    })


@app.get("/api/subjects/{subject}/quiz")
async def list_subject_quizzes(subject: str) -> JSONResponse:
    """List quiz files for a subject, newest first."""
    subject_dir = _safe_subject(subject)
    quizzes_dir = subject_dir / "quizzes"
    items: list[dict[str, Any]] = []
    if quizzes_dir.is_dir():
        for p in sorted(quizzes_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            items.append({
                "filename": p.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return JSONResponse({"subject": subject, "items": items})


def _parse_quiz_file(text: str) -> list[dict[str, Any]]:
    """Parse a quiz markdown file into structured questions."""
    questions: list[dict[str, Any]] = []
    blocks = re.split(r"(?=^##\s+Question\s+\d+)", text, flags=re.MULTILINE)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        q_type = None
        if re.search(r"\*\*Type:\*\*\s*multiple_choice", block, re.I):
            q_type = "multiple_choice"
        elif re.search(r"\*\*Type:\*\*\s*true_false", block, re.I):
            q_type = "true_false"

        # Extract question text (everything after headers/options until options start)
        lines = block.splitlines()
        q_text_lines: list[str] = []
        options: dict[str, str] = {}
        correct = ""
        explanation = ""
        in_question = True

        for i, ln in enumerate(lines):
            # Skip header line
            if re.match(r"^##\s+Question\s+\d+", ln):
                in_question = True
                continue
            # Type line
            if re.match(r"\*\*Type:\*\*", ln):
                continue
            # Explanation
            m = re.match(r"\*\*Explanation:\*\*\s*(.*)", ln)
            if m:
                explanation = m.group(1).strip()
                continue
            # Correct answer
            m = re.match(r"\*\*Correct:\*\*\s*(.+)", ln)
            if m:
                correct = m.group(1).strip()
                in_question = False
                continue
            # Options
            opt_m = re.match(r"^([A-D])\.\s+(.+)", ln)
            if opt_m:
                options[opt_m.group(1)] = opt_m.group(2).strip()
                in_question = False
                continue
            if re.match(r"^(True|False)\.\s*", ln, re.I):
                # true/false option line
                val = ln.split(".")[0].strip()
                options[val.title()] = val.title()
                in_question = False
                continue
            if in_question:
                q_text_lines.append(ln)

        q_text = "\n".join(q_text_lines).strip()
        # Remove markdown bold from question text
        q_text = re.sub(r"\*\*", "", q_text).strip()

        if q_text and q_type:
            # Normalize correct for true_false
            if q_type == "true_false":
                correct = correct.title()
                if not options:
                    options = {"True": "True", "False": "False"}
            questions.append({
                "question": q_text,
                "type": q_type,
                "options": options,
                "correct": correct,
                "explanation": explanation,
            })
    return questions


@app.get("/api/subjects/{subject}/quiz/{filename}")
async def get_subject_quiz(subject: str, filename: str) -> JSONResponse:
    """Parse a quiz file into structured questions."""
    subject_dir = _safe_subject(subject)
    quizzes_dir = subject_dir / "quizzes"
    target = (quizzes_dir / filename).resolve()
    if not str(target).startswith(str(quizzes_dir.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="quiz not found")
    text = target.read_text(encoding="utf-8", errors="replace")
    questions = _parse_quiz_file(text)
    return JSONResponse({
        "subject": subject,
        "filename": filename,
        "questions": questions,
    })


@app.post("/api/quiz/attempt")
async def save_quiz_attempt(payload: dict[str, Any]) -> JSONResponse:
    """Save a quiz attempt."""
    subject = str(payload.get("subject", "")).strip()
    filename = str(payload.get("filename", "")).strip()
    score = int(payload.get("score", 0))
    total = int(payload.get("total", 0))
    time_seconds = int(payload.get("time_seconds", 0))
    if not subject or not filename or total <= 0:
        raise HTTPException(status_code=400, detail="subject, filename, and total required")
    percentage = round((score / total) * 100.0, 1) if total else 0.0
    conn = _db(QUIZ_DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO quiz_attempts (subject, filename, score, total, percentage, time_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (subject, filename, score, total, percentage, time_seconds, datetime.now(timezone.utc).isoformat()),
    )
    attempt_id = c.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({
        "ok": True,
        "id": attempt_id,
        "subject": subject,
        "filename": filename,
        "score": score,
        "total": total,
        "percentage": percentage,
        "time_seconds": time_seconds,
    })


@app.get("/api/quiz/attempts")
async def list_quiz_attempts(subject: str | None = None, limit: int = 50) -> JSONResponse:
    """Return recent quiz attempts plus per-subject averages."""
    conn = _db(QUIZ_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if subject:
        c.execute(
            "SELECT * FROM quiz_attempts WHERE subject = ? ORDER BY created_at DESC LIMIT ?",
            (subject, limit),
        )
    else:
        c.execute(
            "SELECT * FROM quiz_attempts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = [dict(r) for r in c.fetchall()]
    # per-subject averages
    c.execute("""
        SELECT subject, COUNT(*) as attempts, AVG(percentage) as avg_pct, MAX(created_at) as last_attempt
        FROM quiz_attempts GROUP BY subject ORDER BY last_attempt DESC
    """)
    averages = [dict(r) for r in c.fetchall()]
    conn.close()
    return JSONResponse({"items": rows, "averages": averages})



@app.post("/api/subjects/{subject}/flashcard/generate")
async def generate_subject_flashcards(subject: str) -> JSONResponse:
    """Trigger Quizmaster to generate a flashcard deck from a subject's notes."""
    subject_dir = _safe_subject(subject)
    notes_dir = subject_dir / "notes"
    flashcards_dir = subject_dir / "flashcards"
    flashcards_dir.mkdir(parents=True, exist_ok=True)

    notes_text = ""
    if notes_dir.is_dir():
        for p in sorted(notes_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in _NOTE_EXTS:
                try:
                    notes_text += f"\n--- {p.relative_to(notes_dir)} ---\n"
                    notes_text += p.read_text(encoding="utf-8", errors="replace") + "\n"
                except OSError:
                    continue

    if not notes_text.strip():
        raise HTTPException(status_code=400, detail="No notes found for this subject.")

    deck_filename = f"{subject}_deck_{int(time.time())}.md"
    deck_path = flashcards_dir / deck_filename
    task = (
        f"Generate a flashcard deck based on the following study notes.\n"
        f"Subject: {subject}\n"
        f"Save the deck as a Markdown file at: {deck_path}\n\n"
        f"Instructions:\n"
        f"- Each card is a single plain snippet — a key fact, definition, formula, or concept distilled into one or two plain sentences.\n"
        f"- Include at least 15 snippets.\n"
        f"- Write no headings, no numbering, no question-and-answer markers.\n"
        f"- Separate every card from the next with a line containing only three dashes: ---\n"
        f"- Each snippet must be self-contained and skimmable on its own.\n\n"
        f"Here are the notes:\n"
        f"{notes_text[:8000]}"
    )

    env = os.environ.copy()
    env["HERMES_HOME"] = str(PROFILES_DIR / "quizmaster")
    env["AGENT_LOG_DB"] = str(AGENT_LOG_DB)
    _inject_telegram_env(env)
    _hydrate_agent_env(env, PROFILES_DIR / "quizmaster")
    env.setdefault("HERMES_INFERENCE_PROVIDER", "custom:tokenrouter")
    env.setdefault("HERMES_INFERENCE_MODEL", "MiniMax-M3")

    if not HERMES_PYTHON.is_file():
        raise HTTPException(status_code=503, detail=f"hermes python not found: {HERMES_PYTHON}")

    _gen = asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(HERMES_PYTHON), "-m", "hermes_cli.main",
            "-z", task,
            "--provider", "custom:tokenrouter",
            "--model", "MiniMax-M3",
            "--yolo", "--accept-hooks",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    )
    _gen.add_done_callback(lambda f: f.exception() and print(f"[flashcard-gen] spawn failed: {f.exception()}"))

    return JSONResponse({
        "ok": True,
        "message": f"Quizmaster is generating a flashcard deck for {subject}. It will appear in the deck list shortly.",
        "filename": deck_filename,
    })


@app.get("/api/subjects/{subject}/flashcard")
async def list_subject_flashcards(subject: str) -> JSONResponse:
    """List flashcard deck files for a subject, newest first."""
    subject_dir = _safe_subject(subject)
    flashcards_dir = subject_dir / "flashcards"
    items: list[dict[str, Any]] = []
    if flashcards_dir.is_dir():
        for p in sorted(flashcards_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            items.append({
                "filename": p.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
    return JSONResponse({"subject": subject, "items": items})


def _parse_flashcard_file(text: str) -> list[str]:
    """Parse a flashcard deck into individual snippets, splitting on --- separators."""
    cards: list[str] = []
    for block in re.split(r"^\s*---\s*$", text, flags=re.MULTILINE):
        block = block.strip()
        # Remove markdown headings line noise
        lines = [ln for ln in block.splitlines() if not ln.strip().startswith("#")]
        snippet = "\n".join(lines).strip()
        if snippet:
            cards.append(snippet)
    return cards


@app.get("/api/subjects/{subject}/flashcard/{filename}")
async def get_subject_flashcard(subject: str, filename: str) -> JSONResponse:
    """Parse a flashcard deck file into a list of snippet strings."""
    subject_dir = _safe_subject(subject)
    flashcards_dir = subject_dir / "flashcards"
    target = (flashcards_dir / filename).resolve()
    if not str(target).startswith(str(flashcards_dir.resolve())):
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="deck not found")
    text = target.read_text(encoding="utf-8", errors="replace")
    cards = _parse_flashcard_file(text)
    return JSONResponse({
        "subject": subject,
        "filename": filename,
        "count": len(cards),
        "cards": cards,
    })



@app.get("/api/tasks")
async def list_tasks(status: str | None = None) -> JSONResponse:
    """List tasks ordered by position."""
    conn = _db(PRODUCTIVITY_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if status:
        c.execute("SELECT * FROM tasks WHERE status = ? ORDER BY position ASC, created_at DESC", (status,))
    else:
        c.execute("SELECT * FROM tasks ORDER BY position ASC, created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    # counts per status
    c.execute("SELECT status, COUNT(*) as count FROM tasks GROUP BY status")
    counts = {r["status"]: r["count"] for r in c.fetchall()}
    conn.close()
    return JSONResponse({"items": rows, "counts": counts, "statuses": ["todo", "in_progress", "done"]})


@app.post("/api/tasks")
async def upsert_task(payload: dict[str, Any]) -> JSONResponse:
    """Create or update a task."""
    task_id = str(payload.get("id", "")).strip()
    title = str(payload.get("title", "")).strip()
    subject = str(payload.get("subject", "")).strip() or None
    status = str(payload.get("status", "todo")).strip()
    position = float(payload.get("position", 0.0))
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    if not task_id:
        import uuid
        task_id = uuid.uuid4().hex[:12]
    created_at = str(payload.get("created_at", "")).strip() or datetime.now(timezone.utc).isoformat()
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO tasks (id, title, subject, status, position, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, subject=excluded.subject, status=excluded.status, position=excluded.position",
        (task_id, title, subject, status, position, created_at),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": task_id})


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> JSONResponse:
    """Delete a task."""
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="task not found")
    return JSONResponse({"ok": True})



@app.get("/api/stickies")
async def list_stickies() -> JSONResponse:
    conn = _db(PRODUCTIVITY_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM stickies ORDER BY position ASC, created_at ASC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return JSONResponse({"items": rows})


@app.post("/api/stickies")
async def upsert_sticky(payload: dict[str, Any]) -> JSONResponse:
    sid = str(payload.get("id", "")).strip()
    content = str(payload.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    import uuid
    if not sid:
        sid = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
    else:
        conn = _db(PRODUCTIVITY_DB)
        c = conn.cursor()
        c.execute("SELECT created_at FROM stickies WHERE id = ?", (sid,))
        row = c.fetchone()
        created_at = row[0] if row else datetime.now(timezone.utc).isoformat()
        conn.close()
    color = str(payload.get("color", "amber")).strip()
    position = float(payload.get("position", 0))
    updated_at = datetime.now(timezone.utc).isoformat()
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO stickies (id, content, color, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET content=excluded.content, color=excluded.color, position=excluded.position, updated_at=excluded.updated_at",
        (sid, content, color, position, created_at, updated_at),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": sid, "sticky": {"id": sid, "content": content, "color": color, "position": position, "created_at": created_at, "updated_at": updated_at}})


@app.delete("/api/stickies/{sid}")
async def delete_sticky(sid: str) -> JSONResponse:
    conn = _db(PRODUCTIVITY_DB)
    c = conn.cursor()
    c.execute("DELETE FROM stickies WHERE id = ?", (sid,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="sticky not found")
    return JSONResponse({"ok": True})


@app.get("/api/pomodoro-today")
async def get_pomodoro_today() -> JSONResponse:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _db(PRODUCTIVITY_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT day, count, updated_at FROM pomodoro WHERE day = ?", (today,))
    row = c.fetchone()
    conn.close()
    if row:
        return JSONResponse({"day": row["day"], "count": row["count"], "updated_at": row["updated_at"]})
    return JSONResponse({"day": today, "count": 0, "updated_at": datetime.now(timezone.utc).isoformat()})


@app.post("/api/pomodoro-today/increment")
async def increment_pomodoro_today() -> JSONResponse:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn = _db(PRODUCTIVITY_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "INSERT INTO pomodoro (day, count, updated_at) VALUES (?, ?, ?) ON CONFLICT(day) DO UPDATE SET count = count + 1, updated_at = excluded.updated_at RETURNING day, count, updated_at",
        (today, 1, now),
    )
    row = dict(c.fetchone())
    conn.commit()
    conn.close()
    return JSONResponse({"day": row["day"], "count": row["count"], "updated_at": row["updated_at"]})


# ===========================================================================
# Obsidian vault — direct filesystem access
# ---------------------------------------------------------------------------
# Read-only vault browser. We list .md files, return their rendered contents
# (with light markdown cleanup for the dashboard), and offer substring search
# across the vault. We never mutate files from the dashboard — Obsidian owns
# writes so plugin state stays consistent.
# ===========================================================================

import re as _re
import html as _html


def _md_to_html(text: str) -> str:
    """Very small markdown→HTML pass: paragraphs, headings, code blocks, lists,
    bold, italic, inline code, links. Enough to render an Obsidian note in the
    dashboard without pulling in a full markdown lib."""
    # Escape first
    out = _html.escape(text)
    # Code blocks ```lang\n...\n```
    out = _re.sub(
        r"```([\w-]*)\n(.*?)```",
        lambda m: f'<pre class="md-pre"><code class="md-code md-code-lang-{_html.escape(m.group(1) or "txt")}">{m.group(2)}</code></pre>',
        out,
        flags=_re.DOTALL,
    )
    # Inline code `...`
    out = _re.sub(r"`([^`\n]+)`", r"<code class=\"md-code\">\1</code>", out)
    # Headings
    for level in range(6, 0, -1):
        prefix = "#" * level
        out = _re.sub(
            rf"^{prefix}\s+(.+)$",
            rf'<h{level} class="md-h md-h{level}">\1</h{level}>',
            out,
            flags=_re.MULTILINE,
        )
    # Bold **...**  Italic *...* or _..._
    out = _re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", out)
    out = _re.sub(r"(?<![_w])_([^_\n]+)_(?![_w])", r"<em>\1</em>", out)
    # Wiki-links [[Note Name]] and [[Note|Display]]
    out = _re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda m: f'<a class="md-wikilink" href="#" data-note="{_html.escape(m.group(1).strip())}">{_html.escape((m.group(2) or m.group(1)).strip())}</a>',
        out,
    )
    # Markdown links [text](href)
    out = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a class="md-link" href="\2" target="_blank" rel="noopener">\1</a>', out)
    # Blockquote
    out = _re.sub(r"^&gt;\s?(.+)$", r'<blockquote class="md-quote">\1</blockquote>', out, flags=_re.MULTILINE)
    # Unordered list (simple)
    out = _re.sub(r"^[-*]\s+(.+)$", r'<li class="md-li">\1</li>', out, flags=_re.MULTILINE)
    out = _re.sub(r"((?:<li class=\"md-li\">.*?</li>\n?)+)", r"<ul class=\"md-ul\">\1</ul>", out)
    # Paragraphs: split on blank lines for anything that isn't a block element
    blocks: list[str] = []
    buf: list[str] = []
    for line in out.split("\n"):
        if not line.strip():
            if buf:
                joined = " ".join(buf).strip()
                if joined and not joined.lstrip().startswith(("<h", "<pre", "<ul", "<blockquote", "<li")):
                    blocks.append(f"<p class=\"md-p\">{joined}</p>")
                else:
                    blocks.append(joined)
                buf = []
        else:
            buf.append(line)
    if buf:
        joined = " ".join(buf).strip()
        if joined and not joined.lstrip().startswith(("<h", "<pre", "<ul", "<blockquote", "<li")):
            blocks.append(f"<p class=\"md-p\">{joined}</p>")
        else:
            blocks.append(joined)
    return "\n".join(blocks)


@app.get("/api/obsidian/status")
async def obsidian_status() -> dict[str, Any]:
    """Return whether the configured vault exists and how many notes it contains."""
    if not OBSIDIAN_VAULT.is_dir():
        return {"ok": False, "path": str(OBSIDIAN_VAULT), "error": "vault not found"}
    notes = [p for p in OBSIDIAN_VAULT.rglob("*.md")]
    folders = sorted({p.parent.relative_to(OBSIDIAN_VAULT).as_posix() for p in notes})
    return {
        "ok": True,
        "path": str(OBSIDIAN_VAULT),
        "name": OBSIDIAN_VAULT.name,
        "note_count": len(notes),
        "folders": folders,
    }


@app.get("/api/obsidian/notes")
async def obsidian_list_notes(q: str = "", folder: str = "", limit: int = 200) -> dict[str, Any]:
    """List notes in the vault, optionally filtered by folder and/or substring.

    Returns relative path, name (without .md), folder, size, mtime.
    """
    if not OBSIDIAN_VAULT.is_dir():
        raise HTTPException(status_code=404, detail=f"vault not found: {OBSIDIAN_VAULT}")
    notes: list[dict[str, Any]] = []
    q_lower = q.lower()
    folder_prefix = folder.strip("/")
    for p in OBSIDIAN_VAULT.rglob("*.md"):
        rel = p.relative_to(OBSIDIAN_VAULT)
        rel_str = rel.as_posix()
        if folder_prefix and not rel_str.startswith(folder_prefix):
            continue
        name = p.stem
        if q_lower and q_lower not in name.lower() and q_lower not in rel_str.lower():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        notes.append({
            "path": rel_str,
            "name": name,
            "folder": rel.parent.as_posix(),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
        })
    notes.sort(key=lambda n: n["mtime"], reverse=True)
    return {
        "count": len(notes),
        "notes": notes[:limit],
        "truncated": len(notes) > limit,
    }


@app.get("/api/obsidian/notes/{path:path}")
async def obsidian_read_note(path: str) -> dict[str, Any]:
    """Read a single note, returning raw markdown, html-rendered preview, and
    frontmatter (if any)."""
    # Strip leading slash to avoid escape issues
    safe_rel = path.lstrip("/")
    # Defence-in-depth: reject any traversal — even though rglob is not used here.
    if ".." in safe_rel.split("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    full = (OBSIDIAN_VAULT / safe_rel).resolve()
    if not str(full).startswith(str(OBSIDIAN_VAULT.resolve())):
        raise HTTPException(status_code=400, detail="path escapes vault")
    if not full.is_file():
        raise HTTPException(status_code=404, detail=f"note not found: {safe_rel}")
    try:
        raw = full.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")
    frontmatter: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        # Pull off YAML frontmatter if present
        end = raw.find("\n---", 3)
        if end != -1:
            header = raw[3:end].strip()
            body = raw[end + 4 :].lstrip("\n")
            for line in header.split("\n"):
                if ":" in line and not line.startswith(" ") and not line.startswith("-"):
                    k, _, v = line.partition(":")
                    frontmatter[k.strip()] = v.strip().strip('"').strip("'")
    return {
        "path": safe_rel,
        "name": full.stem,
        "raw": raw,
        "html": _md_to_html(body),
        "frontmatter": frontmatter,
        "size": full.stat().st_size,
        "mtime": int(full.stat().st_mtime),
    }


@app.get("/api/obsidian/search")
async def obsidian_search(q: str = "", limit: int = 30) -> dict[str, Any]:
    """Substring search across all notes; returns snippets around each match."""
    if not q.strip():
        return {"query": q, "count": 0, "results": []}
    if not OBSIDIAN_VAULT.is_dir():
        raise HTTPException(status_code=404, detail=f"vault not found: {OBSIDIAN_VAULT}")
    results: list[dict[str, Any]] = []
    q_lower = q.lower()
    for p in OBSIDIAN_VAULT.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text_lower = text.lower()
        idx = text_lower.find(q_lower)
        if idx == -1:
            continue
        # Build a snippet around the first match
        start = max(0, idx - 60)
        end = min(len(text), idx + len(q) + 100)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        # Highlight the match
        highlighted = (
            _html.escape(snippet[: idx - start])
            + "<mark>"
            + _html.escape(snippet[idx - start : idx - start + len(q)])
            + "</mark>"
            + _html.escape(snippet[idx - start + len(q) :])
        )
        results.append({
            "path": p.relative_to(OBSIDIAN_VAULT).as_posix(),
            "name": p.stem,
            "snippet": snippet.strip(),
            "highlighted": highlighted,
            "match_count": text_lower.count(q_lower),
        })
        if len(results) >= limit:
            break
    results.sort(key=lambda r: r["match_count"], reverse=True)
    return {"query": q, "count": len(results), "results": results}


# Wikilink matcher reused by the graph builder: [[Target]], [[Target|alias]],
# [[Target#heading]]. We only care about the target (before | and #).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


@app.get("/api/obsidian/graph")
def obsidian_graph(limit: int = 2000) -> dict[str, Any]:
    """Vault link graph — the classic Obsidian graph, rendered as a neural net.

    One node per ``.md`` note, one undirected edge per ``[[wikilink]]`` that
    resolves to another note. Each node carries ``deg`` (link count → hub size),
    ``folder`` (cluster colour) and file metadata. Read-only; never mutates the
    vault. Hidden dot-folders (``.obsidian``, ``.trash``, ``.gemini`` …) skipped.

    Declared ``def`` (not ``async``): it walks + reads every note, so Starlette
    runs it in a threadpool and the event loop is never blocked.
    """
    if not OBSIDIAN_VAULT.is_dir():
        return {"ok": False, "path": str(OBSIDIAN_VAULT), "error": "vault not found",
                "nodes": [], "links": [], "folders": [],
                "stats": {"notes": 0, "links": 0, "orphans": 0}}

    raw: list[dict[str, Any]] = []
    by_stem: dict[str, str] = {}        # lower basename          -> node id (first wins)
    by_path: dict[str, str] = {}        # lower rel path (no .md)  -> node id (exact)
    # sorted() so "first basename wins" is stable across machines / filesystems.
    for p in sorted(OBSIDIAN_VAULT.rglob("*.md")):
        rel = p.relative_to(OBSIDIAN_VAULT)
        if any(part.startswith(".") for part in rel.parts):
            continue
        nid = rel.as_posix()
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        # Pull just the wikilink targets now; never retain the full body so peak
        # memory is O(#links), not O(sum of note sizes).
        targets: list[str] = []
        for hit in _WIKILINK_RE.findall(text):
            tgt = hit.split("|", 1)[0].split("#", 1)[0].strip()
            if tgt:
                targets.append(tgt)
        raw.append({"id": nid, "label": p.stem, "_dir": rel.parts[:-1],
                    "size": size, "mtime": mtime, "_targets": targets})
        by_stem.setdefault(p.stem.lower(), nid)
        by_path[nid[:-3].lower()] = nid          # strip the ".md" suffix
        if len(raw) >= limit:
            break

    # Cluster colouring: strip the directory prefix shared by EVERY note (vaults
    # commonly wrap everything in one wrapper dir like "Workflow/") so the real
    # categories become the clusters. A note sitting inside the shared prefix
    # itself is named by its own deepest folder via min(cpl, len-1) — never
    # collapsed to "(root)" (only genuine vault-root notes get that), so the
    # brain never goes one-colour while real sub-folders exist.
    dirs = [n["_dir"] for n in raw]
    cpl = 0
    if dirs:
        shortest = min(len(d) for d in dirs)
        while cpl < shortest and all(d[cpl] == dirs[0][cpl] for d in dirs):
            cpl += 1
    for n in raw:
        d = n["_dir"]
        n["folder"] = d[min(cpl, len(d) - 1)] if d else "(root)"

    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    degree: dict[str, int] = {n["id"]: 0 for n in raw}
    for n in raw:
        resolved: set[str] = set()
        for tgt in n["_targets"]:
            t = tgt.lower()
            # Obsidian precedence: exact path, then bare basename, then the
            # basename of a path-qualified link ([[folder/Note]]).
            tid = by_path.get(t) or by_stem.get(t)
            if tid is None and "/" in t:
                tid = by_stem.get(t.rsplit("/", 1)[-1])
            if tid and tid != n["id"]:
                resolved.add(tid)
        for tid in resolved:
            key = (n["id"], tid) if n["id"] < tid else (tid, n["id"])
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": n["id"], "target": tid})
            degree[n["id"]] += 1
            degree[tid] += 1

    nodes = [{
        "id": n["id"], "label": n["label"], "folder": n["folder"],
        "deg": degree.get(n["id"], 0), "size": n["size"], "mtime": n["mtime"],
    } for n in raw]
    folders = sorted({n["folder"] for n in raw})
    orphans = sum(1 for nd in nodes if nd["deg"] == 0)

    return {"ok": True, "path": str(OBSIDIAN_VAULT), "name": OBSIDIAN_VAULT.name,
            "nodes": nodes, "links": links, "folders": folders,
            "stats": {"notes": len(nodes), "links": len(links), "orphans": orphans}}


# ===========================================================================
# Profile avatar — a single uploadable photo (replaces the "D" initial)
# ===========================================================================
PROFILE_AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_AVATAR_BYTES = 8 * 1024 * 1024  # 8 MB is plenty for an avatar


def _current_avatar() -> Path | None:
    """The single stored avatar file (avatar.<ext>), or None."""
    for p in sorted(PROFILE_DIR.glob("avatar.*")):
        if p.is_file():
            return p
    return None


def _avatar_url(p: Path) -> str:
    try:
        mtime = int(p.stat().st_mtime)
    except OSError:
        mtime = 0
    return f"/static/profile/{p.name}?t={mtime}"   # cache-bust on every change


def _looks_like_image(b: bytes) -> bool:
    return (
        b[:8] == b"\x89PNG\r\n\x1a\n"                       # png
        or b[:3] == b"\xff\xd8\xff"                          # jpeg
        or b[:6] in (b"GIF87a", b"GIF89a")                   # gif
        or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")         # webp
    )


@app.get("/api/profile")
async def get_profile() -> dict[str, Any]:
    """Return the current avatar URL (or null → the app bar shows the initial)."""
    p = _current_avatar()
    return {"avatar_url": _avatar_url(p) if p else None}


@app.post("/api/profile/avatar")
async def upload_profile_avatar(file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a single profile photo, replacing any previous one. Only ever writes
    inside static/profile/; never touches anything else in the vault or home."""
    ext = Path(file.filename or "").suffix.lower()
    if ext == ".jpe":
        ext = ".jpg"
    if ext not in PROFILE_AVATAR_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported image type: {ext or '?'}")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="image too large (max 8 MB)")
    if not _looks_like_image(data):
        raise HTTPException(status_code=400, detail="file is not a valid image")
    # One canonical avatar — drop any previous extension variants first.
    for old in PROFILE_DIR.glob("avatar.*"):
        try:
            old.unlink()
        except OSError:
            pass
    dest = PROFILE_DIR / f"avatar{ext}"
    async with aiofiles.open(dest, "wb") as fh:
        await fh.write(data)
    return {"ok": True, "avatar_url": _avatar_url(dest)}


@app.delete("/api/profile/avatar")
async def delete_profile_avatar() -> dict[str, Any]:
    """Remove the avatar → the app bar falls back to the initial."""
    removed = False
    for old in PROFILE_DIR.glob("avatar.*"):
        try:
            old.unlink()
            removed = True
        except OSError:
            pass
    return {"ok": True, "removed": removed}


# ===========================================================================
# Global ⌘K search
# ---------------------------------------------------------------------------
# One endpoint that searches across: obsidian vault notes (filename match) and
# subject notes from ~/subjects. Returned in a single ranked list with a "kind"
# tag so the frontend can render different icons.
# ===========================================================================


@app.get("/api/search/global")
async def global_search(q: str = "", limit: int = 25) -> dict[str, Any]:
    """Ranked cross-source search. Empty query returns an empty list."""
    if not q.strip():
        return {"query": q, "count": 0, "results": []}
    ql = q.lower()
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()  # dedupe by (kind, id)

    def add(kind: str, key: str, title: str, subtitle: str, score: float, deep_link: str = "") -> None:
        if (kind, key) in seen:
            return
        seen.add((kind, key))
        results.append({
            "kind": kind,
            "id": key,
            "title": (title or "")[:140],
            "subtitle": (subtitle or "")[:200],
            "score": round(score, 3),
            "deep_link": deep_link,
        })

    # 3) Obsidian vault notes
    if OBSIDIAN_VAULT.is_dir():
        for p in OBSIDIAN_VAULT.rglob("*.md"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = p.stem
            path_str = p.relative_to(OBSIDIAN_VAULT).as_posix()
            score = 0.0
            if ql in name.lower():
                score = max(score, 0.9)
            if ql in path_str.lower():
                score = max(score, 0.5)
            # Substring-in-content (first 4 KB only, for speed)
            if ql in text[:4096].lower():
                score = max(score, 0.6)
            if score > 0:
                idx = text.lower().find(ql)
                snippet = ""
                if idx != -1:
                    start = max(0, idx - 50)
                    end = min(len(text), idx + 120)
                    snippet = text[start:end].strip()
                add("obsidian", path_str, name,
                    f"{p.parent.name}/  ·  Obsidian" + (f"  ·  {snippet[:80]}" if snippet else ""),
                    score, deep_link="obsidian-vault")
            if len(results) >= limit * 2:
                break

    # 4) Subject notes (~/subjects/<subject>/notes/*.md)
    if SUBJECTS_DIR.is_dir():
        for p in SUBJECTS_DIR.rglob("*.md"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "quiz" in p.name.lower():
                continue
            name = p.stem
            rel = p.relative_to(SUBJECTS_DIR)
            score = 0.0
            if ql in name.lower() or ql in rel.as_posix().lower():
                score = max(score, 0.7)
            if ql in text[:2048].lower():
                score = max(score, 0.4)
            if score > 0:
                add("subject-note", rel.as_posix(), name,
                    f"Subject notes  ·  {rel.parent.as_posix()}", score,
                    deep_link="library-notes")
            if len(results) >= limit * 2:
                break

    results.sort(key=lambda r: r["score"], reverse=True)
    return {"query": q, "count": len(results), "results": results[:limit]}


# ===========================================================================
# Daily Mission Briefing
# ---------------------------------------------------------------------------
# Server-side: pulls today's study data + yesterday's Obsidian daily note +
# recently-added Open Notebook sources, asks the configured LLM to produce
# a 5-bullet brief with a suggested focus, and caches the result for 1 h.
# Returns markdown so the frontend can render it as-is.
# ===========================================================================

import time as _time

_BRIEFING_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_BRIEFING_TTL = 3600.0  # 1 hour


async def _build_briefing() -> dict[str, Any]:
    """Assemble today's data and return markdown + meta.

    Notebook counts come best-effort from the NotebookLM CLI; the brief itself is
    assembled deterministically (NotebookLM answers against sources, not free
    prompts, so there is no drop-in remote LLM for an arbitrary briefing call)."""
    today = datetime.now().astimezone()
    today_iso = today.date().isoformat()
    yesterday_iso = (today.date() - timedelta(days=1)).isoformat()

    # NotebookLM notebook count (best-effort; never blocks the brief)
    nlm_notebooks = 0
    try:
        from notebooklm import _run as _nlm_run, _parse_json as _nlm_parse
        rc, out, _err = await _nlm_run(["list", "--json"], timeout=10.0)
        if rc == 0:
            ok, data = _nlm_parse(out)
            if ok:
                nbs = data.get("notebooks", data) if isinstance(data, dict) else data
                nlm_notebooks = len(nbs or [])
    except Exception:
        pass

    # Obsidian — yesterday's daily note (best guess from filename)
    obs_yesterday_snippet = ""
    if OBSIDIAN_VAULT.is_dir():
        for p in OBSIDIAN_VAULT.rglob("*.md"):
            if yesterday_iso in p.name:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    obs_yesterday_snippet = text[:1200]
                    break
                except OSError:
                    continue

    # Deterministic briefing (always available)
    bullets: list[str] = []
    if nlm_notebooks:
        bullets.append(
            f"**{nlm_notebooks} notebooks** are live in NotebookLM \u2014 open the NotebookLM tab "
            "to chat with your sources or generate a podcast, quiz, or report."
        )
    else:
        bullets.append(
            "No NotebookLM notebooks detected yet \u2014 create one from the NotebookLM tab "
            "and add your first sources."
        )
    if obs_yesterday_snippet:
        bullets.append("Yesterday's Obsidian note is loaded \u2014 pick one open thread to close before adding new ones.")
    else:
        bullets.append("No note dated yesterday in the Obsidian vault \u2014 start today's daily note and lock in one win.")
    bullets.append("Heatmap for today is empty so far; a single completed study block is enough to light it up.")
    bullets.append("**Suggested first task:** open the NotebookLM Chat tab and ask a question from your most recent source.")
    markdown = "\n\n".join([f"- {b}" for b in bullets])

    return {
        "date": today_iso,
        "generated_at": today.isoformat(timespec="seconds"),
        "markdown": markdown,
        "stats": {
            "notebooks": nlm_notebooks,
            "obsidian_yesterday_excerpt_chars": len(obs_yesterday_snippet),
        },
    }


@app.get("/api/briefing/today")
async def briefing_today(refresh: bool = False) -> dict[str, Any]:
    """Return today's daily mission briefing. Cached for 1h by default;
    pass ?refresh=1 to force a rebuild."""
    key = datetime.now().astimezone().date().isoformat()
    now = _time.time()
    cached = _BRIEFING_CACHE.get(key)
    if not refresh and cached and (now - cached[0]) < _BRIEFING_TTL:
        return cached[1]
    payload = await _build_briefing()
    _BRIEFING_CACHE[key] = (now, payload)
    # Keep only today's entry to avoid unbounded growth
    _BRIEFING_CACHE.clear()
    _BRIEFING_CACHE[key] = (now, payload)
    return payload


# Aurora v2.0 additive tools router — FSRS spaced repetition, exam readiness,
# AI tutor. Self-contained module; existing routes are untouched.
from tools import router as tools_router  # noqa: E402
from tracker import router as tracker_router  # noqa: E402
from memory import router as memory_router  # noqa: E402
from notebooklm import router as notebooklm_router  # noqa: E402
from voice import router as voice_router  # noqa: E402
from stats import router as stats_router  # noqa: E402
from anki import router as anki_router  # noqa: E402
from terminal import router as terminal_router  # noqa: E402
from graph import router as graph_router  # noqa: E402
from tts import router as tts_router  # noqa: E402
from automation_hooks import router as automation_hooks_router  # noqa: E402
from system_health import router as system_health_router  # noqa: E402
from knowledge import router as knowledge_router  # noqa: E402
from orchestrator import router as orchestrator_router  # noqa: E402

app.include_router(tools_router)
app.include_router(tracker_router)
app.include_router(memory_router)
app.include_router(notebooklm_router)
app.include_router(voice_router)
app.include_router(stats_router)
app.include_router(anki_router)
app.include_router(terminal_router)
app.include_router(graph_router)
app.include_router(tts_router)
app.include_router(automation_hooks_router)
app.include_router(system_health_router)
app.include_router(knowledge_router)
app.include_router(orchestrator_router)

# Static frontend (mounted last so /api/* and / take precedence).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=51763, log_level="info")
