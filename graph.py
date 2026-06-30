"""
Mission Control — read-only knowledge-graph router (additive module).

Serves graph JSON for the dashboard's System > Graph views. Three independent
graphs, all opened strictly read-only (no writes, ever) and each degrading to
an empty graph if their backing DB / table is missing:

  GET /api/graph/agents    Agent activity graph from ~/.hermes/agent-logs.db.
                           Bipartite agents <-> models, edges weighted by how
                           many logged interactions tie an agent to a model.

  GET /api/graph/concepts  KNOWLEDGE graph built by the vault ingestor
                           (knowledge.py) — your study NOTES and the CONCEPTS
                           they mention (wikilinks + tags) as nodes, with
                           note->concept (mentions) and concept->concept
                           (co-occurrence) edges, read from knowledge_* tables
                           in memory_core.db. If those tables are empty it falls
                           back to scanning ~/subjects directly, and
                           returns a clean empty graph when there are no notes
                           yet. Capped (~300 nodes / ~800 edges) for the frontend.

  GET /api/graph/code      The OLD concepts behaviour: a code-symbol graph from
                           the dashboard codegraph DB (.codegraph/codegraph.db)
                           — functions/methods/classes as nodes, calls/imports
                           as edges. This is the dashboard's own source, kept
                           available but no longer the default "concepts" view.

Response shape (both endpoints):
  { "nodes": [{"id","label","group", ...}],
    "edges": [{"source","target","weight", ...}] }

Self-contained APIRouter, mounted by main.py with a single include line.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter()

# ---- DB locations (all opened read-only; resolved in config.py) ------------
AGENT_LOG_DB = config.AGENT_LOG_DB
CODEGRAPH_DB = config.CODEGRAPH_DB
MEMORY_CORE_DB = config.KNOWLEDGE_DB
# Study vault — the source of the knowledge graph (direct-scan fallback only).
SUBJECTS_DIR = config.SUBJECTS_DIR

# ---- concept-graph caps ----------------------------------------------------
MAX_CONCEPT_NODES = 300
MAX_CONCEPT_EDGES = 800


def _ro_connect(path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB read-only (file:...?mode=ro). None if absent/unopenable.

    mode=ro guarantees we never create or mutate the file even by accident.
    """
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    try:
        row = con.execute(
            "select 1 from sqlite_master where type in ('table','view') and name=? limit 1",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _empty() -> dict:
    return {"nodes": [], "edges": []}


# ===========================================================================
# /api/graph/concepts — KNOWLEDGE graph from the ingested study vault
# ===========================================================================
# Built by knowledge.py's ingestor into memory_core.db (knowledge_* tables):
#   knowledge_notes     one node per scanned .md note
#   knowledge_concepts  one node per wikilink-target / tag
#   knowledge_edges     note->concept (mentions) + concept->concept (cooccurs)
# We read those read-only. If the tables are empty we fall back to scanning the
# subjects tree directly so the view still lights up; if there are no notes at
# all we return a clean empty graph.


def _truncate(text: str, n: int = 60) -> str:
    text = (text or "").strip()
    return (text[: n - 1] + "…") if len(text) > n else text


def _concepts_from_knowledge(con: sqlite3.Connection) -> dict | None:
    """Build the knowledge graph from the ingested knowledge_* tables.

    Returns None if the tables are absent/unbuilt (caller falls back to a live
    vault scan). Returns an empty graph dict if the tables exist but hold no
    notes. Capped to MAX_CONCEPT_NODES / MAX_CONCEPT_EDGES.
    """
    if not (_has_table(con, "knowledge_notes")
            and _has_table(con, "knowledge_concepts")
            and _has_table(con, "knowledge_edges")):
        return None

    try:
        note_rows = con.execute(
            "select path, title, subject, n_concepts from knowledge_notes "
            "order by n_concepts desc, path limit ?",
            (MAX_CONCEPT_NODES,),
        ).fetchall()
        concept_rows = con.execute(
            "select key, label, mentions from knowledge_concepts "
            "order by mentions desc, label limit ?",
            (MAX_CONCEPT_NODES,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None

    if not note_rows and not concept_rows:
        # Tables exist but are empty — clean empty state.
        return _empty()

    nodes: list[dict] = []
    valid: set[str] = set()

    for path, title, subject, n_concepts in note_rows:
        nid = f"note:{path}"
        valid.add(nid)
        nodes.append({
            "id": nid,
            "label": _truncate(title or path),
            "group": "note",
            "subject": subject or "",
            "count": int(n_concepts or 0),
            "path": path,
        })

    for key, label, mentions in concept_rows:
        nid = f"concept:{key}"
        if nid in valid:
            continue
        valid.add(nid)
        nodes.append({
            "id": nid,
            "label": _truncate(label or key),
            "group": "concept",
            "count": int(mentions or 0),
        })

    edges: list[dict] = []
    try:
        edge_rows = con.execute(
            "select source, target, kind, weight from knowledge_edges "
            "order by weight desc limit ?",
            (MAX_CONCEPT_EDGES * 3,),
        ).fetchall()
    except sqlite3.OperationalError:
        edge_rows = []

    for source, target, kind, weight in edge_rows:
        # Drop edges whose endpoints didn't survive the node cap so the
        # frontend never references a missing node.
        if source in valid and target in valid and source != target:
            edges.append({
                "source": source,
                "target": target,
                "kind": kind or "mentions",
                "weight": int(weight or 1),
            })
            if len(edges) >= MAX_CONCEPT_EDGES:
                break

    return {"nodes": nodes, "edges": edges}


def _concepts_from_vault_scan() -> dict:
    """Fallback: build the knowledge graph by scanning the vault directly.

    Used when the ingested knowledge_* tables are empty (e.g. the ingestor
    hasn't run yet) but notes already exist on disk. Reuses knowledge.py's
    parser so the shape matches the ingested path exactly. Returns a clean
    empty graph if there are no notes.
    """
    try:
        import knowledge  # same-dir sibling module
    except Exception:  # noqa: BLE001
        return _empty()

    if not SUBJECTS_DIR.is_dir():
        return _empty()

    concept_label: dict[str, str] = {}
    concept_mentions: dict[str, int] = {}
    parsed: list[dict] = []
    try:
        for path in knowledge._iter_md(SUBJECTS_DIR):
            note = knowledge._parse_note(path, SUBJECTS_DIR)
            if note is None:
                continue
            parsed.append(note)
            for k, lbl in note["concepts"].items():
                concept_label.setdefault(k, lbl)
                concept_mentions[k] = concept_mentions.get(k, 0) + 1
    except Exception:  # noqa: BLE001 — never error the endpoint on a scan hiccup
        return _empty()

    if not parsed:
        return _empty()

    nodes: list[dict] = []
    valid: set[str] = set()
    for note in parsed[:MAX_CONCEPT_NODES]:
        nid = f"note:{note['path']}"
        valid.add(nid)
        nodes.append({
            "id": nid,
            "label": _truncate(note["title"] or note["path"]),
            "group": "note",
            "subject": note.get("subject", ""),
            "count": len(note["concepts"]),
            "path": note["path"],
        })

    # Concepts ranked by mentions, respecting the remaining node budget.
    ranked = sorted(concept_mentions.items(), key=lambda kv: kv[1], reverse=True)
    for key, mentions in ranked:
        if len(valid) >= MAX_CONCEPT_NODES:
            break
        nid = f"concept:{key}"
        if nid in valid:
            continue
        valid.add(nid)
        nodes.append({
            "id": nid,
            "label": _truncate(concept_label.get(key, key)),
            "group": "concept",
            "count": int(mentions),
        })

    edges: list[dict] = []
    cooccur: dict[tuple[str, str], int] = {}
    for note in parsed:
        nid = f"note:{note['path']}"
        keys = [k for k in note["concepts"].keys() if f"concept:{k}" in valid]
        if nid not in valid:
            continue
        for k in keys:
            tid = f"concept:{k}"
            if len(edges) < MAX_CONCEPT_EDGES:
                edges.append({"source": nid, "target": tid,
                              "kind": "mentions", "weight": 1})
        uniq = sorted(set(keys))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                cooccur[(uniq[i], uniq[j])] = cooccur.get((uniq[i], uniq[j]), 0) + 1

    for (a, b), w in cooccur.items():
        if len(edges) >= MAX_CONCEPT_EDGES:
            break
        edges.append({"source": f"concept:{a}", "target": f"concept:{b}",
                      "kind": "cooccurs", "weight": int(w)})

    return {"nodes": nodes, "edges": edges}


# ===========================================================================
# /api/graph/agents — agents <-> models, weighted by interaction count
# ===========================================================================
@router.get("/api/graph/agents")
async def graph_agents() -> JSONResponse:
    """Build an agent-activity graph from agent_logs.

    Each distinct agent and each distinct model becomes a node; an edge ties an
    agent to a model and is weighted by the number of logged interactions
    between them. Agent node size (``count``) reflects total activity, so the
    frontend can scale markers. Empty graph if the DB/table is missing.
    """
    con = _ro_connect(AGENT_LOG_DB)
    if con is None:
        return JSONResponse(_empty())

    try:
        if not _has_table(con, "agent_logs"):
            return JSONResponse(_empty())
        try:
            rows = con.execute(
                "select agent_name, model_used, count(*) "
                "from agent_logs "
                "where agent_name is not null and agent_name != '' "
                "group by agent_name, model_used"
            ).fetchall()
        except sqlite3.OperationalError:
            return JSONResponse(_empty())
    finally:
        con.close()

    if not rows:
        return JSONResponse(_empty())

    agent_totals: dict[str, int] = {}
    model_totals: dict[str, int] = {}
    edges: list[dict] = []

    for agent_name, model_used, cnt in rows:
        agent = (agent_name or "").strip()
        # Normalize agent names so "Bill" and "bill" collapse to one node.
        agent_key = f"agent:{agent.lower()}"
        model = (model_used or "unknown").strip() or "unknown"
        model_key = f"model:{model.lower()}"
        weight = int(cnt or 0)

        agent_totals[agent_key] = agent_totals.get(agent_key, 0) + weight
        model_totals[model_key] = model_totals.get(model_key, 0) + weight

        edges.append(
            {
                "source": agent_key,
                "target": model_key,
                "weight": weight,
            }
        )

    # Preserve a readable label: use the most "title-ish" casing we saw.
    agent_labels: dict[str, str] = {}
    model_labels: dict[str, str] = {}
    for agent_name, model_used, _cnt in rows:
        a = (agent_name or "").strip()
        ak = f"agent:{a.lower()}"
        # Prefer a label that isn't all-lowercase if one exists.
        if ak not in agent_labels or (a != a.lower() and agent_labels[ak] == agent_labels[ak].lower()):
            agent_labels[ak] = a or "(unknown)"
        m = (model_used or "unknown").strip() or "unknown"
        mk = f"model:{m.lower()}"
        if mk not in model_labels:
            model_labels[mk] = m

    nodes: list[dict] = []
    for key, total in agent_totals.items():
        nodes.append(
            {
                "id": key,
                "label": agent_labels.get(key, key.split(":", 1)[-1]),
                "group": "agent",
                "count": total,
            }
        )
    for key, total in model_totals.items():
        nodes.append(
            {
                "id": key,
                "label": model_labels.get(key, key.split(":", 1)[-1]),
                "group": "model",
                "count": total,
            }
        )

    return JSONResponse({"nodes": nodes, "edges": edges})


# ===========================================================================
# /api/graph/concepts — code symbols + their relations (capped)
# ===========================================================================
# Edge kinds in codegraph that represent meaningful concept relations. We skip
# bare structural "contains" (file->symbol) edges to keep the graph about how
# concepts relate to each other rather than file membership.
_CONCEPT_EDGE_KINDS = ("calls", "references", "instantiates", "imports", "extends")

# Symbol kinds we treat as "concepts". File nodes are excluded.
_CONCEPT_NODE_KINDS = (
    "function",
    "method",
    "class",
    "interface",
    "type",
    "route",
    "constant",
    "variable",
)


def _concepts_from_codegraph(con: sqlite3.Connection) -> dict | None:
    """Build a concept graph from the codegraph nodes/edges tables.

    Strategy: pick the highest-connectivity relation edges first (so the capped
    subgraph stays meaningful), collect the symbols they touch up to the node
    cap, then keep only edges whose endpoints both survived. Returns None if the
    expected tables aren't present so the caller can fall back.
    """
    if not (_has_table(con, "nodes") and _has_table(con, "edges")):
        return None

    placeholders = ",".join("?" for _ in _CONCEPT_EDGE_KINDS)
    try:
        # Order edges by combined endpoint degree so the most "central" concept
        # relations are kept when we hit the cap. We approximate degree by how
        # often each endpoint appears across concept edges.
        edge_rows = con.execute(
            f"""
            with rel as (
                select source, target, kind from edges
                where kind in ({placeholders})
            ),
            deg as (
                select node, count(*) c from (
                    select source as node from rel
                    union all
                    select target as node from rel
                ) group by node
            )
            select r.source, r.target, r.kind,
                   coalesce(ds.c,0) + coalesce(dt.c,0) as score
            from rel r
            left join deg ds on ds.node = r.source
            left join deg dt on dt.node = r.target
            order by score desc
            limit ?
            """,
            (*_CONCEPT_EDGE_KINDS, MAX_CONCEPT_EDGES * 3),
        ).fetchall()
    except sqlite3.OperationalError:
        return None

    if not edge_rows:
        # No relation edges — fall back to exposing the symbols themselves.
        node_rows = _fetch_concept_nodes(con, set())
        nodes = [_concept_node(r) for r in node_rows]
        return {"nodes": nodes, "edges": []}

    # Collect candidate node ids in score order, respecting the node cap.
    # Skip file:/import: endpoints — we want a symbol-to-symbol concept graph,
    # not file-membership relations.
    wanted: list[str] = []
    seen: set[str] = set()
    for source, target, _kind, _score in edge_rows:
        if source.startswith(("file:", "import:")) or target.startswith(("file:", "import:")):
            continue
        for nid in (source, target):
            if nid not in seen:
                seen.add(nid)
                wanted.append(nid)
        if len(wanted) >= MAX_CONCEPT_NODES:
            break

    keep_ids = set(wanted[:MAX_CONCEPT_NODES])
    if not keep_ids:
        return {"nodes": [], "edges": []}

    node_rows = _fetch_concept_nodes(con, keep_ids)
    node_map = {r[0]: r for r in node_rows}
    # A referenced id might be a file/import node not in our concept fetch; keep
    # only ids we actually resolved to real nodes.
    resolved = set(node_map.keys())

    edges: list[dict] = []
    edge_seen: set[tuple[str, str, str]] = set()
    for source, target, kind, _score in edge_rows:
        if source in resolved and target in resolved and source != target:
            sig = (source, target, kind)
            if sig in edge_seen:
                continue
            edge_seen.add(sig)
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "kind": kind,
                    "weight": 1,
                }
            )
            if len(edges) >= MAX_CONCEPT_EDGES:
                break

    nodes = [_concept_node(node_map[nid]) for nid in resolved]
    return {"nodes": nodes, "edges": edges}


def _fetch_concept_nodes(con: sqlite3.Connection, ids: set[str]) -> list[tuple]:
    """Fetch (id, kind, name, file_path) for concept nodes.

    If ``ids`` is given, restrict to those (chunked to dodge SQLite's variable
    limit). Otherwise fetch up to the node cap of any concept-kind symbol.
    """
    cols = "id, kind, name, file_path"
    if ids:
        out: list[tuple] = []
        id_list = list(ids)
        CHUNK = 400
        for i in range(0, len(id_list), CHUNK):
            chunk = id_list[i : i + CHUNK]
            ph = ",".join("?" for _ in chunk)
            try:
                out.extend(
                    con.execute(
                        f"select {cols} from nodes where id in ({ph})",
                        chunk,
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                break
        return out

    kind_ph = ",".join("?" for _ in _CONCEPT_NODE_KINDS)
    try:
        return con.execute(
            f"select {cols} from nodes where kind in ({kind_ph}) limit ?",
            (*_CONCEPT_NODE_KINDS, MAX_CONCEPT_NODES),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def _concept_node(row: tuple) -> dict:
    """Map a (id, kind, name, file_path) row to a graph node.

    ``group`` is the symbol kind (function/method/class/...) so the frontend can
    colour by type; ``file`` is kept as a tooltip hint.
    """
    nid, kind, name, file_path = row
    label = name or (nid.split(":", 1)[-1] if nid else "?")
    return {
        "id": nid,
        "label": label,
        "group": kind or "symbol",
        "file": file_path or "",
    }


def _concepts_from_memory_core(con: sqlite3.Connection) -> dict | None:
    """Best-effort concept graph from memory_core.db.

    The memory core schema is CoALA-tiered and evolving; rather than hard-code a
    shape that may not exist, we look for any table that pairs an id-like column
    with a text/label column and any relation table linking two ids. If nothing
    usable is found we return an empty graph (never an error).
    """
    # Discover tables.
    try:
        tables = [
            r[0]
            for r in con.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            ).fetchall()
        ]
    except sqlite3.Error:
        return _empty()

    if not tables:
        return _empty()

    # Heuristic: a "concept/node" table has a text label column; a "relation"
    # table has two columns that look like source/target. We keep this defensive
    # because memory_core.db is currently empty on this box.
    node_candidates = ("concepts", "nodes", "entities", "memories", "facts")
    edge_candidates = ("relations", "edges", "links", "associations")

    node_table = next((t for t in node_candidates if t in tables), None)
    edge_table = next((t for t in edge_candidates if t in tables), None)

    if node_table is None:
        return _empty()

    try:
        cols = [c[1] for c in con.execute(f"PRAGMA table_info('{node_table}')").fetchall()]
    except sqlite3.Error:
        return _empty()
    if not cols:
        return _empty()

    id_col = "id" if "id" in cols else cols[0]
    label_col = next(
        (c for c in ("label", "name", "title", "text", "content", "summary") if c in cols),
        id_col,
    )

    try:
        rows = con.execute(
            f"select {id_col}, {label_col} from '{node_table}' limit ?",
            (MAX_CONCEPT_NODES,),
        ).fetchall()
    except sqlite3.OperationalError:
        return _empty()

    nodes: list[dict] = []
    valid_ids: set[str] = set()
    for nid, label in rows:
        sid = str(nid)
        valid_ids.add(sid)
        text = str(label) if label is not None else sid
        nodes.append(
            {
                "id": sid,
                "label": (text[:80] + "…") if len(text) > 80 else text,
                "group": node_table,
            }
        )

    edges: list[dict] = []
    if edge_table is not None and valid_ids:
        try:
            ecols = [c[1] for c in con.execute(f"PRAGMA table_info('{edge_table}')").fetchall()]
        except sqlite3.Error:
            ecols = []
        src_col = next((c for c in ("source", "src", "from_id", "a", "head") if c in ecols), None)
        tgt_col = next((c for c in ("target", "dst", "to_id", "b", "tail") if c in ecols), None)
        if src_col and tgt_col:
            try:
                erows = con.execute(
                    f"select {src_col}, {tgt_col} from '{edge_table}' limit ?",
                    (MAX_CONCEPT_EDGES,),
                ).fetchall()
            except sqlite3.OperationalError:
                erows = []
            for s, t in erows:
                ss, ts = str(s), str(t)
                if ss in valid_ids and ts in valid_ids and ss != ts:
                    edges.append({"source": ss, "target": ts, "weight": 1})

    return {"nodes": nodes, "edges": edges}


@router.get("/api/graph/concepts")
async def graph_concepts() -> JSONResponse:
    """KNOWLEDGE graph from the ingested study vault (notes + concepts).

    Source of truth is the knowledge_* tables in memory_core.db, populated by
    knowledge.py's ingestor. If those tables are unbuilt/empty but notes exist
    on disk, we fall back to scanning ~/subjects directly so the
    view still lights up. With no notes at all we return a clean empty graph.

    Read-only and capped (~300 nodes / ~800 edges). Never errors.
    """
    # Primary: the ingested knowledge tables.
    con = _ro_connect(MEMORY_CORE_DB)
    if con is not None:
        try:
            result = _concepts_from_knowledge(con)
        finally:
            con.close()
        # Tables present AND non-empty -> use them. Present-but-empty (or
        # absent) -> fall through to a live vault scan.
        if result is not None and result.get("nodes"):
            return JSONResponse(result)

    # Fallback: scan the vault directly (clean empty graph if no notes).
    return JSONResponse(_concepts_from_vault_scan())


@router.get("/api/graph/code")
async def graph_code() -> JSONResponse:
    """Code-symbol graph from codegraph.db (the dashboard's own source).

    This is the OLD /api/graph/concepts behaviour, preserved verbatim under a
    clearly-named route: functions/methods/classes as nodes, calls/imports as
    edges, capped (~300 nodes / ~800 edges). Falls back to a best-effort read
    of memory_core.db. Returns an empty graph rather than erroring.
    """
    # Prefer the codegraph DB — it has rich symbol + relation data.
    con = _ro_connect(CODEGRAPH_DB)
    if con is not None:
        try:
            result = _concepts_from_codegraph(con)
        finally:
            con.close()
        if result is not None and result.get("nodes"):
            return JSONResponse(result)

    # Fall back to memory_core.db.
    con = _ro_connect(MEMORY_CORE_DB)
    if con is not None:
        try:
            result = _concepts_from_memory_core(con)
        finally:
            con.close()
        if result is not None:
            return JSONResponse(result)

    return JSONResponse(_empty())
