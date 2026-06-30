"""
Mission Control — Memory Core router (Phase 1, additive module).

A shared persistent memory layer for the Hermes team, modelled on the CoALA
memory taxonomy. Phase 1 ships the foundation:

  · A unified `memory_items` store with a 4-tier `kind` taxonomy
    (episodic / semantic / procedural / working) and salience + decay fields.
  · FTS5 full-text index for fast lexical retrieval (the vector tier is a
    documented Phase-2 slot; the column exists but is optional).
  · Episodic recording — the AI tutor records each Q/A turn here.
  · Hybrid retrieval — FTS5 BM25 + recency + salience, exposed at
    /api/memory/* and injected into the tutor as grounding context.

Design constraints honored:
  · Self-contained APIRouter, mounted by main.py with one include line.
  · SQLite only, no new infra. Lives in its OWN db (~/.hermes/memory_core.db)
    so the Hermes-owned memory_store.db is never touched or migrated.
  · Every route degrades gracefully; recording failures never break the tutor.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import config

router = APIRouter()

HERMES_HOME = config.HERMES_HOME
MEMORY_DB = HERMES_HOME / "memory_core.db"

# CoALA-aligned memory tiers. `working` is short-lived scratch; the rest persist.
KINDS = ("episodic", "semantic", "procedural", "working")

# Half-life (days) for the recency component of the retrieval score, per tier.
# Episodic memories fade faster; semantic/procedural are effectively durable.
_HALF_LIFE_DAYS = {"episodic": 14.0, "working": 1.0, "semantic": 365.0, "procedural": 365.0}

# ---------------------------------------------------------------------------
# Memory⇄Wiki bridge (the unified "know-me" recall layer). Guarded so a bridge
# import failure can never break the memory router.
# ---------------------------------------------------------------------------
try:
    import memory_bridge as _bridge

    def _is_study(content: str, *, subject: str = "", source: str = "",
                  tags: str = "") -> bool:
        return _bridge.is_study(content, subject=subject, source=source, tags=tags)
    _BRIDGE_OK = True
except Exception:  # pragma: no cover - bridge optional
    _bridge = None
    _BRIDGE_OK = False

    def _is_study(content: str, *, subject: str = "", source: str = "",
                  tags: str = "") -> bool:
        return False


# The holographic store (memory_store.db) lives in the Hermes agent tree, whose
# top-level module names (memory, tools, …) collide with the dashboard's once
# both are on sys.path. So any bridge op that touches it runs in a CLEAN
# subprocess via the bridge CLI (same isolation pattern as notebooklm.py).
import subprocess  # noqa: E402
import sys  # noqa: E402

_BRIDGE_PY = sys.executable
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent / "memory_bridge.py")


async def _run_bridge(*args: str, timeout: float = 60.0) -> dict[str, Any]:
    import asyncio

    def _call() -> dict[str, Any]:
        try:
            p = subprocess.run(
                [_BRIDGE_PY, _BRIDGE_SCRIPT, *args],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(Path(__file__).resolve().parent),
            )
            out = (p.stdout or "").strip()
            if not out:
                return {"ok": False, "error": (p.stderr or "no output")[:300]}
            return json.loads(out)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    return await asyncio.to_thread(_call)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Normalise to tz-aware UTC so arithmetic against _now() never raises on a
    # naive timestamp written by an external Hermes process.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(MEMORY_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db() -> None:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB), timeout=10)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL DEFAULT 'episodic',  -- episodic|semantic|procedural|working
            content     TEXT NOT NULL,
            summary     TEXT DEFAULT '',
            source      TEXT DEFAULT '',                   -- e.g. 'ai-tutor', 'chat', 'agent:scholar'
            actor       TEXT DEFAULT '',                   -- who/what produced it
            subject     TEXT DEFAULT '',                   -- optional study subject tag
            tags        TEXT DEFAULT '',                   -- comma-separated
            salience    REAL NOT NULL DEFAULT 0.5,         -- 0..1 importance
            access_count INTEGER NOT NULL DEFAULT 0,
            embedding   BLOB,                              -- Phase-2 vector slot (optional)
            meta        TEXT DEFAULT '{}',                 -- JSON sidecar
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            last_access TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mem_kind ON memory_items(kind)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mem_subject ON memory_items(subject)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mem_created ON memory_items(created_at)")
    # FTS5 mirror for lexical search (external-content, kept in sync via triggers).
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
        USING fts5(content, summary, tags, content=memory_items, content_rowid=id)
    """)
    # Keep the FTS index in lockstep with the base table.
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory_items BEGIN
            INSERT INTO memory_fts(rowid, content, summary, tags)
            VALUES (new.id, new.content, new.summary, new.tags);
        END
    """)
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory_items BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, summary, tags)
            VALUES ('delete', old.id, old.content, old.summary, old.tags);
        END
    """)
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory_items BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, content, summary, tags)
            VALUES ('delete', old.id, old.content, old.summary, old.tags);
            INSERT INTO memory_fts(rowid, content, summary, tags)
            VALUES (new.id, new.content, new.summary, new.tags);
        END
    """)
    conn.commit()
    conn.close()


_init_db()


# ---------------------------------------------------------------------------
# Core write / read primitives (importable by other modules, e.g. tools.py)
# ---------------------------------------------------------------------------
def record_memory(
    content: str,
    *,
    kind: str = "episodic",
    summary: str = "",
    source: str = "",
    actor: str = "",
    subject: str = "",
    tags: str = "",
    salience: float = 0.5,
    meta: dict[str, Any] | None = None,
) -> int | None:
    """Insert one memory item. Returns its id, or None on failure.

    Intentionally swallows errors: recording must never break a caller's
    primary flow (e.g. answering the user)."""
    content = (content or "").strip()
    if not content:
        return None
    # Keep the know-me memory layer study-free: study content is routed to
    # NotebookLM, never recorded here (per the user's memory-layer policy).
    if _is_study(content, subject=subject, source=source, tags=tags):
        return None
    if kind not in KINDS:
        kind = "episodic"
    now = _iso()
    try:
        conn = _conn()
        cur = conn.execute(
            """INSERT INTO memory_items
               (kind, content, summary, source, actor, subject, tags, salience,
                meta, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, content, summary, source, actor, subject, tags,
             max(0.0, min(1.0, float(salience))),
             json.dumps(meta or {}), now, now),
        )
        conn.commit()
        rid = int(cur.lastrowid)
        conn.close()
        return rid
    except sqlite3.Error:
        return None


def _fts_query(q: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression: keep word
    tokens, OR them so partial overlaps still match."""
    toks = re.findall(r"[A-Za-z0-9_]+", q.lower())
    toks = [t for t in toks if len(t) > 1][:12]
    return " OR ".join(toks)


def retrieve(
    query: str,
    *,
    limit: int = 6,
    kinds: tuple[str, ...] | None = None,
    subject: str = "",
) -> list[dict[str, Any]]:
    """Hybrid lexical retrieval: FTS5 BM25 relevance, reweighted by recency
    (per-tier half-life) and salience. Returns ranked memory dicts."""
    query = (query or "").strip()
    if not query:
        return []
    match = _fts_query(query)
    if not match:
        return []
    now = _now()
    rows: list[sqlite3.Row] = []
    conn = None
    try:
        conn = _conn()
        # bm25() returns a cost (lower = better); negate to a positive relevance.
        sql = """
            SELECT m.*, bm25(memory_fts) AS bm
            FROM memory_fts
            JOIN memory_items m ON m.id = memory_fts.rowid
            WHERE memory_fts MATCH ?
        """
        params: list[Any] = [match]
        if kinds:
            sql += " AND m.kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        if subject:
            sql += " AND (m.subject = ? OR m.subject = '')"
            params.append(subject)
        sql += " ORDER BY bm LIMIT 50"
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        d = dict(r)
        # lexical: map bm25 cost to ~0..1 (bm25 is typically negative-ish small)
        bm = float(d.pop("bm", 0.0) or 0.0)
        lexical = 1.0 / (1.0 + math.exp(bm))  # logistic squashing
        # recency: exponential decay on the item's tier half-life
        created = _parse_iso(d.get("created_at"))
        age_days = max(0.0, (now - created).total_seconds() / 86400.0) if created else 9999.0
        hl = _HALF_LIFE_DAYS.get(d.get("kind", "episodic"), 30.0)
        recency = math.pow(0.5, age_days / hl)
        sal = float(d.get("salience", 0.5) or 0.5)
        score = 0.6 * lexical + 0.25 * recency + 0.15 * sal
        d["_score"] = round(score, 4)
        d["_age_days"] = round(age_days, 2)
        scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


def _touch_access(ids: list[int]) -> None:
    if not ids:
        return
    now = _iso()
    try:
        conn = _conn()
        conn.executemany(
            "UPDATE memory_items SET access_count = access_count + 1, last_access = ? WHERE id = ?",
            [(now, i) for i in ids],
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def context_block(query: str, *, subject: str = "", limit: int = 5) -> str:
    """Return a compact text block of relevant memories for LLM grounding,
    or '' if nothing relevant. Also bumps access counters."""
    hits = retrieve(query, limit=limit, subject=subject)
    if not hits:
        return ""
    _touch_access([h["id"] for h in hits])
    lines = []
    for h in hits:
        tag = h.get("kind", "")[:3].upper()
        txt = (h.get("summary") or h.get("content") or "").strip().replace("\n", " ")
        if len(txt) > 280:
            txt = txt[:277] + "…"
        lines.append(f"- [{tag}] {txt}")
    return "Relevant memory:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@router.get("/api/memory/health")
async def memory_health() -> JSONResponse:
    try:
        conn = _conn()
        total = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
        by_kind = {
            row[0]: row[1]
            for row in conn.execute("SELECT kind, COUNT(*) FROM memory_items GROUP BY kind")
        }
        conn.close()
        return JSONResponse({"ok": True, "db": str(MEMORY_DB), "total": total, "by_kind": by_kind})
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/memory/record")
async def memory_record(payload: dict[str, Any]) -> JSONResponse:
    content = str(payload.get("content", "")).strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    # Study content is excluded from the know-me layer (routed to NotebookLM).
    if _is_study(content, subject=str(payload.get("subject", "")),
                 source=str(payload.get("source", "")), tags=str(payload.get("tags", ""))):
        return JSONResponse({"ok": True, "recorded": False, "reason": "study-excluded"})
    # Coerce salience defensively — malformed input should be a clean 400, not a 500.
    try:
        salience = float(payload.get("salience", 0.5))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="salience must be a number 0..1")
    rid = record_memory(
        content,
        kind=str(payload.get("kind", "episodic")),
        summary=str(payload.get("summary", "")),
        source=str(payload.get("source", "api")),
        actor=str(payload.get("actor", "")),
        subject=str(payload.get("subject", "")),
        tags=str(payload.get("tags", "")),
        salience=salience,
        meta=payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    )
    if rid is None:
        raise HTTPException(status_code=500, detail="failed to record memory")
    return JSONResponse({"ok": True, "id": rid})


@router.get("/api/memory/search")
async def memory_search(q: str, limit: int = 8, kind: str = "", subject: str = "") -> JSONResponse:
    kinds = tuple(k for k in (kind,) if k in KINDS) or None
    hits = retrieve(q, limit=max(1, min(50, limit)), kinds=kinds, subject=subject)
    return JSONResponse({"ok": True, "query": q, "count": len(hits), "results": hits})


@router.get("/api/memory/recent")
async def memory_recent(limit: int = 20, kind: str = "") -> JSONResponse:
    try:
        conn = _conn()
        if kind in KINDS:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                (kind, max(1, min(100, limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memory_items ORDER BY created_at DESC LIMIT ?",
                (max(1, min(100, limit)),),
            ).fetchall()
        conn.close()
        return JSONResponse({"ok": True, "results": [dict(r) for r in rows]})
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/memory/{item_id}")
async def memory_delete(item_id: int) -> JSONResponse:
    try:
        conn = _conn()
        cur = conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "deleted": cur.rowcount})
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Unified "know-me" layer — wiki (LLM Wiki) + memory DBs + Hermes facts
# ---------------------------------------------------------------------------
@router.get("/api/memory/knowledge")
async def memory_knowledge() -> JSONResponse:
    """Status of the combined know-me layer: vault pages, facts, profile,
    study-free state. Read-only."""
    if not _BRIDGE_OK:
        return JSONResponse({"ok": False, "reason": "bridge-unavailable"})
    return JSONResponse(await _run_bridge("status", timeout=30))


@router.post("/api/memory/sync")
async def memory_sync() -> JSONResponse:
    """Run the vault⇄memory bridge: materialize know-me memories into the LLM
    Wiki, refresh the Hermes USER.md profile, and upsert vault know-me facts."""
    if not _BRIDGE_OK:
        return JSONResponse({"ok": False, "reason": "bridge-unavailable"})
    return JSONResponse(await _run_bridge("run", timeout=120))


@router.get("/api/memory/unified")
async def memory_unified(q: str, limit: int = 8) -> JSONResponse:
    """Combined recall across three sources, study-filtered and source-labeled:
      · memory  — dashboard capture buffer (memory_core.db)
      · fact    — Hermes holographic know-me facts (memory_store.db)
      · wiki    — the Obsidian LLM Wiki pages
    """
    q = (q or "").strip()
    if not q:
        return JSONResponse({"ok": True, "query": q, "count": 0, "results": []})
    lim = max(1, min(20, limit))
    results: list[dict[str, Any]] = []

    # 1. dashboard memory_core (in-process — its own db, no collision)
    for h in retrieve(q, limit=lim):
        txt = (h.get("summary") or h.get("content") or "").strip()
        results.append({
            "source": "memory", "kind": h.get("kind", ""),
            "title": txt[:80], "text": txt[:400],
            "score": h.get("_score", 0.0),
        })

    # 2 + 3. Hermes facts + LLM Wiki pages (via the isolated bridge subprocess)
    if _BRIDGE_OK:
        sr = await _run_bridge("search", q, timeout=30)
        for f in sr.get("facts", []) or []:
            c = (f.get("content", "") or "")
            results.append({
                "source": "fact", "kind": f.get("category", ""),
                "title": c[:80], "text": c[:400],
                "score": float(f.get("trust", 0.5) or 0.5),
            })
        for v in sr.get("vault", []) or []:
            results.append({
                "source": "wiki", "kind": "page",
                "title": v.get("title", ""), "text": v.get("snippet", ""),
                "path": v.get("path", ""),
                "score": float(v.get("score", 0)),
            })

    results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return JSONResponse({"ok": True, "query": q, "count": len(results),
                         "results": results[: lim * 2]})
