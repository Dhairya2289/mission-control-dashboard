"""
Mission Control — Tracker router (server-backed study tracker).

A server-backed study tracker driven by ``roadmap_spec``: phase / week / day
plan generation, a 100-point daily discipline score, streak, 90-day heatmap,
and per-subject readiness. The roadmap that ships with the repo is a generic
multi-exam sample; drop a ``roadmap_private.py`` next to ``roadmap_spec.py`` to
override it at import time (gitignored — see ``.gitignore``).

Why server-backed: state lives in ``$HERMES_HOME/tracker.db``, so the Hermes
agents and the daily brief can read and write it too — the tracker is part of
the agent fabric, not a sealed client app. ``/api/tracker/summary`` is the
Hermes contract.

Daily discipline score (100):
    45 blocks + 15 MCQs + 10 water + 10 sleep + 5 specs + 5 sunlight
    + 5 liver + 5 screen-time

All day keys + phase math use LOCAL time (the user's day boundary is local).
The phase model, subjects, exam countdowns and the entire study-block plan come
from ``roadmap_spec`` — change the roadmap there, not here.
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import config
import roadmap_spec

router = APIRouter()

# Serializes the read-modify-write of a day's JSON blob. The module is written
# by both the UI and the Hermes agents; without this, two concurrent writers can
# read the same blob and the later write clobbers the earlier one. One in-process
# lock suffices — the dashboard is a single uvicorn process bound to localhost.
_WRITE_LOCK = asyncio.Lock()


def _round_half_up(x: float) -> int:
    """Match JS Math.round (round-half-up), not Python's banker's rounding, so
    daily scores stay byte-identical to the original store.ts implementation."""
    return math.floor(x + 0.5)


def _as_int(v: Any, default: int = 0) -> int:
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DASHBOARD_DIR = Path(__file__).resolve().parent
TRACKER_DB = config.TRACKER_DB

# ===========================================================================
# Roadmap model  (data-driven from roadmap_spec — the single source of truth)
# ===========================================================================
META = roadmap_spec.build_meta()
PHASES = META["phases"]                       # ordered list, each with totalDays
PHASE_ORDER = [p["key"] for p in PHASES]
PHASE_BY_KEY = {p["key"]: p for p in PHASES}

PHASE_TOTAL_DAYS = {p["key"]: p["totalDays"] for p in PHASES}
MCQ_DAILY_TARGET = {p["key"]: p["mcqTarget"] for p in PHASES}
STUDY_HOURS_TARGET = {p["key"]: p["studyHoursTarget"] for p in PHASES}
PHASE_LABEL = {p["key"]: p["label"] for p in PHASES}
PHASE_NAME = {p["key"]: p["name"] for p in PHASES}
PHASE_TARGET = {p["key"]: p["target"] for p in PHASES}
PHASE_START = {p["key"]: datetime.fromisoformat(p["start"] + "T00:00:00") for p in PHASES}
PHASE_END = {p["key"]: datetime.fromisoformat(p["end"] + "T23:59:59") for p in PHASES}

# Plan calendar bounds — used to validate date navigation / backfill.
PLAN_FIRST_DATE = date.fromisoformat(PHASES[0]["start"])
PLAN_LAST_DATE = date.fromisoformat(PHASES[-1]["end"])

SUBJECT_LABELS = {k: v["label"] for k, v in META["subjects"].items()}
SUBJECT_COLORS = {k: v["color"] for k, v in META["subjects"].items()}
TYPE_LABELS = dict(META["types"])

EXAMS = META["exams"]
HEADER_COUNTDOWNS = META["headerCountdowns"]
PRIMARY_EXAM = META["primaryExam"]

DEFAULT_SLEEP_TARGET_BEDTIME = "01:30"
DEFAULT_SLEEP_TARGET_WAKE = "09:00"


def _now() -> datetime:
    return datetime.now()  # local time


def _today_key(d: datetime | date | None = None) -> str:
    d = d or _now()
    if isinstance(d, datetime):
        d = d.date()
    return d.isoformat()


def auto_detect_phase(now: datetime | None = None) -> str:
    """The phase whose [start, end] window contains `now`. Clamps to the first
    phase before the plan starts and the last phase after it ends."""
    now = now or _now()
    for p in PHASES:
        if PHASE_START[p["key"]] <= now <= PHASE_END[p["key"]]:
            return p["key"]
    if now < PHASE_START[PHASE_ORDER[0]]:
        return PHASE_ORDER[0]
    return PHASE_ORDER[-1]


def phase_start(phase: str) -> datetime:
    return PHASE_START[phase]


def days_until(target: datetime, now: datetime | None = None) -> int:
    now = now or _now()
    diff = (target - now).total_seconds()
    return 0 if diff <= 0 else math.ceil(diff / 86400.0)


def _days_until_date(target: date, now: datetime | None = None) -> int:
    """Whole calendar days until `target` (0 if today or past)."""
    today = (now or _now()).date()
    return max(0, (target - today).days)


def plan_day_for(phase: str, when: datetime | None = None) -> int:
    """Clamp the calendar offset from phase start into [1, total]."""
    when = when or _now()
    start = phase_start(phase)
    diff = (when.date() - start.date()).days + 1
    return max(1, min(diff, PHASE_TOTAL_DAYS[phase]))


# ===========================================================================
# Plan (generated from roadmap_spec, loaded once)
# ===========================================================================
def _load_plan() -> list[dict[str, Any]]:
    raw = roadmap_spec.build_plan()
    # Block ids are unique by construction, but stay defensive: de-dupe so ids
    # remain valid keys everywhere (DB completion sets, Alpine x-for :key).
    seen: dict[str, int] = {}
    for b in raw:
        bid = b.get("id", "")
        if bid in seen:
            seen[bid] += 1
            b["id"] = f"{bid}__{seen[bid]}"
        else:
            seen[bid] = 0
    return raw


PLAN: list[dict[str, Any]] = _load_plan()
PLAN_BY_PHASE_DAY: dict[tuple[str, int], list[dict[str, Any]]] = {}
PLAN_BY_ID: dict[str, dict[str, Any]] = {}
for _b in PLAN:
    PLAN_BY_PHASE_DAY.setdefault((_b["phase"], _b["day"]), []).append(_b)
    PLAN_BY_ID[_b["id"]] = _b
for _k in PLAN_BY_PHASE_DAY:
    PLAN_BY_PHASE_DAY[_k].sort(key=lambda b: b.get("startTime", "99:99"))


def _blocks_for(phase: str, day: int) -> list[dict[str, Any]]:
    return PLAN_BY_PHASE_DAY.get((phase, day), [])


# ===========================================================================
# tracker.db
# ===========================================================================
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(TRACKER_DB), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    TRACKER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRACKER_DB), timeout=5.0)
    # TRUNCATE, not WAL: tracker.db lives on an ntfs3 mount (/mnt/storage) and is
    # also read by SHORT-LIVED cron processes (jee-consistency/tracker_bridge.py
    # in job_brief/job_shutdown/job_weekly). WAL's -shm shared-memory mmap is
    # unreliable on ntfs3 across short-lived openers and throws "disk I/O error"
    # — the exact failure that silently killed the proactive engine on
    # study_progress.db. TRUNCATE is a rollback journal (no -shm); busy_timeout
    # gives the read/write concurrency the low-write dashboard actually needs.
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracker_days (
            date TEXT PRIMARY KEY,
            phase TEXT NOT NULL,
            data TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE TABLE IF NOT EXISTS tracker_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()


_init_db()


def _meta_get(key: str, default: Any = None) -> Any:
    conn = _conn()
    row = conn.execute("SELECT value FROM tracker_meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def _meta_set(key: str, value: Any) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO tracker_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def _sleep_targets() -> dict[str, str]:
    return _meta_get("sleep_targets", {
        "currentTargetBedtime": DEFAULT_SLEEP_TARGET_BEDTIME,
        "currentTargetWakeTime": DEFAULT_SLEEP_TARGET_WAKE,
    })


def _default_log(key: str, phase: str, target_bedtime: str) -> dict[str, Any]:
    return {
        "date": key, "phase": phase, "score": 0,
        "studyBlocksCompleted": [],
        "totalStudyMinutes": 0,
        "mcqEntries": [],
        "waterGlasses": 0, "steps": 0,
        "productiveHours": 0, "wastedHours": 0,
        "specsWorn": False, "sunlightDone": False, "liverProtocolDone": False,
        "sleep": {"date": key, "sleepTime": "", "wakeTime": "", "hoursSlept": 0,
                  "targetBedtime": target_bedtime, "targetMet": False},
        "notes": "",
    }


def _read_log(key: str) -> dict[str, Any] | None:
    conn = _conn()
    row = conn.execute("SELECT data FROM tracker_days WHERE date = ?", (key,)).fetchone()
    conn.close()
    if not row:
        return None
    try:
        blob = json.loads(row["data"])
    except (ValueError, TypeError):
        return None
    if not isinstance(blob, dict):
        return None
    # Backfill missing keys (logs may be written by the Hermes agents or an
    # older schema) so every consumer sees a complete blob and never KeyErrors.
    # Stored values win; only absent keys take defaults. A phase from the old
    # plan is remapped to the current model so plan-day math stays valid.
    phase = blob.get("phase")
    if phase not in PHASE_BY_KEY:
        phase = auto_detect_phase(datetime.fromisoformat(key + "T12:00:00"))
        blob["phase"] = phase
    try:
        target_bedtime = _sleep_targets()["currentTargetBedtime"]
    except Exception:  # noqa: BLE001
        target_bedtime = ""
    skeleton = _default_log(key, phase, target_bedtime)
    skeleton.update(blob)
    if isinstance(blob.get("sleep"), dict):
        merged_sleep = dict(skeleton["sleep"]); merged_sleep.update(blob["sleep"])
        skeleton["sleep"] = merged_sleep
    return skeleton


def _get_or_create_today() -> dict[str, Any]:
    key = _today_key()
    existing = _read_log(key)
    if existing:
        return existing
    phase = _meta_get("phase_override") or auto_detect_phase()
    if phase not in PHASE_BY_KEY:
        phase = auto_detect_phase()
    log = _default_log(key, phase, _sleep_targets()["currentTargetBedtime"])
    _write_log(log)
    return log


def _write_log(log: dict[str, Any]) -> None:
    key = log["date"]
    score = _score_log(log)
    log["score"] = score
    conn = _conn()
    conn.execute(
        "INSERT INTO tracker_days (date, phase, data, score, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET phase=excluded.phase, data=excluded.data, "
        "score=excluded.score, updated_at=excluded.updated_at",
        (key, log["phase"], json.dumps(log), score, _now().isoformat()),
    )
    conn.commit()
    conn.close()


# ===========================================================================
# Day navigation / backfill  (log a day other than today — "cover up" misses)
# ===========================================================================
def _phase_for_key(key: str) -> str:
    if key == _today_key():
        p = _meta_get("phase_override") or auto_detect_phase()
    else:
        p = auto_detect_phase(datetime.fromisoformat(key + "T12:00:00"))
    return p if p in PHASE_BY_KEY else auto_detect_phase()


def _read_or_default_day(key: str) -> dict[str, Any]:
    """A day log for VIEWING — never writes a row, so browsing past days does
    not litter the DB with empty rows."""
    return _read_log(key) or _default_log(
        key, _phase_for_key(key), _sleep_targets()["currentTargetBedtime"])


def _get_or_create_day(key: str) -> dict[str, Any]:
    """A day log for WRITING — creates the row if absent, so a tick on a missed
    day is persisted under that day's own date."""
    existing = _read_log(key)
    if existing:
        return existing
    log = _default_log(key, _phase_for_key(key), _sleep_targets()["currentTargetBedtime"])
    _write_log(log)
    return log


def _view_key(date_param: Any) -> str:
    """Validate a ?date= for VIEWING — clamp into [plan start, today]."""
    if not date_param:
        return _today_key()
    try:
        d = date.fromisoformat(str(date_param))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date")
    return max(PLAN_FIRST_DATE, min(d, _now().date())).isoformat()


def _write_key(payload: dict[str, Any]) -> str:
    """Validate a payload date for WRITING — within the plan and not in the
    future (backfill the past + today; never pre-log tomorrow)."""
    raw = str(payload.get("date") or "").strip()
    if not raw:
        return _today_key()
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad date")
    if d > _now().date():
        raise HTTPException(status_code=400, detail="cannot log a future day")
    if not (PLAN_FIRST_DATE <= d <= PLAN_LAST_DATE):
        raise HTTPException(status_code=400, detail="date outside plan range")
    return d.isoformat()


def _catchup(now: datetime) -> dict[str, Any]:
    """The recent plan days (last 7, from plan start) with no logged progress —
    what the user still has to 'cover up'. Bounded so it stays actionable."""
    today = now.date()
    missed: list[str] = []
    for i in range(1, 8):
        d = today - timedelta(days=i)
        if d < PLAN_FIRST_DATE:
            break
        log = _read_log(d.isoformat())
        attended = bool(log and (log.get("score", 0) > 0 or log.get("studyBlocksCompleted")))
        if not attended:
            missed.append(d.isoformat())
    missed.sort()
    return {"missedCount": len(missed), "firstMissed": missed[0] if missed else None}


# ===========================================================================
# Scoring  (verbatim port of store.ts calculateDailyScore + breakdown)
# ===========================================================================
def _bed_minutes(t: str) -> int:
    if not t:
        return 9999
    try:
        h, m = (int(x) for x in t.split(":"))
    except (ValueError, AttributeError):
        return 9999
    mins = h * 60 + m
    return mins + 1440 if h <= 6 else mins


def _sleep_target_met(actual: str, target: str) -> bool:
    if not actual or not target:
        return False
    return _bed_minutes(actual) <= _bed_minutes(target) + 30


def _score_breakdown(log: dict[str, Any]) -> dict[str, Any]:
    phase = log["phase"] if log["phase"] in PHASE_BY_KEY else auto_detect_phase()
    log_date = datetime.fromisoformat(log["date"] + "T00:00:00")
    day_number = plan_day_for(phase, log_date)
    today_blocks = _blocks_for(phase, day_number)

    parts: dict[str, dict[str, Any]] = {}

    # Blocks — 45
    today_ids = {b["id"] for b in today_blocks}
    if today_blocks:
        done = sum(1 for bid in log["studyBlocksCompleted"] if bid in today_ids)
        blocks_pts = _round_half_up((done / len(today_blocks)) * 45)
        blocks_meta = {"done": done, "total": len(today_blocks)}
    else:
        # Rest / free day: full credit if any *real* plan block was completed.
        real_done = [bid for bid in log["studyBlocksCompleted"] if bid in PLAN_BY_ID]
        blocks_pts = 45 if real_done else 0
        blocks_meta = {"done": len(real_done), "total": len(real_done)}
    parts["blocks"] = {"points": blocks_pts, "max": 45, **blocks_meta}

    # MCQs — 15
    mcq_done = sum(_as_int(e.get("attempted", 0)) for e in log["mcqEntries"])
    mcq_target = MCQ_DAILY_TARGET[phase]
    if mcq_done >= mcq_target:
        mcq_pts = 15
    elif mcq_done > 0:
        mcq_pts = _round_half_up((mcq_done / mcq_target) * 10)
    else:
        mcq_pts = 0
    parts["mcqs"] = {"points": mcq_pts, "max": 15, "done": mcq_done, "target": mcq_target}

    # Water — 10
    water = log["waterGlasses"]
    water_pts = 10 if water >= 8 else (5 if water >= 4 else 0)
    parts["water"] = {"points": water_pts, "max": 10, "glasses": water}

    # Sleep — 10
    sleep_pts = 10 if log["sleep"].get("targetMet") else 0
    parts["sleep"] = {"points": sleep_pts, "max": 10, "met": bool(log["sleep"].get("targetMet"))}

    # Habits — 5 each
    parts["specs"] = {"points": 5 if log["specsWorn"] else 0, "max": 5, "done": log["specsWorn"]}
    parts["sunlight"] = {"points": 5 if log["sunlightDone"] else 0, "max": 5, "done": log["sunlightDone"]}
    parts["liver"] = {"points": 5 if log["liverProtocolDone"] else 0, "max": 5, "done": log["liverProtocolDone"]}

    # Screen time — 5
    screen_ok = log["productiveHours"] > log["wastedHours"] and log["productiveHours"] > 0
    parts["screen"] = {"points": 5 if screen_ok else 0, "max": 5,
                       "productive": log["productiveHours"], "wasted": log["wastedHours"]}

    total = min(100, math.floor(sum(p["points"] for p in parts.values())))
    return {"total": total, "parts": parts, "plan_day": day_number}


def _score_log(log: dict[str, Any]) -> int:
    return _score_breakdown(log)["total"]


# ===========================================================================
# Streak / heatmap / weekly / subjects
# ===========================================================================
def _all_day_scores() -> dict[str, int]:
    conn = _conn()
    rows = conn.execute("SELECT date, score FROM tracker_days").fetchall()
    conn.close()
    return {r["date"]: r["score"] for r in rows}


def _studied_days() -> set[str]:
    """Date keys on which there was real *study* — at least one plan block
    ticked or some MCQs logged. Habits alone (water/sleep) do NOT count, so the
    streak means 'I studied', not 'I drank water'."""
    conn = _conn()
    rows = conn.execute("SELECT date, data FROM tracker_days").fetchall()
    conn.close()
    out: set[str] = set()
    for r in rows:
        try:
            log = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        mcqs = sum(_as_int(e.get("attempted", 0)) for e in log.get("mcqEntries", []))
        if log.get("studyBlocksCompleted") or mcqs > 0:
            out.add(r["date"])
    return out


def _streak() -> int:
    """Consecutive study days up to & including today (a day counts when a plan
    block was ticked or MCQs were logged — see _studied_days)."""
    studied = _studied_days()
    today = _now().date()
    current = 0
    for i in range(1, 366):
        k = (today - timedelta(days=i)).isoformat()
        if k in studied:
            current += 1
        else:
            break
    if today.isoformat() in studied:
        current += 1
    return current


def _heatmap(days: int = 90) -> list[dict[str, Any]]:
    scores = _all_day_scores()
    today = _now().date()
    out: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        k = d.isoformat()
        out.append({"date": k, "score": scores.get(k, 0), "weekday": d.weekday()})
    return out


def _weekly_scores() -> list[dict[str, Any]]:
    scores = _all_day_scores()
    today = _now().date()
    out: list[dict[str, Any]] = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        k = d.isoformat()
        out.append({"date": k, "weekday": d.strftime("%a"), "score": scores.get(k, 0)})
    return out


def _subject_progress() -> list[dict[str, Any]]:
    total: dict[str, int] = {}
    for b in PLAN:
        total[b["subject"]] = total.get(b["subject"], 0) + 1
    done_ids: dict[str, set] = {}
    conn = _conn()
    for r in conn.execute("SELECT data FROM tracker_days").fetchall():
        try:
            log = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        for bid in log.get("studyBlocksCompleted", []):
            blk = PLAN_BY_ID.get(bid)
            if blk:
                done_ids.setdefault(blk["subject"], set()).add(bid)
    conn.close()
    out = []
    for subj in sorted(total, key=lambda s: -total[s]):
        t = total[subj]
        d = len(done_ids.get(subj, set()))
        out.append({
            "subject": subj, "label": SUBJECT_LABELS.get(subj, subj),
            "color": SUBJECT_COLORS.get(subj, "#818cf8"),
            "total": t, "done": d,
            "percent": round((d / t) * 100) if t else 0,
        })
    return out


# ===========================================================================
# Serialization helpers
# ===========================================================================
def _decorate_block(b: dict[str, Any], completed_ids: set) -> dict[str, Any]:
    return {
        "id": b["id"], "subject": b["subject"],
        "subjectLabel": SUBJECT_LABELS.get(b["subject"], b["subject"]),
        "color": SUBJECT_COLORS.get(b["subject"], "#818cf8"),
        "chapter": b.get("chapter", ""), "topic": b.get("topic", ""),
        "type": b.get("type", ""), "typeLabel": TYPE_LABELS.get(b.get("type", ""), b.get("type", "")),
        "durationMinutes": b.get("durationMinutes", 0),
        "startTime": b.get("startTime", ""),
        "resources": b.get("resources", []),
        "ncertPages": b.get("ncertPages"),
        "done": b["id"] in completed_ids,
    }


def _phase_block(phase: str, now: datetime) -> dict[str, Any]:
    start = phase_start(phase)
    total = PHASE_TOTAL_DAYS[phase]
    day = plan_day_for(phase, now)
    return {
        "key": phase, "label": PHASE_LABEL[phase], "name": PHASE_NAME[phase],
        "target": PHASE_TARGET[phase],
        "day": day, "totalDays": total,
        "percentElapsed": round((day / total) * 100) if total else 0,
        "mcqTarget": MCQ_DAILY_TARGET[phase],
        "studyHoursTarget": STUDY_HOURS_TARGET[phase],
        "startDate": start.date().isoformat(),
        "color": PHASE_BY_KEY[phase].get("color", "#818cf8"),
    }


def _exam_countdowns(now: datetime) -> list[dict[str, Any]]:
    """All exams with day counts, soonest meaningful first."""
    out = []
    for e in EXAMS:
        d = date.fromisoformat(e["date"])
        out.append({
            **e, "days": _days_until_date(d, now),
            "passed": d < now.date(), "primary": e.get("key") == PRIMARY_EXAM,
        })
    return out


def _countdowns(now: datetime) -> list[dict[str, Any]]:
    """The header countdowns (configured in roadmap_spec.HEADER_COUNTDOWNS)."""
    by_key = {e["key"]: e for e in _exam_countdowns(now)}
    return [by_key[k] for k in HEADER_COUNTDOWNS if k in by_key]


def _study_now(log: dict[str, Any], phase: str, now: datetime) -> dict[str, Any]:
    if not _meta_get("doctor_visit_done", False):
        return {"status": "blocked", "message": "Commitment lock active. Tap to commit to the year."}
    day = plan_day_for(phase, now)
    today_blocks = _blocks_for(phase, day)
    completed = set(log["studyBlocksCompleted"])
    pending = [b for b in today_blocks if b["id"] not in completed]
    if not today_blocks:
        return {"status": "rest", "message": "No scheduled blocks today — free study / PYQ sprint."}
    if not pending:
        return {"status": "allDone", "message": "All blocks done. PYQ sprint now."}
    nxt = pending[0]
    return {
        "status": "pending",
        "block": _decorate_block(nxt, completed),
        "message": f"{nxt.get('chapter','')} — {nxt.get('topic','')}",
    }


# ===========================================================================
# Endpoints
# ===========================================================================
@router.get("/api/tracker/state")
async def tracker_state(date: str | None = None) -> JSONResponse:
    """Everything the daily view needs in one call. `date` (optional, YYYY-MM-DD)
    selects a past plan day for catch-up; default is today."""
    now = _now()
    key = _view_key(date)
    is_today = key == _today_key()
    when = datetime.fromisoformat(key + "T12:00:00")
    log = _get_or_create_day(key) if is_today else _read_or_default_day(key)
    phase = log["phase"]
    breakdown = _score_breakdown(log)
    completed = set(log["studyBlocksCompleted"])
    day = breakdown["plan_day"]
    blocks = [_decorate_block(b, completed) for b in _blocks_for(phase, day)]
    streak = _streak()
    return JSONResponse({
        "today": log,
        "phase": _phase_block(phase, when),
        "countdowns": _countdowns(now),
        "score": breakdown,
        "blocks": blocks,
        "studyNow": _study_now(log, phase, now) if is_today else {
            "status": "past",
            "message": "Catch-up — tick the blocks you've completed for this day.",
        },
        "streak": streak,
        "longestStreak": max(_meta_get("longest_streak", 0), streak),
        "doctorVisitDone": bool(_meta_get("doctor_visit_done", False)),
        "sleepTarget": _sleep_targets(),
        "weeklyScores": _weekly_scores(),
        "subjects": SUBJECT_LABELS,
        "mission": META["meta"]["mission"],
        "viewDate": key,
        "isToday": is_today,
        "planStart": PLAN_FIRST_DATE.isoformat(),
        "catchup": _catchup(now),
        "today_date": _today_key(),
    })


def _persist_longest_streak() -> None:
    _meta_set("longest_streak", max(_meta_get("longest_streak", 0), _streak()))


@router.post("/api/tracker/block/toggle")
async def tracker_block_toggle(payload: dict[str, Any]) -> JSONResponse:
    block_id = str(payload.get("block_id", "")).strip()
    if not block_id:
        raise HTTPException(status_code=400, detail="block_id required")
    key = _write_key(payload)
    async with _WRITE_LOCK:
        log = _get_or_create_day(key)
        completed = list(log["studyBlocksCompleted"])
        blk = PLAN_BY_ID.get(block_id)
        dur = blk.get("durationMinutes", 0) if blk else 0
        if block_id in completed:
            completed.remove(block_id)
            log["totalStudyMinutes"] = max(0, log["totalStudyMinutes"] - dur)
        else:
            completed.append(block_id)
            log["totalStudyMinutes"] = log["totalStudyMinutes"] + dur
        log["studyBlocksCompleted"] = completed
        _write_log(log)
        _persist_longest_streak()
        return JSONResponse({"ok": True, "score": _score_breakdown(log), "completed": completed})


@router.post("/api/tracker/mcq")
async def tracker_mcq(payload: dict[str, Any]) -> JSONResponse:
    attempted = _as_int(payload.get("attempted", 0))
    correct = _as_int(payload.get("correct", 0))
    subject = str(payload.get("subject", "")).strip()
    if attempted <= 0:
        raise HTTPException(status_code=400, detail="attempted must be > 0")
    correct = max(0, min(correct, attempted))
    key = _write_key(payload)
    async with _WRITE_LOCK:
        log = _get_or_create_day(key)
        entry = {"id": _now().strftime("%H%M%S%f")[:9], "subject": subject,
                 "attempted": attempted, "correct": correct, "ts": _now().isoformat()}
        log["mcqEntries"].append(entry)
        _write_log(log)
        _persist_longest_streak()
        return JSONResponse({"ok": True, "score": _score_breakdown(log), "entry": entry,
                             "mcqEntries": log["mcqEntries"]})


@router.post("/api/tracker/log")
async def tracker_log(payload: dict[str, Any]) -> JSONResponse:
    """Generic partial update for scalar habit fields. Recomputes score.
    All numeric coercions are defensive — bad input becomes a 400, never a 500."""
    field = str(payload.get("field", "")).strip()
    val = payload.get("value")
    valid = {"water", "steps", "specs", "sunlight", "liver", "notes", "screen", "sleep"}
    if field not in valid:
        raise HTTPException(status_code=400, detail=f"unknown field: {field}")

    key = _write_key(payload)
    async with _WRITE_LOCK:
        log = _get_or_create_day(key)
        if field == "water":
            log["waterGlasses"] = max(0, _as_int(val))
        elif field == "steps":
            log["steps"] = max(0, _as_int(val))
        elif field == "specs":
            log["specsWorn"] = bool(val)
        elif field == "sunlight":
            log["sunlightDone"] = bool(val)
        elif field == "liver":
            log["liverProtocolDone"] = bool(val)
        elif field == "notes":
            log["notes"] = str(val or "")
        elif field == "screen":
            log["productiveHours"] = max(0.0, _as_float(payload.get("productive", log["productiveHours"])))
            log["wastedHours"] = max(0.0, _as_float(payload.get("wasted", log["wastedHours"])))
        elif field == "sleep":
            sleep_time = str(payload.get("sleepTime", "")).strip()
            wake_time = str(payload.get("wakeTime", "")).strip()
            target = log["sleep"].get("targetBedtime") or _sleep_targets()["currentTargetBedtime"]

            def _to_min(t: str) -> int:
                if not t or ":" not in t:
                    return 0
                try:
                    h, m = (int(x) for x in t.split(":")[:2])
                except ValueError:
                    return 0
                return h * 60 + m
            s_min, w_min = _to_min(sleep_time), _to_min(wake_time)
            if s_min > w_min:
                w_min += 1440
            hours = round((w_min - s_min) / 60 * 10) / 10 if (sleep_time and wake_time) else 0
            log["sleep"] = {"date": log["date"], "sleepTime": sleep_time, "wakeTime": wake_time,
                            "hoursSlept": hours, "targetBedtime": target,
                            "targetMet": _sleep_target_met(sleep_time, target)}

        _write_log(log)
        _persist_longest_streak()
        return JSONResponse({"ok": True, "score": _score_breakdown(log), "today": log})


@router.post("/api/tracker/doctor")
async def tracker_doctor() -> JSONResponse:
    """Commitment lock — tapped once to commit to the year (kept endpoint name
    for backward compatibility with the original store)."""
    _meta_set("doctor_visit_done", True)
    _meta_set("doctor_visit_date", _now().isoformat())
    return JSONResponse({"ok": True})


@router.post("/api/tracker/phase")
async def tracker_phase_override(payload: dict[str, Any]) -> JSONResponse:
    phase = payload.get("phase")
    if phase not in (None, *PHASE_ORDER):
        raise HTTPException(status_code=400, detail="invalid phase")
    _meta_set("phase_override", phase)
    return JSONResponse({"ok": True, "phase": phase or auto_detect_phase()})


@router.get("/api/tracker/roadmap")
async def tracker_roadmap(phase: str | None = None) -> JSONResponse:
    now = _now()
    active_phase = _meta_get("phase_override") or auto_detect_phase()
    if active_phase not in PHASE_BY_KEY:
        active_phase = auto_detect_phase()
    want = phase or active_phase
    if want not in PHASE_TOTAL_DAYS:
        raise HTTPException(status_code=400, detail="invalid phase")
    completed_ids: set = set()
    conn = _conn()
    for r in conn.execute("SELECT data FROM tracker_days").fetchall():
        try:
            completed_ids |= set(json.loads(r["data"]).get("studyBlocksCompleted", []))
        except (ValueError, TypeError):
            pass
    conn.close()
    current_day = plan_day_for(want, now) if want == active_phase else 0
    days = []
    day_nums = sorted({d for (p, d) in PLAN_BY_PHASE_DAY if p == want})
    for dn in day_nums:
        blocks = [_decorate_block(b, completed_ids) for b in _blocks_for(want, dn)]
        done = sum(1 for b in blocks if b["done"])
        # The calendar date for this plan-day (blocks carry an ISO date).
        day_date = (blocks[0].get("date") if blocks else None)
        days.append({
            "day": dn, "date": day_date, "blocks": blocks, "done": done, "total": len(blocks),
            "isToday": dn == current_day,
            "subjects": sorted({b["subject"] for b in blocks}),
            "minutes": sum(b["durationMinutes"] for b in blocks),
        })
    return JSONResponse({
        "phase": _phase_block(want, now),
        "activePhase": active_phase,
        "days": days,
        "phases": [{"key": p["key"], "label": p["label"], "name": p["name"],
                    "color": p.get("color", "#818cf8"), "totalDays": p["totalDays"],
                    "start": p["start"], "end": p["end"]} for p in PHASES],
    })


@router.get("/api/tracker/meta")
async def tracker_meta() -> JSONResponse:
    """Strategic roadmap layer for the Roadmap/Stats views and Hermes: phases,
    full exam calendar with live countdowns, tests, alerts, KPIs, priority
    ladder, batch chapter windows, subjects."""
    now = _now()
    return JSONResponse({
        "title": META["meta"]["title"],
        "subtitle": META["meta"]["subtitle"],
        "mission": META["meta"]["mission"],
        "oneLine": META["meta"]["oneLine"],
        "phases": [_phase_block(p["key"], now) | {"start": p["start"], "end": p["end"]} for p in PHASES],
        "activePhase": _meta_get("phase_override") or auto_detect_phase(now),
        "exams": _exam_countdowns(now),
        "tests": META["tests"],
        "alerts": META["alerts"],
        "kpis": META["kpis"],
        "priorityLadder": META["priorityLadder"],
        "batchWindows": META["batchWindows"],
        "subjects": META["subjects"],
        "habits": META["habits"],
    })


@router.get("/api/tracker/stats")
async def tracker_stats() -> JSONResponse:
    scores = _all_day_scores()
    streak = _streak()
    conn = _conn()
    rows = conn.execute("SELECT date, data FROM tracker_days ORDER BY date DESC LIMIT 30").fetchall()
    conn.close()
    mcq_total = mcq_correct = 0
    active_days = 0
    total_minutes = 0
    for r in rows:
        try:
            log = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        if scores.get(r["date"], 0) > 0:
            active_days += 1
        total_minutes += log.get("totalStudyMinutes", 0)
        for e in log.get("mcqEntries", []):
            mcq_total += int(e.get("attempted", 0))
            mcq_correct += int(e.get("correct", 0))
    avg_score = round(sum(scores.values()) / len(scores)) if scores else 0
    return JSONResponse({
        "streak": streak,
        "longestStreak": max(_meta_get("longest_streak", 0), streak),
        "avgScore": avg_score,
        "activeDays": active_days,
        "totalStudyHours": round(total_minutes / 60, 1),
        "mcqAccuracy": round((mcq_correct / mcq_total) * 100, 1) if mcq_total else None,
        "mcqTotal": mcq_total,
        "heatmap": _heatmap(364),
        "weekly": _weekly_scores(),
        "subjects": _subject_progress(),
    })


@router.get("/api/tracker/summary")
async def tracker_summary() -> JSONResponse:
    """Compact summary for the Hermes daily brief / other agents to consume.
    This is the integration contract — keep keys stable."""
    now = _now()
    log = _get_or_create_today()
    phase = log["phase"]
    bd = _score_breakdown(log)
    completed = set(log["studyBlocksCompleted"])
    blocks = _blocks_for(phase, bd["plan_day"])
    pending = [_decorate_block(b, completed) for b in blocks if b["id"] not in completed]
    exams = _exam_countdowns(now)
    by_key = {e["key"]: e for e in exams}
    # Active batch chapters today, per subject (what the batch is on right now).
    active_batch = {}
    for b in blocks:
        if b.get("type") == "batch" and b["subject"] in ("phy", "chem", "math"):
            active_batch[b["subject"]] = b.get("chapter", "")
    next_exam = next((e for e in sorted(exams, key=lambda e: e["days"]) if not e["passed"]), None)
    return JSONResponse({
        "date": _today_key(),
        "phase": PHASE_LABEL[phase],
        "phaseName": PHASE_NAME[phase],
        "planDay": bd["plan_day"],
        "planTotalDays": PHASE_TOTAL_DAYS[phase],
        "score": bd["total"],
        "blocksDone": bd["parts"]["blocks"]["done"],
        "blocksTotal": bd["parts"]["blocks"]["total"],
        "pendingBlocks": [{"subject": b["subject"], "topic": b["topic"],
                           "type": b["type"], "startTime": b["startTime"]} for b in pending],
        "mcqsDone": bd["parts"]["mcqs"]["done"],
        "mcqTarget": bd["parts"]["mcqs"]["target"],
        "streak": _streak(),
        "activeBatch": active_batch,
        "daysToPrimary": by_key.get(PRIMARY_EXAM, {}).get("days"),
        "primaryExam": by_key.get(PRIMARY_EXAM, {}).get("name"),
        "nextExam": ({"name": next_exam["name"], "days": next_exam["days"],
                      "date": next_exam["date"]} if next_exam else None),
        "committed": bool(_meta_get("doctor_visit_done", False)),
    })
