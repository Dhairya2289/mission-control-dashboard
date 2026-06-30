"""
Mission Control — Aurora tools router (v2.0 additive module).

Self-contained FastAPI APIRouter mounted by main.py with a single include line.
Adds the "active study engine" layer without touching existing routes:

  · FSRS-5 spaced repetition over the existing flashcard decks  -> study.db
  · Per-subject exam readiness from quiz.db + planning files
  · A provider-configurable AI tutor (tokenrouter / minimax-m3 by default),
    reading its key from the server environment / ~/.hermes/.env at call time.

Everything is local except the AI tutor, which degrades gracefully (clear 503)
when no LLM provider is configured.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import config

router = APIRouter()

# ---------------------------------------------------------------------------
# Paths (resolved centrally in config.py)
# ---------------------------------------------------------------------------
HERMES_HOME = config.HERMES_HOME
SUBJECTS_DIR = config.SUBJECTS_DIR
QUIZ_DB = HERMES_HOME / "quiz.db"
STUDY_DB = HERMES_HOME / "study.db"
PLANNING_DIR = HERMES_HOME / "planning"
AGENT_LOG_DB = HERMES_HOME / "agent-logs.db"
ENV_FILE = config.ENV_FILE

NOTE_EXTS = {".md", ".markdown", ".txt"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ===========================================================================
# FSRS-5 scheduler  (default parameters from open-spaced-repetition / py-fsrs)
# ===========================================================================
W = [
    0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046,
    1.54575, 0.1192, 1.01925, 1.9395, 0.11, 0.29605, 2.2698, 0.2315,
    2.9898, 0.51655, 0.6621,
]
DECAY = -0.5
FACTOR = 0.9 ** (1.0 / DECAY) - 1.0          # ≈ 0.2345679
DESIRED_RETENTION = 0.9
MAX_INTERVAL_DAYS = 365 * 4
NEW_PER_DAY_DEFAULT = 20

# rating: 1=Again 2=Hard 3=Good 4=Easy
# state:  0=new 1=learning 2=review 3=relearning


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _retrievability(elapsed_days: float, stability: float) -> float:
    if stability <= 0:
        return 0.0
    return (1.0 + FACTOR * elapsed_days / stability) ** DECAY


def _init_stability(rating: int) -> float:
    return max(0.1, W[rating - 1])


def _init_difficulty(rating: int) -> float:
    return _clamp(W[4] - math.exp(W[5] * (rating - 1)) + 1.0, 1.0, 10.0)


def _next_difficulty(d: float, rating: int) -> float:
    delta = -W[6] * (rating - 3)
    next_d = d + delta * (10.0 - d) / 9.0           # FSRS-5 linear damping
    reverted = W[7] * _init_difficulty(4) + (1.0 - W[7]) * next_d
    return _clamp(reverted, 1.0, 10.0)


def _stability_success(d: float, s: float, r: float, rating: int) -> float:
    hard_penalty = W[15] if rating == 2 else 1.0
    easy_bonus = W[16] if rating == 4 else 1.0
    inc = (
        math.exp(W[8])
        * (11.0 - d)
        * (s ** -W[9])
        * (math.exp(W[10] * (1.0 - r)) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return s * (1.0 + inc)


def _stability_fail(d: float, s: float, r: float) -> float:
    sf = W[11] * (d ** -W[12]) * (((s + 1.0) ** W[13]) - 1.0) * math.exp(W[14] * (1.0 - r))
    return min(sf, s)


def _interval_days(stability: float) -> int:
    iv = (stability / FACTOR) * (DESIRED_RETENTION ** (1.0 / DECAY) - 1.0)
    return int(_clamp(round(iv), 1, MAX_INTERVAL_DAYS))


def _schedule(card: dict[str, Any], rating: int) -> dict[str, Any]:
    """Apply FSRS to a card row, return updated scheduling fields + next due."""
    now = _now()
    state = int(card.get("state") or 0)
    s = card.get("stability")
    d = card.get("difficulty")
    last = _parse_iso(card.get("last_review"))
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)

    if not s or not d or state == 0:
        # First real exposure
        d = _init_difficulty(rating)
        s = _init_stability(rating)
    else:
        elapsed = max(0.0, (now - last).total_seconds() / 86400.0) if last else 0.0
        r = _retrievability(elapsed, s)
        d = _next_difficulty(d, rating)
        if rating == 1:
            s = _stability_fail(d, s, r)
        else:
            s = _stability_success(d, s, r, rating)

    reps += 1
    if rating == 1:
        lapses += 1
        new_state = 3                         # relearning
        due = now + timedelta(minutes=10)     # re-queue this session
    else:
        iv = _interval_days(s)
        new_state = 2                         # review
        if iv < 1:
            due = now + timedelta(minutes=10)
        else:
            due = now + timedelta(days=iv)

    return {
        "stability": round(float(s), 4),
        "difficulty": round(float(d), 4),
        "due": _iso(due),
        "state": new_state,
        "reps": reps,
        "lapses": lapses,
        "last_review": _iso(now),
        "next_interval_days": _interval_days(s) if rating != 1 else 0,
    }


def _preview_intervals(card: dict[str, Any]) -> dict[str, str]:
    """Human-friendly 'next due' labels for each of the 4 grade buttons."""
    out: dict[str, str] = {}
    for rating, label in ((1, "again"), (2, "hard"), (3, "good"), (4, "easy")):
        sched = _schedule(dict(card), rating)
        due = _parse_iso(sched["due"])
        delta = (due - _now())
        mins = delta.total_seconds() / 60.0
        if mins < 60:
            out[label] = f"{max(1, round(mins))}m"
        elif mins < 60 * 24:
            out[label] = f"{round(mins / 60)}h"
        else:
            days = round(mins / (60 * 24))
            out[label] = f"{days}d" if days < 30 else (f"{round(days/30)}mo" if days < 365 else f"{round(days/365,1)}y")
    return out


# ===========================================================================
# study.db
# ===========================================================================
def _study_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STUDY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _init_study_db() -> None:
    conn = sqlite3.connect(str(STUDY_DB))
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sr_cards (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            deck TEXT NOT NULL,
            front TEXT NOT NULL,
            state INTEGER NOT NULL DEFAULT 0,
            stability REAL,
            difficulty REAL,
            due TEXT,
            reps INTEGER NOT NULL DEFAULT 0,
            lapses INTEGER NOT NULL DEFAULT 0,
            last_review TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cards_due ON sr_cards(due)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cards_subject ON sr_cards(subject)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS sr_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            rating INTEGER NOT NULL,
            state INTEGER NOT NULL,
            reviewed_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_at ON sr_reviews(reviewed_at)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL DEFAULT 'review',
            cards_reviewed INTEGER NOT NULL DEFAULT 0,
            focus_minutes REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT
        )
    """)
    conn.commit()
    conn.close()


_init_study_db()


def _parse_flashcard_file(text: str) -> list[str]:
    cards: list[str] = []
    for block in re.split(r"^\s*---\s*$", text, flags=re.MULTILINE):
        block = block.strip()
        lines = [ln for ln in block.splitlines() if not ln.strip().startswith("#")]
        snippet = "\n".join(lines).strip()
        if snippet:
            cards.append(snippet)
    return cards


def _card_id(subject: str, deck: str, front: str) -> str:
    h = hashlib.sha1(f"{subject}\x1f{deck}\x1f{front}".encode("utf-8")).hexdigest()
    return h[:20]


# ===========================================================================
# Spaced repetition endpoints
# ===========================================================================
@router.post("/api/study/sync")
async def study_sync() -> JSONResponse:
    """Import / refresh SR cards from every subject's flashcard decks. Idempotent:
    existing cards keep their schedule; new snippets are added; vanished ones
    are deactivated (history preserved)."""
    now_iso = _iso(_now())
    conn = _study_conn()
    c = conn.cursor()
    c.execute("UPDATE sr_cards SET active = 0")
    added = 0
    seen = 0
    if SUBJECTS_DIR.is_dir():
        for subj_dir in sorted(SUBJECTS_DIR.iterdir()):
            if not subj_dir.is_dir():
                continue
            fc_dir = subj_dir / "flashcards"
            if not fc_dir.is_dir():
                continue
            subject = subj_dir.name
            for deck in sorted(fc_dir.glob("*.md")):
                try:
                    text = deck.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for front in _parse_flashcard_file(text):
                    seen += 1
                    cid = _card_id(subject, deck.name, front)
                    row = c.execute("SELECT id FROM sr_cards WHERE id = ?", (cid,)).fetchone()
                    if row:
                        c.execute("UPDATE sr_cards SET active = 1, front = ? WHERE id = ?", (front, cid))
                    else:
                        c.execute(
                            "INSERT INTO sr_cards (id, subject, deck, front, state, due, active, created_at) "
                            "VALUES (?, ?, ?, ?, 0, ?, 1, ?)",
                            (cid, subject, deck.name, front, now_iso, now_iso),
                        )
                        added += 1
    conn.commit()
    total = c.execute("SELECT COUNT(*) FROM sr_cards WHERE active = 1").fetchone()[0]
    conn.close()
    return JSONResponse({"ok": True, "added": added, "seen": seen, "active_total": total})


def _card_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


@router.get("/api/study/queue")
async def study_queue(limit: int = 20, new_limit: int = NEW_PER_DAY_DEFAULT, subject: str | None = None) -> JSONResponse:
    """Cards due now (review/relearning) first, then a capped slice of new cards."""
    now_iso = _iso(_now())
    conn = _study_conn()
    c = conn.cursor()
    subj_clause = " AND subject = ?" if subject else ""
    params_due: tuple = (now_iso,) + ((subject,) if subject else tuple())
    due_rows = c.execute(
        f"SELECT * FROM sr_cards WHERE active = 1 AND state != 0 AND (due IS NULL OR due <= ?){subj_clause} "
        f"ORDER BY due ASC LIMIT ?",
        params_due + (limit,),
    ).fetchall()
    remaining = max(0, limit - len(due_rows))
    new_rows: list[sqlite3.Row] = []
    if remaining > 0:
        params_new: tuple = ((subject,) if subject else tuple())
        new_rows = c.execute(
            f"SELECT * FROM sr_cards WHERE active = 1 AND state = 0{subj_clause} "
            f"ORDER BY created_at ASC LIMIT ?",
            params_new + (min(remaining, new_limit),),
        ).fetchall()
    conn.close()
    queue = [_card_to_dict(r) for r in (list(due_rows) + list(new_rows))]
    for card in queue:
        card["preview"] = _preview_intervals(card)
    return JSONResponse({"queue": queue, "due_count": len(due_rows), "new_count": len(new_rows)})


@router.post("/api/study/grade")
async def study_grade(payload: dict[str, Any]) -> JSONResponse:
    card_id = str(payload.get("card_id", "")).strip()
    rating = int(payload.get("rating", 0))
    if not card_id or rating not in (1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="card_id and rating(1-4) required")
    conn = _study_conn()
    c = conn.cursor()
    row = c.execute("SELECT * FROM sr_cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="card not found")
    sched = _schedule(_card_to_dict(row), rating)
    c.execute(
        "UPDATE sr_cards SET state=?, stability=?, difficulty=?, due=?, reps=?, lapses=?, last_review=? WHERE id=?",
        (sched["state"], sched["stability"], sched["difficulty"], sched["due"],
         sched["reps"], sched["lapses"], sched["last_review"], card_id),
    )
    c.execute(
        "INSERT INTO sr_reviews (card_id, rating, state, reviewed_at) VALUES (?, ?, ?, ?)",
        (card_id, rating, sched["state"], sched["last_review"]),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "card_id": card_id, "due": sched["due"], "next_interval_days": sched["next_interval_days"]})


@router.get("/api/study/stats")
async def study_stats() -> JSONResponse:
    now = _now()
    now_iso = _iso(now)
    today = now.date().isoformat()
    conn = _study_conn()
    c = conn.cursor()
    active = c.execute("SELECT COUNT(*) FROM sr_cards WHERE active = 1").fetchone()[0]
    due = c.execute("SELECT COUNT(*) FROM sr_cards WHERE active=1 AND state!=0 AND (due IS NULL OR due <= ?)", (now_iso,)).fetchone()[0]
    new_avail = c.execute("SELECT COUNT(*) FROM sr_cards WHERE active=1 AND state=0").fetchone()[0]
    mature = c.execute("SELECT COUNT(*) FROM sr_cards WHERE active=1 AND stability >= 21").fetchone()[0]
    reviewed_today = c.execute("SELECT COUNT(*) FROM sr_reviews WHERE substr(reviewed_at,1,10) = ?", (today,)).fetchone()[0]
    # retention: share of non-Again grades over last 30 days
    rows = c.execute(
        "SELECT rating FROM sr_reviews WHERE reviewed_at >= ?",
        (_iso(now - timedelta(days=30)),),
    ).fetchall()
    total_r = len(rows)
    good = sum(1 for r in rows if r["rating"] != 1)
    retention = round(100.0 * good / total_r, 1) if total_r else None
    # streak: consecutive days (incl today or yesterday) with >=1 review
    days = {r["d"] for r in c.execute("SELECT DISTINCT substr(reviewed_at,1,10) AS d FROM sr_reviews").fetchall()}
    streak = 0
    probe = now.date()
    if today not in days and (now.date() - timedelta(days=1)).isoformat() in days:
        probe = now.date() - timedelta(days=1)
    while probe.isoformat() in days:
        streak += 1
        probe = probe - timedelta(days=1)
    # per-subject due
    per_subject = [
        {"subject": r["subject"], "due": r["due_n"], "total": r["total_n"]}
        for r in c.execute(
            "SELECT subject, COUNT(*) AS total_n, "
            "SUM(CASE WHEN state!=0 AND (due IS NULL OR due <= ?) THEN 1 ELSE 0 END) AS due_n "
            "FROM sr_cards WHERE active=1 GROUP BY subject ORDER BY due_n DESC",
            (now_iso,),
        ).fetchall()
    ]
    conn.close()
    return JSONResponse({
        "active": active, "due": due, "new_available": new_avail, "mature": mature,
        "reviewed_today": reviewed_today, "retention": retention, "streak": streak,
        "per_subject": per_subject,
    })


@router.post("/api/study/session")
async def study_session(payload: dict[str, Any]) -> JSONResponse:
    mode = str(payload.get("mode", "review"))
    cards_reviewed = int(payload.get("cards_reviewed", 0))
    focus_minutes = float(payload.get("focus_minutes", 0))
    conn = _study_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO study_sessions (mode, cards_reviewed, focus_minutes, started_at, ended_at) VALUES (?, ?, ?, ?, ?)",
        (mode, cards_reviewed, focus_minutes, _iso(_now()), _iso(_now())),
    )
    sid = c.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": sid})


# ===========================================================================
# Exam readiness  (quiz.db accuracy + SR coverage + recency + exam countdown)
# ===========================================================================
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "%Y-%m-%d"),
    (re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b"), "%d/%m/%Y"),
]


def _find_dates(text: str) -> list[datetime]:
    out: list[datetime] = []
    for m in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b", text):
        try:
            out.append(datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    return out


def _exam_dates_for_subject(subject: str) -> datetime | None:
    """Nearest future exam date mentioning this subject, scanning planning files."""
    candidates: list[datetime] = []
    sources: list[Path] = []
    for name in ("exams.md", "deadlines.md"):
        p = PLANNING_DIR / name
        if p.is_file():
            sources.append(p)
    # Path-traversal guard (same as _subject_notes_snippet): keep request-supplied
    # `subject` inside SUBJECTS_DIR before globbing/reading planning files.
    _base = SUBJECTS_DIR.resolve()
    try:
        subj_plan = (_base / subject / "planning").resolve()
    except (OSError, RuntimeError, ValueError):
        subj_plan = None
    if subj_plan and subj_plan.is_relative_to(_base) and subj_plan.is_dir():
        sources.extend([p for p in subj_plan.glob("*.md")])
    now = _now()
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_subject_file = src.parent.parent.name == subject
        for line in text.splitlines():
            if not in_subject_file and subject.lower() not in line.lower():
                continue
            for dt in _find_dates(line):
                if dt >= now - timedelta(days=1):
                    candidates.append(dt)
    return min(candidates) if candidates else None


@router.get("/api/study/readiness")
async def study_readiness() -> JSONResponse:
    now = _now()
    now_iso = _iso(now)

    # quiz accuracy + recency per subject
    quiz: dict[str, dict[str, Any]] = {}
    if QUIZ_DB.is_file():
        qc = sqlite3.connect(f"file:{QUIZ_DB}?mode=ro", uri=True)
        qc.row_factory = sqlite3.Row
        for r in qc.execute(
            "SELECT subject, AVG(percentage) AS avg_pct, COUNT(*) AS n, MAX(created_at) AS last "
            "FROM quiz_attempts GROUP BY subject"
        ).fetchall():
            quiz[r["subject"]] = {"avg_pct": r["avg_pct"], "n": r["n"], "last": r["last"]}
        qc.close()

    # SR coverage per subject
    sr: dict[str, dict[str, Any]] = {}
    sc = _study_conn()
    for r in sc.execute(
        "SELECT subject, COUNT(*) AS total, "
        "SUM(CASE WHEN stability>=21 THEN 1 ELSE 0 END) AS mature, "
        "SUM(CASE WHEN state!=0 AND (due IS NULL OR due<=?) THEN 1 ELSE 0 END) AS due "
        "FROM sr_cards WHERE active=1 GROUP BY subject",
        (now_iso,),
    ).fetchall():
        sr[r["subject"]] = {"total": r["total"], "mature": r["mature"] or 0, "due": r["due"] or 0}
    sc.close()

    subjects = set(quiz) | set(sr)
    if SUBJECTS_DIR.is_dir():
        # Only real subject folders — skip hidden dirs (.stfolder etc.) and
        # underscore-prefixed scratch dirs (_autotest) that aren't subjects.
        subjects |= {
            p.name
            for p in SUBJECTS_DIR.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        }

    items = []
    for subject in sorted(subjects):
        q = quiz.get(subject, {})
        s = sr.get(subject, {})
        acc = q.get("avg_pct")
        total_cards = s.get("total", 0)
        mature = s.get("mature", 0)
        coverage = (mature / total_cards) if total_cards else 0.0
        last_q = _parse_iso(q.get("last"))
        days_since = (now - last_q).days if last_q else None
        recency_factor = 1.0
        if days_since is not None:
            recency_factor = max(0.4, 1.0 - days_since / 30.0)
        # readiness 0-100: 55% accuracy, 30% mature-coverage, 15% recency
        acc_component = (acc / 100.0) if acc is not None else 0.0
        has_data = (acc is not None) or total_cards > 0
        readiness = round(100.0 * (0.55 * acc_component + 0.30 * coverage + 0.15 * recency_factor)) if has_data else None
        exam_dt = _exam_dates_for_subject(subject)
        days_to_exam = (exam_dt.date() - now.date()).days if exam_dt else None
        # urgency: soon exam + low readiness => high
        urgency = 0.0
        if days_to_exam is not None:
            time_pressure = max(0.0, 1.0 - days_to_exam / 30.0)
            gap = 1.0 - ((readiness or 0) / 100.0)
            urgency = round(100.0 * (0.6 * time_pressure + 0.4 * gap), 1)
        items.append({
            "subject": subject,
            "readiness": readiness,
            "accuracy": round(acc, 1) if acc is not None else None,
            "quiz_attempts": q.get("n", 0),
            "cards_total": total_cards,
            "cards_mature": mature,
            "cards_due": s.get("due", 0),
            "days_since_quiz": days_since,
            "days_to_exam": days_to_exam,
            "exam_date": exam_dt.date().isoformat() if exam_dt else None,
            "urgency": urgency,
        })
    items.sort(key=lambda x: (x["days_to_exam"] if x["days_to_exam"] is not None else 9999, -(x["urgency"] or 0)))
    return JSONResponse({"items": items, "generated_at": now_iso})


# ===========================================================================
# AI tutor  (provider-configurable; tokenrouter / minimax-m3 by default)
# ===========================================================================
def _llm_config() -> dict[str, str | None]:
    """Resolve LLM endpoint from env, hydrating from ~/.hermes/.env if needed.
    Preference: TOKENROUTER_* -> STUDY_LLM_* -> KIMCHI_*. Never logged."""
    env: dict[str, str] = {}
    keys = (
        "TOKENROUTER_API_KEY", "TOKENROUTER_BASE_URL", "TOKENROUTER_MODEL",
        "STUDY_LLM_API_KEY", "STUDY_LLM_BASE_URL", "STUDY_LLM_MODEL",
        "KIMCHI_API_KEY", "KIMCHI_BASE_URL",
    )
    for k in keys:
        v = os.environ.get(k)
        if v:
            env[k] = v
    if ENV_FILE.is_file() and not all(env.get(k) for k in keys):
        try:
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                k = k.strip()
                if k in keys and not env.get(k):
                    env[k] = val.strip().strip('"').strip("'")
        except OSError:
            pass
    base = env.get("TOKENROUTER_BASE_URL") or env.get("STUDY_LLM_BASE_URL") or env.get("KIMCHI_BASE_URL")
    key = env.get("TOKENROUTER_API_KEY") or env.get("STUDY_LLM_API_KEY") or env.get("KIMCHI_API_KEY")
    model = env.get("TOKENROUTER_MODEL") or env.get("STUDY_LLM_MODEL")
    if not model:
        # minimax-m3 is the standing default (tokenrouter, unconfigured, or study creds);
        # only an explicit KIMCHI-only setup keeps the legacy kimi model.
        using_kimchi_only = bool(env.get("KIMCHI_API_KEY")) and not (
            env.get("TOKENROUTER_API_KEY") or env.get("STUDY_LLM_API_KEY")
        )
        model = "kimi-k2.6" if using_kimchi_only else "MiniMax-M3"
    return {"base": base.rstrip("/") if base else None, "key": key, "model": model}


def _strip_reasoning(text: str) -> str:
    """MiniMax-M3 (and other reasoning models) emit <think>…</think> chain-of-thought
    inline in the content. Show the user only the final answer."""
    import re
    # Drop well-formed reasoning blocks.
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    # If the answer was truncated mid-think (unclosed tag), the reasoning runs to
    # the end with no closing tag — discard everything from the open tag onward so
    # raw chain-of-thought never reaches the user.
    text = re.sub(r"(?is)<think>.*$", "", text)
    # Strip any remaining stray markers.
    text = re.sub(r"(?is)</?think>", "", text)
    return text.strip()


def _llm_chat(messages: list[dict[str, str]], *, temperature: float = 0.4, max_tokens: int = 1800) -> str:
    cfg = _llm_config()
    if not cfg["base"] or not cfg["key"]:
        raise HTTPException(
            status_code=503,
            detail="AI tutor not configured. Set TOKENROUTER_BASE_URL + TOKENROUTER_API_KEY "
                   "(model TOKENROUTER_MODEL, default MiniMax-M3) in ~/.hermes/.env.",
        )
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise HTTPException(status_code=502, detail=f"LLM provider error {e.code}: {detail}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    try:
        return _strip_reasoning(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        raise HTTPException(status_code=502, detail="Unexpected LLM response shape.")


def _subject_notes_snippet(subject: str, budget: int = 6000) -> str:
    # Path-traversal guard: `subject` comes straight from the /api/ai/ask request
    # body. Resolve and require the result to stay inside SUBJECTS_DIR, else a
    # caller could read .md/.txt outside the vault via '../' or an absolute path
    # (e.g. subject="~/.ssh" or "../../secrets").
    base = SUBJECTS_DIR.resolve()
    try:
        notes_dir = (base / subject / "notes").resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    if not notes_dir.is_relative_to(base) or not notes_dir.is_dir():
        return ""
    out = ""
    for p in sorted(notes_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in NOTE_EXTS:
            try:
                out += f"\n# {p.name}\n" + p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(out) > budget:
                break
    return out[:budget]


@router.get("/api/ai/health")
async def ai_health() -> JSONResponse:
    cfg = _llm_config()
    return JSONResponse({"configured": bool(cfg["base"] and cfg["key"]), "model": cfg["model"]})


@router.post("/api/ai/ask")
async def ai_ask(payload: dict[str, Any]) -> JSONResponse:
    import asyncio
    question = str(payload.get("question", "")).strip()
    subject = str(payload.get("subject", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question required")
    context = _subject_notes_snippet(subject) if subject else ""
    # Memory Core grounding: pull relevant prior memories (best-effort).
    mem_block = ""
    try:
        from memory import context_block
        mem_block = context_block(question, subject=subject, limit=5)
    except Exception:  # noqa: BLE001 — memory must never break the tutor
        mem_block = ""
    sys = (
        "You are a sharp, encouraging study tutor for a science student. "
        "Answer clearly and concisely, use plain language, and show your reasoning for problems. "
        "When study notes are provided, ground your answer in them and say if they do not cover it. "
        "Prior 'Relevant memory' is background on this student; use it only if helpful."
    )
    parts = []
    if context:
        parts.append(f"Study notes for {subject}:\n{context}")
    if mem_block:
        parts.append(mem_block)
    parts.append(f"Question: {question}")
    user = "\n\n---\n\n".join(parts)
    answer = await asyncio.to_thread(_llm_chat, [{"role": "system", "content": sys}, {"role": "user", "content": user}])
    # Record the turn episodically (best-effort, never blocks the response).
    try:
        from memory import record_memory
        record_memory(
            f"Q: {question}\nA: {answer}",
            kind="episodic", source="ai-tutor", actor="tutor",
            subject=subject, summary=question[:200],
            tags=("ai-tutor," + (subject or "general")),
            salience=0.55,
        )
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({
        "ok": True, "answer": answer,
        "grounded": bool(context), "memory_used": bool(mem_block),
        "model": _llm_config()["model"],
    })


@router.post("/api/ai/explain")
async def ai_explain(payload: dict[str, Any]) -> JSONResponse:
    import asyncio
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    sys = "You are a study tutor. Explain the given concept simply, then give one concrete example and one common misconception."
    answer = await asyncio.to_thread(_llm_chat, [{"role": "system", "content": sys}, {"role": "user", "content": text}], temperature=0.3)
    return JSONResponse({"ok": True, "answer": answer})
