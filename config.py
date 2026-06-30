"""
Central configuration for Mission Control.

Every external path, data source, and bind address the dashboard touches is
resolved **here**, from environment variables, with sensible defaults. Nothing
else in the codebase should hardcode an absolute path — import from this module
instead. This is what makes the project portable: drop a `.env` (see
`.env.example`) or export a few variables and the same code runs on any machine,
for any user, pointed at any Hermes install.

Design notes
------------
* Defaults derive from ``$HOME`` (``MC_HOME`` to override), so a clone works
  out of the box for the current user without any configuration.
* The dashboard is a **front-end over a Hermes install** (https://github.com —
  the open-source agent framework it ships data from). When Hermes data is
  absent the server still starts; data-backed tabs simply render empty. Point
  ``HERMES_HOME`` at a real install to light them up.
* Two interpreters, deliberately kept separate:
    - the *web server* runs under the dashboard's own venv (``requirements.txt``),
      so a ``hermes update`` that rebuilds Hermes' venv can't break it;
    - *agent invocations* shell out to ``HERMES_PYTHON`` (the Hermes venv),
      because they import ``hermes_cli``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    """Return ``$name`` as an expanded Path, or *default* when unset/empty."""
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw if raw else default


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------
HOME = _env_path("MC_HOME", Path.home())
HERMES_HOME = _env_path("HERMES_HOME", HOME / ".hermes")
DASHBOARD_DIR = Path(__file__).resolve().parent
STATIC_DIR = DASHBOARD_DIR / "static"

# Study/content roots (the markdown vault the dashboard indexes, research output).
SUBJECTS_DIR = _env_path("SUBJECTS_DIR", HOME / "subjects")
RESEARCH_DIR = _env_path("RESEARCH_DIR", HOME / "research")
VOICE_DIR = _env_path("MC_VOICE_DIR", HOME / "voice")

# Obsidian vault surfaced through the Obsidian / Brain tabs. Defaults to a data
# disk on the author's box; override with $OBSIDIAN_VAULT anywhere else.
OBSIDIAN_VAULT = _env_path(
    "OBSIDIAN_VAULT", Path("/mnt/storage/Obsidian/Obsidian_Vault_Master")
)

# Optional secondary note locations (legacy memory bridge).
LLM_WIKI_VAULT = _env_path("MC_LLM_WIKI", HOME / "Desktop" / "LLM Wiki")
MAIN_OBSIDIAN = _env_path("MC_MAIN_OBSIDIAN", HOME / "Obsidian")
CLAUDE_PROJECTS = _env_path("CLAUDE_PROJECTS_DIR", HOME / ".claude" / "projects")

# ---------------------------------------------------------------------------
# Hermes data sources (SQLite DBs + dirs, all under HERMES_HOME)
# ---------------------------------------------------------------------------
AGENT_LOG_DB = HERMES_HOME / "agent-logs.db"
RESEARCH_DB = HERMES_HOME / "research.db"
QUIZ_DB = HERMES_HOME / "quiz.db"
STUDY_DB = HERMES_HOME / "study.db"
FLASHCARD_DB = HERMES_HOME / "flashcards.db"
PRODUCTIVITY_DB = HERMES_HOME / "productivity.db"
CHAT_DB = HERMES_HOME / "chat.db"
TRACKER_DB = HERMES_HOME / "tracker.db"
MEMORY_CORE_DB = HERMES_HOME / "memory_core.db"
MEMORY_STORE_DB = HERMES_HOME / "memory_store.db"

PROFILES_DIR = HERMES_HOME / "profiles"
PLANNING_DIR = HERMES_HOME / "planning"
SHARED_DIR = HERMES_HOME / "shared"
NLM_DOWNLOAD_DIR = HERMES_HOME / "nlm_artifacts"
PIPELINE_SCRIPT = HERMES_HOME / "agents" / "bill" / "pipeline.py"
BUILD_FILE = HERMES_HOME / "dashboard-build.txt"
ENV_FILE = HERMES_HOME / ".env"  # holds bot tokens at runtime; never committed

# ---------------------------------------------------------------------------
# Dashboard-local data (lives in this repo dir; gitignored — personal)
# ---------------------------------------------------------------------------
KNOWLEDGE_DB = _env_path("KNOWLEDGE_DB", DASHBOARD_DIR / "memory_core.db")
CODEGRAPH_DB = DASHBOARD_DIR / ".codegraph" / "codegraph.db"
PLAN_FILE = DASHBOARD_DIR / "tracker_plan.json"
ROADMAP_META = DASHBOARD_DIR / "roadmap_meta.json"

# ---------------------------------------------------------------------------
# Interpreters & server bind
# ---------------------------------------------------------------------------
# Hermes venv python — used ONLY to shell out to `hermes` agent oneshots.
HERMES_PYTHON = _env_path(
    "HERMES_PYTHON", HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"
)
HERMES_AGENT_ROOT = HERMES_HOME / "hermes-agent"

# Where uvicorn binds. Defaults to loopback — safe for anyone who clones this.
# The author runs it behind Tailscale (set MC_HOST to the tailnet IP).
BIND_HOST = _env_str("MC_HOST", "127.0.0.1")
BIND_PORT = int(_env_str("MC_PORT", "51763"))

# ---------------------------------------------------------------------------
# Optional integrations (Anki export, Kokoro TTS) — degrade silently if absent
# ---------------------------------------------------------------------------
VOICE_TTS_DIR = _env_str("KOKORO_TTS_DIR", str(VOICE_DIR / "tts"))

# Display name surfaced in the greeting card and chat sender label.
USER_NAME = _env_str("MC_USER_NAME", "Student")
ANKI_VENV_PY = str(VOICE_DIR / "anki" / ".venv" / "bin" / "python")
ANKI_EXPORT_SCRIPT = str(VOICE_DIR / "anki" / "export.py")


# ---------------------------------------------------------------------------
# Graceful degradation helper
# ---------------------------------------------------------------------------
def safe_connect(db_path, *, read_only: bool = True):
    """Open *db_path* for reading, or return ``None`` if it doesn't exist.

    Lets data-backed endpoints return empty payloads on a machine without a
    Hermes install instead of creating stray empty SQLite files or 500-ing.
    Callers should treat ``None`` as "no data source configured".
    """
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        if read_only:
            return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        return sqlite3.connect(str(p))
    except sqlite3.Error:
        return None
