"""
Mission Control — Knowledge ingestion router (additive module).

Turns the study vault (~/subjects/**/*.md) into a queryable
KNOWLEDGE graph — the thing the dashboard's Graph view is supposed to show,
as opposed to the dashboard's own Python source (that lives behind
/api/graph/code now).

What it does
------------
An idempotent INGESTOR scans every Markdown note under SUBJECTS_DIR and
extracts:

  · per-file title (first H1, else front-matter `title:`, else filename)
  · headings (#, ##, …)  — folded into the note's text for search
  · [[wikilinks]]        — explicit concept references (note -> concept edge)
  · #tags                — lightweight concept references (note -> concept edge)

From those it builds a small graph in the dashboard-local memory_core.db
(~/dashboard/memory_core.db) using ADDITIVE tables only:

  knowledge_notes      one row per scanned .md file
  knowledge_concepts   one row per distinct concept (wikilink target / tag)
  knowledge_edges      note->concept (mentions) + concept->concept
                       (co-occurrence inside the same note)
  knowledge_fts        FTS5 lexical index over note title + text

The table names are deliberately prefixed `knowledge_` so they never collide
with the CoALA tutor-memory tables (`memory_items`, `memory_fts`, …) defined in
memory.py — and in any case those live in a DIFFERENT db file
(~/.hermes/memory_core.db), not this dashboard-local one. We never touch them.

Re-runs are idempotent: the whole knowledge_* set is rebuilt from the current
state of the vault on each reindex, so deleted notes disappear and the graph
always reflects what is on disk right now. CREATE TABLE IF NOT EXISTS means a
first run on an empty 0-byte db just works.

Routes:
  POST /api/knowledge/reindex        -> {"ok", "notes", "concepts", "edges"}
  GET  /api/knowledge/search?q=...   -> {"ok", "query", "count", "results"}
  GET  /api/knowledge/health         -> table presence + row counts

Self-contained APIRouter, mounted by main.py with a single include line.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter()

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
# The study vault. Overridable via env (mirrors automation_hooks.py) so tests
# can point it elsewhere without touching the real tree.
SUBJECTS_DIR = config.SUBJECTS_DIR

# Dashboard-local memory core (NOT ~/.hermes/memory_core.db — that one is the
# tutor's, is write-guarded, and uses different table names). This file is the
# additive store the knowledge graph owns.
KNOWLEDGE_DB = config.KNOWLEDGE_DB

# Caps so the graph stays snappy in the frontend even if the vault grows large.
MAX_CONCEPTS = 4000
MAX_EDGES = 20000

# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:[#|][^\[\]]*)?\]\]")
_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z][A-Za-z0-9_\-/]*)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_H1_RE = re.compile(r"^\s{0,3}#\s+(.+?)\s*#*\s*$")
_FM_TITLE_RE = re.compile(r"^title\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _concept_key(name: str) -> str:
    """Normalise a concept label to a stable id key (case/space-insensitive)."""
    return re.sub(r"\s+", " ", name.strip()).lower()


def _strip_code(text: str) -> str:
    """Remove fenced + inline code so `#define`-style hashes aren't read as
    tags and code identifiers don't masquerade as wikilinks."""
    text = _CODE_FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return text


def _front_matter_and_body(text: str) -> tuple[str, str]:
    """Split a leading YAML front-matter block (--- … ---) from the body."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            fm = text[3:end]
            body = text[nl + 1 :] if nl != -1 else ""
            return fm, body
    return "", text


def _parse_note(path: Path, root: Path) -> dict[str, Any] | None:
    """Parse one .md file. Returns a dict of extracted fields, or None if the
    file can't be read."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm, body = _front_matter_and_body(raw)
    clean = _strip_code(body)

    # Title precedence: first H1 -> front-matter title -> filename stem.
    title = ""
    for line in body.splitlines():
        m = _H1_RE.match(line)
        if m:
            title = m.group(1).strip()
            break
    if not title and fm:
        m = _FM_TITLE_RE.search(fm)
        if m:
            title = m.group(1).strip().strip("\"'")
    if not title:
        title = path.stem.replace("_", " ").replace("-", " ").strip()

    # Headings (all levels) — used as searchable signal.
    headings = [m.group(2).strip() for ln in clean.splitlines()
                if (m := _HEADING_RE.match(ln))]

    # Concepts: wikilink targets + tags. Preserve first-seen display casing.
    concepts: dict[str, str] = {}
    for m in _WIKILINK_RE.finditer(clean):
        label = m.group(1).strip()
        if label:
            concepts.setdefault(_concept_key(label), label)
    for m in _TAG_RE.finditer(clean):
        label = m.group(1).strip().lstrip("/")
        if label:
            concepts.setdefault(_concept_key(label), label)

    # Subject = top-level folder under the vault root (best-effort).
    subject = ""
    try:
        rel = path.relative_to(root)
        if len(rel.parts) > 1:
            subject = rel.parts[0]
    except ValueError:
        pass

    # Searchable text: title + headings + a bounded slice of the body.
    search_text = "\n".join([title, *headings, clean])[:20000]

    try:
        rel_path = str(path.relative_to(root))
    except ValueError:
        rel_path = str(path)

    return {
        "path": rel_path,
        "abspath": str(path),
        "title": title,
        "subject": subject,
        "headings": headings,
        "concepts": concepts,  # key -> display label
        "search_text": search_text,
    }


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL is fine here: KNOWLEDGE_DB defaults to the repo dir (a normal POSIX
    # filesystem) and is opened only by the single long-lived web-server
    # process. If you override KNOWLEDGE_DB onto an ntfs3 / SMB / NFS mount,
    # switch this to journal_mode=TRUNCATE — WAL's -shm mmap is unreliable there
    # (see memory.py for the ntfs3-safe pattern).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the additive knowledge_* tables if absent. Never drops anything;
    safe against an empty 0-byte db and against a db that already holds the
    tutor's memory_items/memory_fts (different names, no overlap)."""
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT NOT NULL UNIQUE,   -- vault-relative path
            title       TEXT NOT NULL DEFAULT '',
            subject     TEXT NOT NULL DEFAULT '',
            headings    TEXT NOT NULL DEFAULT '',   -- newline-joined
            n_concepts  INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_concepts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL UNIQUE,   -- normalised concept key
            label       TEXT NOT NULL,          -- display label
            mentions    INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,          -- node id (note:… or concept:…)
            target      TEXT NOT NULL,          -- node id
            kind        TEXT NOT NULL,          -- 'mentions' | 'cooccurs'
            weight      INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source, target, kind)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_kedges_kind ON knowledge_edges(kind)")
    # FTS5 index over note title + text (standalone content, rebuilt each run).
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts
        USING fts5(path, title, body)
    """)
    conn.commit()


def _clear(conn: sqlite3.Connection) -> None:
    """Wipe the knowledge_* rows for an idempotent rebuild. Only touches the
    knowledge_* tables — the tutor tables (if present in this file) are never
    referenced."""
    for tbl in ("knowledge_edges", "knowledge_concepts", "knowledge_notes",
                "knowledge_fts"):
        try:
            conn.execute(f"DELETE FROM {tbl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def _iter_md(root: Path):
    """Yield every *.md / *.markdown file under root, skipping dotfolders."""
    if not root.is_dir():
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".md", ".markdown"):
            continue
        # Skip hidden / sync-metadata folders (.stfolder, .git, …).
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        yield p


def reindex(root: Path | None = None, db: Path | None = None) -> dict[str, int]:
    """Scan the vault and rebuild the knowledge graph in memory_core.db.

    Idempotent: clears and rebuilds knowledge_* from the current on-disk state.
    Returns {"notes", "concepts", "edges"} counts. Never raises on an empty or
    missing vault — it just produces zeros (clean empty state)."""
    root = (root or SUBJECTS_DIR)
    db = db or KNOWLEDGE_DB

    parsed: list[dict[str, Any]] = []
    for path in _iter_md(root):
        note = _parse_note(path, root)
        if note is not None:
            parsed.append(note)

    # Aggregate concepts across all notes (count mentions).
    concept_label: dict[str, str] = {}
    concept_mentions: dict[str, int] = {}
    for note in parsed:
        for key, label in note["concepts"].items():
            concept_label.setdefault(key, label)
            concept_mentions[key] = concept_mentions.get(key, 0) + 1

    # Cap concepts by mention count if the vault is huge.
    if len(concept_label) > MAX_CONCEPTS:
        top = sorted(concept_mentions.items(), key=lambda kv: kv[1], reverse=True)
        keep = {k for k, _ in top[:MAX_CONCEPTS]}
        concept_label = {k: v for k, v in concept_label.items() if k in keep}
        concept_mentions = {k: v for k, v in concept_mentions.items() if k in keep}

    now = datetime.now(timezone.utc).isoformat()

    # Build edges:
    #   note->concept   (mentions, weight 1)
    #   concept->concept (cooccurs, weight = #notes sharing both)
    mention_edges: list[tuple[str, str]] = []
    cooccur: dict[tuple[str, str], int] = {}
    for note in parsed:
        keys = [k for k in note["concepts"].keys() if k in concept_label]
        nid = f"note:{note['path']}"
        for k in keys:
            mention_edges.append((nid, f"concept:{k}"))
        uniq = sorted(set(keys))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                cooccur[(a, b)] = cooccur.get((a, b), 0) + 1

    conn = _conn()
    try:
        _init_db(conn)
        _clear(conn)

        conn.executemany(
            "INSERT OR IGNORE INTO knowledge_notes "
            "(path, title, subject, headings, n_concepts, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            [(n["path"], n["title"], n["subject"],
              "\n".join(n["headings"]),
              len([k for k in n["concepts"] if k in concept_label]), now)
             for n in parsed],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO knowledge_concepts "
            "(key, label, mentions, updated_at) VALUES (?,?,?,?)",
            [(k, concept_label[k], concept_mentions.get(k, 0), now)
             for k in concept_label],
        )
        conn.executemany(
            "INSERT INTO knowledge_fts (path, title, body) VALUES (?,?,?)",
            [(n["path"], n["title"], n["search_text"]) for n in parsed],
        )

        edge_rows: list[tuple[str, str, str, int]] = []
        for src, tgt in mention_edges:
            edge_rows.append((src, tgt, "mentions", 1))
        for (a, b), w in cooccur.items():
            edge_rows.append((f"concept:{a}", f"concept:{b}", "cooccurs", w))
        edge_rows = edge_rows[:MAX_EDGES]
        conn.executemany(
            "INSERT OR REPLACE INTO knowledge_edges "
            "(source, target, kind, weight) VALUES (?,?,?,?)",
            edge_rows,
        )
        conn.commit()

        n_notes = conn.execute("SELECT COUNT(*) FROM knowledge_notes").fetchone()[0]
        n_concepts = conn.execute("SELECT COUNT(*) FROM knowledge_concepts").fetchone()[0]
        n_edges = conn.execute("SELECT COUNT(*) FROM knowledge_edges").fetchone()[0]
    finally:
        conn.close()

    return {"notes": int(n_notes), "concepts": int(n_concepts), "edges": int(n_edges)}


def _fts_match(q: str) -> str:
    """Safe FTS5 MATCH expression: word tokens OR'd together, prefix-matched."""
    toks = re.findall(r"[A-Za-z0-9_]+", q.lower())
    toks = [t for t in toks if len(t) > 1][:12]
    return " OR ".join(f"{t}*" for t in toks)


def search(q: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """FTS lexical search over ingested notes. Returns [] on empty / no index."""
    q = (q or "").strip()
    if not q:
        return []
    match = _fts_match(q)
    if not match:
        return []
    conn = _conn()
    try:
        # If the index hasn't been built yet, the table may be absent.
        try:
            rows = conn.execute(
                "SELECT path, title, snippet(knowledge_fts, 2, '[', ']', '…', 12) AS snip, "
                "bm25(knowledge_fts) AS bm "
                "FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm LIMIT ?",
                (match, max(1, min(50, limit))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        out.append({
            "path": d.get("path", ""),
            "title": d.get("title", ""),
            "snippet": (d.get("snip", "") or "").replace("\n", " ").strip(),
            "score": round(-float(d.get("bm", 0.0) or 0.0), 4),
        })
    return out


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@router.post("/api/knowledge/reindex")
async def knowledge_reindex() -> JSONResponse:
    """Re-scan the study vault and rebuild the knowledge graph. Idempotent.

    Called by the vault-reindex watcher (alongside reindex-subjects) and on
    demand from the dashboard. Returns fresh {notes, concepts, edges} counts.
    """
    import asyncio
    try:
        counts = await asyncio.to_thread(reindex)
        return JSONResponse({"ok": True, **counts})
    except Exception as e:  # noqa: BLE001 — never 500 the watcher
        return JSONResponse({"ok": False, "error": str(e),
                             "notes": 0, "concepts": 0, "edges": 0})


@router.get("/api/knowledge/search")
async def knowledge_search(q: str, limit: int = 12) -> JSONResponse:
    """FTS5 hits over ingested note titles + bodies."""
    hits = search(q, limit=limit)
    return JSONResponse({"ok": True, "query": q, "count": len(hits), "results": hits})


@router.get("/api/knowledge/health")
async def knowledge_health() -> JSONResponse:
    """Row counts for the knowledge graph; clean zeros before first ingest."""
    out: dict[str, Any] = {"ok": True, "db": str(KNOWLEDGE_DB),
                           "subjects_dir": str(SUBJECTS_DIR),
                           "notes": 0, "concepts": 0, "edges": 0}
    try:
        conn = _conn()
        try:
            for tbl, key in (("knowledge_notes", "notes"),
                             ("knowledge_concepts", "concepts"),
                             ("knowledge_edges", "edges")):
                try:
                    out[key] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except sqlite3.OperationalError:
                    out[key] = 0
        finally:
            conn.close()
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse(out)
