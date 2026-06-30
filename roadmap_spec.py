"""
Roadmap — the single machine-readable source of truth for the tracker.

This module defines a study/exam-prep roadmap as plain Python data and expands
it into the flat block list the tracker engine consumes. What ships in this repo
is a small, fictional **sample** roadmap so the tracker renders something sensible
out of the box and the data model is self-documenting.

To use your own plan, drop a `roadmap_private.py` next to this file that redefines
the same constants (`META`, `PHASES`, `BATCH_WINDOWS`, …). It is gitignored and,
when present, transparently overrides the sample below — change the roadmap there,
not here. Set `ROADMAP_SAMPLE=1` in the environment to force the bundled sample
even when a private plan exists.

Two consumers read this module:
  • `tracker.py` — the dashboard tracker engine (phases, countdowns, subjects,
                   and the generated study-block plan), via `build_meta()` /
                   `build_plan()`.
  • Hermes      — the proactive daily brief, via the emitted JSON
                  (`roadmap_meta.json`) or the `/api/tracker/summary` endpoint,
                  so the morning message is roadmap-aware.

Design: the roadmap is *day-precise* only for an opening "kill zone" sprint
(explicit day-by-day blocks). After that it is week/month-level, anchored to
per-subject chapter windows (`BATCH_WINDOWS`), a test calendar (`TESTS`), and
per-phase daily templates (`PHASE_TASKS`). So `build_plan()` generates:
  · explicit day blocks for the kill zone, and
  · template + window-injection blocks for every later day (the chapter windows
    active *that* date, plus practice / revision / mocks / admin).

Run directly to (re)emit the artifacts the rest of the system loads:
    python3 roadmap_spec.py
writes  tracker_plan.json  ·  roadmap_meta.json  ·  roadmap_spec.json
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).resolve().parent


def _d(s: str) -> date:
    return date.fromisoformat(s)


# ===========================================================================
# SAMPLE ROADMAP (fictional — overridden by roadmap_private.py when present)
# ===========================================================================
# Everything from here down to the override hook is illustrative placeholder
# data. It exercises every code path in build_plan() so a fresh clone renders a
# complete, believable tracker without any private files.

# --- META --------------------------------------------------------------------
META = {
    "title": "Exam Prep 2027 — Sample Roadmap",
    "subtitle": "Physics · Chemistry · Mathematics — a worked example plan",
    "started": "2026-07-01",
    "mission": ("A fictional sample roadmap that demonstrates the tracker's data "
                "model end to end. Replace it with your own plan in "
                "roadmap_private.py."),
    "oneLine": "Kickoff sprint → build phase → PYQ sprint → finals.",
    "source": "roadmap.sample.json",
}

# --- SUBJECTS (calm palette) -------------------------------------------------
# Keys are referenced by the plan generator; keep phy/chem/math/review/mock/admin.
SUBJECTS: dict[str, dict[str, str]] = {
    "phy":    {"label": "Physics",          "color": "#fbbf24"},
    "chem":   {"label": "Chemistry",        "color": "#38bdf8"},
    "math":   {"label": "Mathematics",      "color": "#c084fc"},
    "bio":    {"label": "Biology",          "color": "#34d399"},
    "review": {"label": "Drill / Revision", "color": "#f472b6"},
    "mock":   {"label": "Mocks",            "color": "#fb7185"},
    "admin":  {"label": "Admin / Exams",    "color": "#94a3b8"},
}

TYPES: dict[str, str] = {
    "batch":    "Batch Lecture",
    "oneshot":  "One Shot",
    "ncert":    "Textbook",
    "dpp":      "Daily Practice",
    "pyq":      "PYQ Drill",
    "revision": "Revision",
    "formula":  "Formula Sheet",
    "test":     "Mock / Test",
    "bio":      "Bio Sprint",
    "admin":    "Admin",
}

# --- PHASES (totalDays computed from start/end) ------------------------------
# `batch` = chapter recordings run in this phase. `pace` = recordings/day target.
PHASES: list[dict[str, Any]] = [
    {
        "key": "killzone", "label": "KICKOFF", "name": "Kickoff Sprint",
        "target": "Build momentum — clear foundations + start the lecture series",
        "start": "2026-07-01", "end": "2026-07-10",
        "batch": True, "pace": 3, "mcqTarget": 15, "studyHoursTarget": 6,
        "color": "#f43f5e",
    },
    {
        "key": "build", "label": "BUILD", "name": "Build Phase",
        "target": "Close the lecture backlog — 4 recordings/day",
        "start": "2026-07-11", "end": "2026-07-24",
        "batch": True, "pace": 4, "mcqTarget": 25, "studyHoursTarget": 7,
        "color": "#fb923c",
    },
    {
        "key": "sprint", "label": "PYQ SPRINT", "name": "PYQ Sprint",
        "target": "Past-paper drill on the weakest topics + benchmark mocks",
        "start": "2026-07-25", "end": "2026-08-03",
        "batch": False, "pace": 0, "mcqTarget": 40, "studyHoursTarget": 8,
        "color": "#eab308",
    },
    {
        "key": "finals", "label": "FINALS", "name": "Finals",
        "target": "Zero new content · full mocks → analysis · peak revision",
        "start": "2026-08-04", "end": "2026-08-14",
        "batch": False, "pace": 0, "mcqTarget": 60, "studyHoursTarget": 8,
        "color": "#22c55e",
    },
]

# --- EXAM CALENDAR -----------------------------------------------------------
# `primary` marks the real target exam used for the main countdown.
EXAMS: list[dict[str, Any]] = [
    {"key": "midterm", "name": "Mock Test A", "date": "2026-07-20",
     "kind": "test", "urgent": True, "note": "First benchmark"},
    {"key": "reg", "name": "Exam Registration", "date": "2026-07-31",
     "kind": "deadline", "urgent": True, "note": "Register before the deadline"},
    {"key": "final", "name": "Final Exam", "date": "2026-08-15",
     "kind": "exam", "primary": True, "target": "Top percentile", "note": "REAL EXAM"},
]
PRIMARY_EXAM = "final"
# Countdowns surfaced on the Today header (in order).
HEADER_COUNTDOWNS = ["midterm", "final"]

# --- KICKOFF KILL ZONE (verbatim day-by-day) --------------------------------
# morning = the chapter recordings (watched in backlog order, "Subject: ..." so
# the plan generator can map each line to a subject); evening = the focused work.
KILLZONE_DAYS: list[dict[str, Any]] = [
    {"date": "2026-07-01", "morning": ["Math: Foundations L1", "PChem: Basics L1", "Phy: Units L1"],
     "evening": "Foundations one-shot + 20 warm-up problems", "etype": "oneshot", "emin": 180},
    {"date": "2026-07-02", "morning": ["Math: Foundations L2", "PChem: Basics L2", "Phy: Units L2"],
     "evening": "Topic 1 one-shot (part 1) + textbook read", "etype": "oneshot", "emin": 200},
    {"date": "2026-07-03", "morning": ["Math: Foundations L3", "PChem: Basics L3", "Phy: Units L3"],
     "evening": "Topic 1 one-shot (finish) + textbook exercises", "etype": "oneshot", "emin": 200},
    {"date": "2026-07-04", "morning": ["Math: Foundations L4", "PChem: Basics L4", "Phy: Units L4"],
     "evening": "Topic 2 one-shot + notes", "etype": "oneshot", "emin": 190},
    {"date": "2026-07-05", "morning": ["Math: Foundations L5", "PChem: Basics L5", "Phy: Units L5"],
     "evening": "PYQ drill: foundations + topic 1 (30 Qs)", "etype": "pyq", "emin": 150},
    {"date": "2026-07-06", "morning": ["Math: Foundations L6", "PChem: Basics L6", "Phy: Units L6"],
     "evening": "Buffer / review — error log top 3", "etype": "revision", "emin": 120},
]
# Days after the listed kill-zone days fall into the "exam window" branch.
KILLZONE_EXAM_NOTE = "Buffer window — recordings continue (step up to 4/day)"

# --- CHAPTER WINDOWS ---------------------------------------------------------
# One stream per subject per date drives the daily "recordings" blocks after the
# kill zone. tier 1 = maximum depth; gold = high-yield emphasis window.
BATCH_WINDOWS: dict[str, list[dict[str, Any]]] = {
    "phy": [
        {"chapter": "Kinematics", "start": "2026-07-07", "end": "2026-07-18"},
        {"chapter": "Newton's Laws", "start": "2026-07-19", "end": "2026-07-24", "tier": 1, "gold": True},
    ],
    "math": [
        {"chapter": "Algebra", "start": "2026-07-07", "end": "2026-07-18"},
        {"chapter": "Calculus I", "start": "2026-07-19", "end": "2026-07-24", "tier": 1},
    ],
    "chem": [
        {"chapter": "Atomic Structure", "start": "2026-07-07", "end": "2026-07-18"},
        {"chapter": "Chemical Bonding", "start": "2026-07-19", "end": "2026-07-24", "tier": 1, "note": "Tier 1 — max depth"},
    ],
}

# --- TEST / MOCK CALENDAR ----------------------------------------------------
TESTS: list[dict[str, Any]] = [
    {"name": "Mock Test A", "date": "2026-07-20", "target": "50–70", "note": "First benchmark"},
    {"name": "Mock Test B", "date": "2026-08-05", "target": "60–80", "note": "Pre-finals"},
]

# --- ADMIN / ELIGIBILITY MILESTONES -----------------------------------------
ADMIN_TASKS: list[dict[str, Any]] = [
    {"date": "2026-07-31", "topic": "📌 Register for the exam before the deadline"},
    {"date": "2026-08-02", "topic": "📞 Confirm exam center + admit-card details"},
]

# --- Per-phase NON-batch daily tasks ----------------------------------------
# Recurring evening/discipline blocks. Recordings are generated from BATCH_WINDOWS.
PHASE_TASKS: dict[str, list[dict[str, Any]]] = {
    "build": [
        {"subject": "review", "type": "dpp", "topic": "Daily practice problems on the current chapter (10+ per sub-topic)", "durationMinutes": 90, "startTime": "15:00"},
        {"subject": "admin", "type": "revision", "topic": "Error log + formula sheet + spaced-repetition due cards", "durationMinutes": 30, "startTime": "21:30"},
    ],
    "sprint": [
        {"subject": "review", "type": "pyq", "topic": "Past-paper drill on the weakest topics (timed)", "durationMinutes": 120, "startTime": "10:00"},
        {"subject": "mock", "type": "test", "topic": "Benchmark mock on test days + full analysis", "durationMinutes": 120, "startTime": "14:00"},
    ],
    "finals": [
        {"subject": "mock", "type": "test", "topic": "Full-length mock (timed strictly)", "durationMinutes": 180, "startTime": "09:00"},
        {"subject": "review", "type": "revision", "topic": "Mock analysis → error log top 3 → targeted PYQs", "durationMinutes": 90, "startTime": "13:00"},
        {"subject": "review", "type": "formula", "topic": "Formula sheet full read + flashcards", "durationMinutes": 45, "startTime": "20:00"},
    ],
}

# Night ritual present in every phase (daily shutdown).
NIGHT_RITUAL = {"subject": "admin", "type": "revision",
                "topic": "Formula sheet (15 min) + Daily Shutdown: wins · mistake · tomorrow's first task",
                "durationMinutes": 30, "startTime": "23:00"}

# --- CRITICAL ALERTS · WEEKLY KPIs · PRIORITY LADDER ------------------------
ALERTS: list[dict[str, str]] = [
    {"level": "red", "text": "Exam registration closes Jul 31 — register now."},
    {"level": "red", "text": "Pace: step up to 4 recordings/day in the Build phase. Non-negotiable."},
    {"level": "amber", "text": "Newton's Laws (Jul 19–24) is a Tier-1 window — 10+ PYQs the same week."},
    {"level": "amber", "text": "Chemical Bonding (Jul 19–24) = Tier 1. Don't coast."},
    {"level": "yellow", "text": "Confirm exam center + admit card details by Aug 2."},
]

KPIS: list[dict[str, str]] = [
    {"metric": "Lectures completed / week", "target": "18+ (3/day) · 24+ (4/day in Build)"},
    {"metric": "Practice questions / lecture", "target": "20–30"},
    {"metric": "PYQ questions / week", "target": "50+"},
    {"metric": "Error-log entries reviewed", "target": "All due this week"},
    {"metric": "Mock accuracy", "target": ">70% on attempted"},
]

PRIORITY_LADDER: list[str] = [
    "1 · Chapter recordings (never sacrifice)",
    "2 · Daily question solving (practice after every lecture)",
    "3 · Revision (spaced-repetition schedule)",
    "4 · Mocks + analysis (per test calendar)",
    "5 · Admin / registration deadlines",
]

# Daily discipline habits (scoring weights mirror tracker.py).
HABITS: list[dict[str, Any]] = [
    {"key": "water", "label": "Water 3–4 L (8 glasses)", "max": 10},
    {"key": "sleep", "label": "Sleep 7–8 h", "max": 10},
    {"key": "sunlight", "label": "Sunlight / 20–30 min walk", "max": 5},
    {"key": "focus", "label": "Deep-work block hit", "max": 5},
    {"key": "exercise", "label": "Exercise / movement", "max": 5},
    {"key": "screen", "label": "Productive > wasted screen time", "max": 5},
]

# --- Generator framing labels ------------------------------------------------
# Cosmetic strings build_plan() wraps around the data (lecture-source tag, the
# verb for a recording, resource lists for the kill zone). Kept as overridable
# data so a private roadmap can name its own course/material without touching
# the generator.
LABELS: dict[str, Any] = {
    "lecture_source": "Lecture series",                  # resource tag on recordings
    "rec_verb": "Chapter recording",                     # verb for a recording block
    "gold_tag": " ⚡HIGH YIELD",                          # suffix on high-yield windows
    "oneshot_resources": ["One Shot", "Textbook", "PYQs"],
    "kz_chapter": "Lecture series — backlog catch-up",   # kill-zone morning chapter
    "kz_evening_chapter": "Focused work",                # kill-zone evening chapter
    "kz_4day_chapter": "Lecture series — 4/day",         # post-kill-zone buffer chapter
    "kz_4day_topic": "Chapter recording @1.5x (step up to 4/day)",
    "kz_exam_chapter": "Buffer",                         # buffer-window admin chapter
    "catchup_topic": "4th recording — close the backlog",
}


# ===========================================================================
# Private override
# ===========================================================================
# If a real plan exists in roadmap_private.py (gitignored), load it over the
# sample above. Set ROADMAP_SAMPLE=1 to force the bundled sample regardless.
if os.getenv("ROADMAP_SAMPLE") != "1":
    try:
        from roadmap_private import *  # noqa: F401,F403  (real plan, gitignored)
    except ImportError:
        pass


# ===========================================================================
# Helpers
# ===========================================================================
def phase_total_days(p: dict[str, Any]) -> int:
    return (_d(p["end"]) - _d(p["start"])).days + 1


def _active_window(subject: str, on: date) -> dict[str, Any] | None:
    """The chapter window covering `on` for `subject` (prefer Tier 1)."""
    hits = [w for w in BATCH_WINDOWS.get(subject, [])
            if _d(w["start"]) <= on <= _d(w["end"])]
    if not hits:
        return None
    hits.sort(key=lambda w: (0 if w.get("tier") == 1 else 1, _d(w["end"])))
    return hits[0]


def _tests_on(on: date) -> list[dict[str, Any]]:
    return [t for t in TESTS if _d(t["date"]) == on]


def _admin_on(on: date) -> list[dict[str, Any]]:
    return [a for a in ADMIN_TASKS if _d(a["date"]) == on]


# ===========================================================================
# build_plan()  — expand the spec into the flat block list the engine loads.
# ===========================================================================
def build_plan() -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    kz_by_date = {k["date"]: k for k in KILLZONE_DAYS}

    for p in PHASES:
        key, start, end = p["key"], _d(p["start"]), _d(p["end"])
        day = 0
        cur = start
        while cur <= end:
            day += 1
            iso = cur.isoformat()
            b_idx = 0

            def add(subject: str, chapter: str, topic: str, btype: str,
                    minutes: int, start_time: str, resources: list[str] | None = None) -> None:
                nonlocal b_idx
                b_idx += 1
                blocks.append({
                    "id": f"{key}-d{day:02d}-b{b_idx}",
                    "phase": key, "day": day, "date": iso,
                    "subject": subject, "chapter": chapter, "topic": topic,
                    "type": btype, "durationMinutes": minutes,
                    "startTime": start_time, "resources": resources or [],
                    "ncertPages": None,
                })

            if key == "killzone" and iso in kz_by_date:
                # Verbatim day-by-day kill-zone block.
                kz = kz_by_date[iso]
                subj_of = {"Math": "math", "PChem": "chem", "Phy": "phy"}
                t = 9
                for rec in kz["morning"]:
                    head = rec.split(":")[0].strip()
                    add(subj_of.get(head, "review"), LABELS["kz_chapter"],
                        f'{LABELS["rec_verb"]} — {rec} @1.5x', "batch", 60, f"{t:02d}:00",
                        [LABELS["lecture_source"]])
                    t += 1
                add("chem", LABELS["kz_evening_chapter"], kz["evening"], kz["etype"],
                    kz["emin"], "18:00", list(LABELS["oneshot_resources"]))
            elif key == "killzone":
                # Buffer window after the explicit days.
                for i, subj in enumerate(("math", "phy", "chem", "review")):
                    add(subj, LABELS["kz_4day_chapter"], LABELS["kz_4day_topic"],
                        "batch", 60, f"{9 + i:02d}:00", [LABELS["lecture_source"]])
                add("admin", LABELS["kz_exam_chapter"], KILLZONE_EXAM_NOTE, "test", 180, "14:00")
            else:
                # Recordings = active chapter windows that date.
                if p["batch"]:
                    t = 9
                    for subj in ("math", "phy", "chem"):
                        w = _active_window(subj, cur)
                        if w:
                            tier = " · Tier 1" if w.get("tier") == 1 else ""
                            gold = LABELS["gold_tag"] if w.get("gold") else ""
                            add(subj, w["chapter"],
                                f'{LABELS["rec_verb"]} @1.5x — {w["chapter"]}{tier}{gold}',
                                "batch", 70, f"{t:02d}:00", [LABELS["lecture_source"]])
                            t += 1
                    if p.get("pace", 0) >= 4:
                        add("review", "Catch-up lag", LABELS["catchup_topic"],
                            "batch", 70, f"{t:02d}:00", [LABELS["lecture_source"]])
                # Phase-specific recurring tasks.
                for tk in PHASE_TASKS.get(key, []):
                    add(tk["subject"], tk.get("chapter", p["name"]), tk["topic"],
                        tk["type"], tk["durationMinutes"], tk["startTime"])

            # Injections (every phase): tests + admin on their dates. Distinct
            # start times (08:00 / 13:00) so they never collide with the
            # recordings (09:00–12:00) or the evening tasks (15:00+).
            for tst in _tests_on(cur):
                add("mock", tst["name"],
                    f"🎯 {tst['name']} — target {tst.get('target','')}. Analysis = as long as the test.",
                    "test", 180, "08:00")
            for adm in _admin_on(cur):
                add("admin", "Eligibility / Admin", adm["topic"], "admin", 60, "13:00")

            # Night ritual (skip the explicit kill-zone days, which already end on review).
            if not (key == "killzone" and iso in kz_by_date):
                add(NIGHT_RITUAL["subject"], "Daily Shutdown", NIGHT_RITUAL["topic"],
                    NIGHT_RITUAL["type"], NIGHT_RITUAL["durationMinutes"], NIGHT_RITUAL["startTime"])
            else:
                add("admin", "Daily Shutdown", NIGHT_RITUAL["topic"], "revision", 30, "23:00")

            cur += timedelta(days=1)
    return blocks


def build_meta() -> dict[str, Any]:
    """Everything the engine + Hermes need that isn't a per-day block."""
    phases = []
    for p in PHASES:
        phases.append({**p, "totalDays": phase_total_days(p)})
    return {
        "meta": META,
        "phases": phases,
        "exams": EXAMS,
        "primaryExam": PRIMARY_EXAM,
        "headerCountdowns": HEADER_COUNTDOWNS,
        "subjects": SUBJECTS,
        "types": TYPES,
        "batchWindows": BATCH_WINDOWS,
        "tests": TESTS,
        "alerts": ALERTS,
        "kpis": KPIS,
        "priorityLadder": PRIORITY_LADDER,
        "habits": HABITS,
    }


def build_spec_json() -> dict[str, Any]:
    """The full canonical spec (for inspection / external tooling)."""
    return {**build_meta(), "killzoneDays": KILLZONE_DAYS, "adminTasks": ADMIN_TASKS,
            "phaseTasks": PHASE_TASKS, "nightRitual": NIGHT_RITUAL}


def _write_artifacts() -> None:
    plan = build_plan()
    (DASHBOARD_DIR / "tracker_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    (DASHBOARD_DIR / "roadmap_meta.json").write_text(
        json.dumps(build_meta(), ensure_ascii=False, indent=1), encoding="utf-8")
    (DASHBOARD_DIR / "roadmap_spec.json").write_text(
        json.dumps(build_spec_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    # Quick summary to stdout.
    from collections import Counter
    print(f"blocks: {len(plan)}")
    print("by phase:", dict(Counter(b["phase"] for b in plan)))
    print("by subject:", dict(Counter(b["subject"] for b in plan)))
    print("by type:", dict(Counter(b["type"] for b in plan)))


if __name__ == "__main__":
    _write_artifacts()
