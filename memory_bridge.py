"""
Mission Control — Memory ⇄ Wiki bridge (the "know-me" recall layer).

This module unifies three stores into ONE persistent, study-free knowledge
layer that both the dashboard and the Hermes agent recall from:

  1. memory_core.db   — dashboard capture buffer (CoALA tiers, episodic/semantic…)
  2. memory_store.db  — Hermes-owned holographic fact store (trust-scored facts)
  3. The Obsidian LLM Wiki (~/Desktop/LLM Wiki) — durable, human-readable hub

Data flow (loop-safe, idempotent):

  durable non-study items (memory_core)  ─┐
  high-trust non-study facts (memory_store)├─▶ wiki/memory/*.md   (human-readable)
                                           └─▶ USER.md managed block (Hermes always-on profile)
  human-authored vault know-me pages       ─▶ memory_store facts   (Hermes query-time recall)

A single `is_study()` classifier gates every write into the wiki + memory layer,
so study content (exam prep, quizzes, flashcards, the academic hub)
never enters the know-me layer — it is routed to NotebookLM instead.

The module is import-safe: if the Hermes holographic store can't be imported
(e.g. hermes-agent moved), the holographic half degrades and the rest keeps
working. Nothing here raises on import.

CLI:  python memory_bridge.py {run|profile|status|study-scan}
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

# ---------------------------------------------------------------------------
# Paths (resolved centrally in config.py)
# ---------------------------------------------------------------------------
HERMES_HOME = config.HERMES_HOME
MEMORY_CORE_DB = HERMES_HOME / "memory_core.db"
MEMORY_STORE_DB = HERMES_HOME / "memory_store.db"
USER_MD = HERMES_HOME / "memories" / "USER.md"

VAULT = config.LLM_WIKI_VAULT
WIKI_MEM_DIR = VAULT / "wiki" / "memory"
INDEX_MD = VAULT / "index.md"
LOG_MD = VAULT / "log.md"

MAIN_OBSIDIAN = config.MAIN_OBSIDIAN

# Claude Code ⇄ Hermes pairing (delegation backend).
#   forward : Claude Code's curated memory  -> a know-me vault page -> agents pool
#             (so the dev / System-Builder lanes recall what Claude learned).
#   reverse : Hermes's know-me facts -> a file the hermes-claude launcher injects
#             into every delegated run's system prompt (one shared picture).
# The bridge only ever touches the AGENTS pool (this memory_store.db), never the
# personal profiles/dhairya pool — the scope wall is preserved on both sides.
CLAUDE_PROJECTS = config.CLAUDE_PROJECTS
FROM_CLAUDE_PAGE = WIKI_MEM_DIR / "from-claude-code.md"
SHARED_DIR = HERMES_HOME / "shared"
KNOWME_FOR_CLAUDE = SHARED_DIR / "hermes-knowme-for-claude.md"
KNOWME_FOR_CLAUDE_BUDGET = 4500  # chars; keep the injected system-prompt block small

# Managed-block markers (USER.md) — everything between them is bridge-owned.
PROFILE_BEGIN = "<!-- BEGIN hermes-knowme (auto-generated from LLM Wiki — do not edit inside) -->"
PROFILE_END = "<!-- END hermes-knowme -->"
PROFILE_CHAR_BUDGET = 850  # keep the managed block well under config user_char_limit (1375)
# Hermes's builtin memory store splits USER.md into entries on this delimiter and
# enforces the char limit PER ENTRY (an over-limit entry blocks future writes and
# a threat-flagged entry is dropped whole). So the managed block is kept as its
# own entry, separated from the rest with the delimiter.
ENTRY_DELIMITER = "\n§\n"

# Frontmatter marker that tags bridge-generated vault pages (so the reverse
# sync never re-imports our own output as "human-authored" knowledge).
SYNC_ORIGIN = "hermes-memory-sync"

# ---------------------------------------------------------------------------
# Study classifier — the single source of truth for "is this study content?"
# ---------------------------------------------------------------------------
_STUDY_SUBJECTS = {
    "biology", "bio", "chemistry", "chem", "physics", "phy", "phys",
    "math", "maths", "mathematics", "cs", "computer science", "cuet",
    "iiser", "neet", "jee", "boards", "board", "science",
}
_STUDY_KEYWORDS = {
    "exam", "quiz", "flashcard", "flash card", "spaced repetition", "sr_card",
    "srs", "fsrs", "syllabus", "mock test", "revision", "mcq", "ncert",
    "previous year", "pyq", "past paper", "semester", "lecture", "tutorial",
    "homework", "assignment", "study session", "study dashboard", "error book",
    "academic", "academics", "chapter ", "marks", "mark scheme",
}
_STUDY_SOURCES = {"ai-tutor", "tutor", "quizmaster", "scholar", "study"}
_STUDY_PATH_MARKERS = ("01 academics", "/study", "study_obsidian", "/academics", "academic_map")

_WORD_RE = re.compile(r"[a-z0-9]+")

# Strong academic-CONTENT signals — these win outright (real study material).
_STRONG_STUDY_RE = re.compile(
    r"\b(exams?|neet|iiser|jee|cuet|boards?|syllabus|mock tests?|"
    r"past papers?|previous year|pyq|ncert|midterm|finals?|"
    r"question paper|answer key|mark scheme)\b"
)
# Agent/system/infra markers. Per the user's policy ("AI/agent/brain → vault,
# study → NotebookLM"), a fact ABOUT the agent system is NOT study even if it
# happens to mention a study tool (quizmaster, flashcard decks, etc.). This
# exemption fires only AFTER the strong-content check above.
_AGENT_INFRA_RE = re.compile(
    r"\b(profiles?|soul\.md|discord|channel|gateway|infra|self-heal|persona|"
    r"agents?|hermes|logging|config|capability|capabilities|mcp|systemd|cron|"
    r"rclone|docker|sqlite|repo|architecture|format|deck format|companion|"
    r"daemon|webhook|api|endpoint|dashboard|router)\b"
)


def is_study(
    text: str = "",
    *,
    subject: str = "",
    source: str = "",
    tags: str = "",
    path: str = "",
) -> bool:
    """Return True if the content is study/academic CONTENT and must be kept
    OUT of the know-me wiki + memory layer (routed to NotebookLM instead).

    Order of decision:
      1. Structural study scoping (study source, a `subject` tag, study path) → study.
      2. Strong academic-content signals (exam, NEET, syllabus, …) → study.
      3. Agent/system/infra fact (AI-about-itself) → NOT study (policy: → vault).
      4. Weaker keyword / subject-word hits → study.
    """
    if source and source.strip().lower() in _STUDY_SOURCES:
        return True
    if subject and subject.strip():
        return True
    p = (path or "").lower()
    if any(m in p for m in _STUDY_PATH_MARKERS):
        return True
    blob = " ".join(x for x in (text, tags) if x).lower()
    if not blob:
        return False
    if _STRONG_STUDY_RE.search(blob):
        return True
    if _AGENT_INFRA_RE.search(blob):
        return False  # AI/agent/system content → belongs in the vault, not study
    if any(kw in blob for kw in _STUDY_KEYWORDS):
        return True
    if set(_WORD_RE.findall(blob)) & _STUDY_SUBJECTS:
        return True
    return False


# Obvious non-knowledge noise that should never become "about-me" content.
_NOISE = ("placeholder", "test fact", "lorem ipsum")


def _is_noise(content: str) -> bool:
    c = (content or "").strip().lower()
    return (not c) or (c in _NOISE) or len(c) < 8


# Raw-transcript / system artifacts that pollute auto-extracted fact stores.
# A know-me fact must be a clean, single-line declarative statement — not a
# chat fragment, system note, process log, or document dump.
_NOISE_SUBSTR = (
    "system note", "background process", "matched watch pattern", "gateway restart",
    "the user sent a text document", "content of message", "smoke test",
    "matched output", "interrupted by", "document_cache", "proc_", "doc_",
    "ytdl", "mpv ", "http://", "https://", ".txt", "previous turn",
)


def _is_quality_fact(content: str) -> bool:
    """True for a legitimate fact suitable for the know-me layer. Drops raw
    chat turns, system notes, process logs, and document dumps — but keeps
    detailed multi-clause facts (agent config, preferences, etc.)."""
    c = (content or "").strip()
    if len(c) < 10 or len(c) > 600:
        return False
    if c[0] in "[>`":             # system note / Discord [TAG] / quote / code fence
        return False
    if c.count("\n") > 3:         # transcript / multi-paragraph dump
        return False
    cl = c.lower()
    if any(n in cl for n in _NOISE):
        return False
    if any(s in cl for s in _NOISE_SUBSTR):
        return False
    return True


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Holographic store (Hermes memory_store.db) — optional import
# ---------------------------------------------------------------------------
_HERMES_ROOT = str(config.HERMES_AGENT_ROOT)


def _memory_store():
    """Return a MemoryStore bound to memory_store.db, or None if unavailable.

    Import is best-effort: the dashboard must never fail to import this module
    just because the Hermes agent tree moved."""
    try:
        if _HERMES_ROOT not in sys.path:
            sys.path.insert(0, _HERMES_ROOT)
        from plugins.memory.holographic.store import MemoryStore  # type: ignore
        return MemoryStore(db_path=str(MEMORY_STORE_DB))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def pull_core_durable(limit: int = 500) -> list[dict[str, Any]]:
    """Durable, non-study items from memory_core.db (the dashboard buffer).

    Durable = semantic/procedural of any salience, or episodic with salience
    >= 0.6. Study-scoped rows are excluded."""
    out: list[dict[str, Any]] = []
    if not MEMORY_CORE_DB.exists():
        return out
    try:
        conn = sqlite3.connect(str(MEMORY_CORE_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM memory_items
               WHERE kind IN ('semantic','procedural')
                  OR (kind='episodic' AND salience >= 0.6)
               ORDER BY salience DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return out
    for r in rows:
        d = dict(r)
        if is_study(d.get("content", ""), subject=d.get("subject", ""),
                    source=d.get("source", ""), tags=d.get("tags", "")):
            continue
        if _is_noise(d.get("content", "")):
            continue
        out.append(d)
    return out


def pull_store_facts(min_trust: float = 0.3, limit: int = 1000) -> list[dict[str, Any]]:
    """Non-study, human/agent-authored facts from memory_store.db.

    Excludes facts the bridge itself synced back in (tag 'vault-sync'), so the
    vault→store→vault path can't loop."""
    store = _memory_store()
    if store is None:
        return []
    try:
        facts = store.list_facts(min_trust=min_trust, limit=limit)
    except Exception:
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass
    out = []
    seen_prefix: set[str] = set()
    for f in facts:
        content = f.get("content", "")
        tags = f.get("tags", "") or ""
        if "vault-sync" in tags:
            continue
        if not _is_quality_fact(content):
            continue
        if is_study(content, tags=tags, source=f.get("category", "")):
            continue
        # Light near-duplicate suppression: collapse to a normalized prefix.
        # list_facts() is trust-desc ordered, so the first-seen variant (kept)
        # is the highest-trust one.
        norm = re.sub(r"[^a-z0-9 ]", "", content.lower())
        norm = re.sub(r"\s+", " ", norm).strip()[:55]
        if norm in seen_prefix:
            continue
        seen_prefix.add(norm)
        out.append(f)
    return out


def scan_study_items() -> dict[str, list[dict[str, Any]]]:
    """Find study content currently sitting in the know-me layer (memory DBs).
    Used to keep the layer study-free and to route study → NotebookLM."""
    found: dict[str, list[dict[str, Any]]] = {"memory_core": [], "memory_store": []}
    # memory_core.db
    if MEMORY_CORE_DB.exists():
        try:
            conn = sqlite3.connect(str(MEMORY_CORE_DB), timeout=10)
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT * FROM memory_items"):
                d = dict(r)
                if is_study(d.get("content", ""), subject=d.get("subject", ""),
                            source=d.get("source", ""), tags=d.get("tags", "")):
                    found["memory_core"].append(d)
            conn.close()
        except sqlite3.Error:
            pass
    # memory_store.db
    store = _memory_store()
    if store is not None:
        try:
            for f in store.list_facts(min_trust=0.0, limit=5000):
                if "vault-sync" in (f.get("tags", "") or ""):
                    continue
                if is_study(f.get("content", ""), tags=f.get("tags", "")):
                    found["memory_store"].append(f)
        except Exception:
            pass
        finally:
            try:
                store.close()
            except Exception:
                pass
    return found


# ---------------------------------------------------------------------------
# Vault materialization (DB → wiki markdown)
# ---------------------------------------------------------------------------
def _fm(title: str, kind: str, *, extra: dict[str, str] | None = None) -> str:
    lines = [
        "---",
        f"title: {title}",
        f"type: {kind}",
        "tags: [topic/know-me, hermes/memory]",
        f"created: {_today()}",
        f"updated: {_today()}",
        "status: auto",
        f"origin: {SYNC_ORIGIN}",
    ]
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _cat_label(cat: str) -> str:
    return {
        "user_pref": "Preferences",
        "general": "General",
        "project": "Projects",
        "vault": "From the wiki",
    }.get(cat, cat.title() if cat else "Other")


def materialize_to_vault(facts: list[dict[str, Any]],
                         core: list[dict[str, Any]]) -> dict[str, Any]:
    """Write the bridge-owned know-me pages under wiki/memory/. Regenerated
    wholesale each run (idempotent; these pages carry origin: hermes-memory-sync)."""
    WIKI_MEM_DIR.mkdir(parents=True, exist_ok=True)

    # --- facts.md: holographic facts grouped by category ---
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        by_cat.setdefault(f.get("category", "general"), []).append(f)
    parts = [_fm("Know-me facts", "semantic"), "",
             "# Know-me facts",
             "",
             f"_Synced from Hermes's fact store ({len(facts)} facts) on {_today()}. "
             "Study content is excluded by policy._", ""]
    for cat in sorted(by_cat):
        parts.append(f"## {_cat_label(cat)}")
        for f in sorted(by_cat[cat], key=lambda x: -float(x.get("trust_score", 0) or 0)):
            txt = (f.get("content", "") or "").strip().replace("\n", " ")
            parts.append(f"- {txt}")
        parts.append("")
    (WIKI_MEM_DIR / "facts.md").write_text("\n".join(parts), encoding="utf-8")

    # --- captures.md: durable dashboard memories (if any) ---
    cparts = [_fm("Captured memories", "episodic"), "",
              "# Captured memories",
              "",
              f"_Durable, non-study items captured via the dashboard "
              f"({len(core)} items)._", ""]
    if core:
        for d in core:
            txt = (d.get("summary") or d.get("content") or "").strip().replace("\n", " ")
            src = d.get("source", "") or "dashboard"
            cparts.append(f"- [{d.get('kind','?')[:3]}] {txt} _({src})_")
    else:
        cparts.append("_No captured memories yet._")
    cparts.append("")
    (WIKI_MEM_DIR / "captures.md").write_text("\n".join(cparts), encoding="utf-8")

    # --- about-dhairya.md: the profile hub ---
    prefs = [f for f in facts if f.get("category") == "user_pref"]
    aparts = [_fm("About Dhairya", "entity"), "",
              "# About Dhairya",
              "",
              "The know-me hub for the Hermes agent — a synthesis of what the "
              "system knows about Dhairya, kept current by the memory bridge. "
              "See [[facts]] for the full fact list and [[captures]] for the "
              "dashboard capture log.", ""]
    if prefs:
        aparts.append("## Preferences & working style")
        for f in sorted(prefs, key=lambda x: -float(x.get("trust_score", 0) or 0))[:12]:
            aparts.append(f"- {(f.get('content','') or '').strip()}")
        aparts.append("")
    aparts.append("## Links")
    aparts.append("- [[facts|Know-me facts]] · [[captures|Captured memories]] · "
                  "[[overview|Wiki overview]]")
    aparts.append("")
    (WIKI_MEM_DIR / "about-dhairya.md").write_text("\n".join(aparts), encoding="utf-8")

    _update_index()
    _append_log(len(facts), len(core))
    return {"pages": ["about-dhairya.md", "facts.md", "captures.md"],
            "facts": len(facts), "captures": len(core)}


def _update_index() -> None:
    """Refresh a managed '## Memory (Hermes)' section in the vault index.md."""
    if not INDEX_MD.exists():
        return
    try:
        text = INDEX_MD.read_text(encoding="utf-8")
    except OSError:
        return
    section = (
        "## Memory (Hermes know-me)\n"
        "- [[about-dhairya|About Dhairya]] — synthesized know-me profile.\n"
        "- [[facts|Know-me facts]] — Hermes fact store (study-free).\n"
        "- [[captures|Captured memories]] — durable dashboard captures.\n"
    )
    begin = "<!-- BEGIN hermes-memory-index -->"
    end = "<!-- END hermes-memory-index -->"
    block = f"{begin}\n{section}{end}"
    if begin in text and end in text:
        text = re.sub(re.escape(begin) + r".*?" + re.escape(end), block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    INDEX_MD.write_text(text, encoding="utf-8")


def _append_log(n_facts: int, n_core: int) -> None:
    if not LOG_MD.exists():
        return
    try:
        text = LOG_MD.read_text(encoding="utf-8")
    except OSError:
        return
    entry = (f"\n## [{_today()}] maintain | memory sync\n"
             f"- Materialized {n_facts} know-me facts + {n_core} captures into wiki/memory/.\n"
             f"- Refreshed USER.md profile block and holographic vault facts.\n")
    LOG_MD.write_text(text.rstrip() + "\n" + entry, encoding="utf-8")


# ---------------------------------------------------------------------------
# USER.md profile (vault → Hermes always-on recall)
# ---------------------------------------------------------------------------
def build_user_profile(facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Write a concise managed 'know-me' block into ~/.hermes/memories/USER.md.

    This is the Hermes built-in memory's always-on user profile, so it must
    stay under the injection budget. Everything outside the markers (e.g. the
    user's own policy line) is preserved verbatim."""
    # Rank: preferences first, then project, then general; by trust.
    order = {"user_pref": 0, "project": 1, "general": 2}
    ranked = sorted(
        facts,
        key=lambda f: (order.get(f.get("category", "general"), 3),
                       -float(f.get("trust_score", 0) or 0)),
    )
    bullets: list[str] = []
    used = 0
    for f in ranked:
        c = (f.get("content", "") or "").strip().replace("\n", " ")
        if _is_noise(c):
            continue
        line = f"- {c}"
        if len(line) > 200:
            line = line[:197] + "…"
        if used + len(line) + 1 > PROFILE_CHAR_BUDGET:
            break
        bullets.append(line)
        used += len(line) + 1

    body = (
        f"{PROFILE_BEGIN}\n"
        "**About Dhairya (know-me — synced from the LLM Wiki vault):**\n"
        + ("\n".join(bullets) if bullets else "- (no know-me facts yet)")
        + f"\n_Source: ~/Desktop/LLM Wiki · synced {_today()}_\n"
        f"{PROFILE_END}"
    )

    USER_MD.parent.mkdir(parents=True, exist_ok=True)
    existing = USER_MD.read_text(encoding="utf-8") if USER_MD.exists() else ""
    # Strip any prior managed block, then keep the remaining user-authored
    # content as a SEPARATE entry (split on the delimiter) so each entry stays
    # under the per-entry char limit and threat-isolation is per-entry.
    rest = re.sub(re.escape(PROFILE_BEGIN) + r".*?" + re.escape(PROFILE_END),
                  "", existing, flags=re.S)
    rest = rest.strip().strip("§").strip()
    new = body + (ENTRY_DELIMITER + rest if rest else "")
    if not new.endswith("\n"):
        new += "\n"
    USER_MD.write_text(new, encoding="utf-8")
    return {"profile_chars": len(body), "bullets": len(bullets)}


# ---------------------------------------------------------------------------
# Vault → holographic store (human-authored vault knowledge → Hermes facts)
# ---------------------------------------------------------------------------
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


def _is_knowme_page(text: str) -> bool:
    """A vault page is a reverse-sync candidate only if it explicitly carries
    the `topic/know-me` (or `topic/personal`) tag AND is not bridge-generated.
    Requiring the tag — not a loose word match — keeps reference/seed content
    (e.g. the Karpathy pages, which all mention "personal knowledge base") out
    of Hermes's fact store."""
    if f"origin: {SYNC_ORIGIN}" in text:
        return False
    head = text[:800].lower()
    return ("topic/know-me" in head or "topic/personal" in head
            or "topic/about-me" in head)


def sync_vault_to_store() -> dict[str, Any]:
    """Import salient bullets from HUMAN-authored know-me vault pages into the
    holographic fact store (tag 'vault-sync'), so Hermes recalls them at query
    time. Only pages tagged know-me/personal are scanned; bridge-generated and
    reference/seed pages are skipped."""
    store = _memory_store()
    if store is None:
        return {"upserted": 0, "store": False}
    upserted = 0
    scanned = 0
    try:
        for md in sorted(VAULT.glob("wiki/**/*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            if not _is_knowme_page(text):
                continue
            rel = str(md.relative_to(VAULT))
            if is_study(text, path=rel):
                continue
            scanned += 1
            for line in text.splitlines():
                m = _BULLET_RE.match(line)
                if not m:
                    continue
                fact = m.group(1).strip()
                # strip wikilink syntax for cleaner facts
                fact = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", fact)
                if _is_noise(fact) or len(fact) > 300:
                    continue
                if is_study(fact):
                    continue
                try:
                    store.add_fact(fact, category="vault", tags="vault-sync")
                    upserted += 1
                except Exception:
                    pass
    finally:
        try:
            store.close()
        except Exception:
            pass
    return {"upserted": upserted, "pages_scanned": scanned, "store": True}


# ---------------------------------------------------------------------------
# Claude Code ⇄ Hermes pairing
# ---------------------------------------------------------------------------
_FM_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.M)
_FM_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.M)


def _parse_claude_memory(text: str) -> dict[str, str]:
    """Pull {name, description, body} out of a Claude Code memory file.

    Claude memory files are `--- frontmatter --- \\n body`. The `description`
    is a clean one-line summary (ideal as a fact); the body is the full note."""
    name = desc = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            body = text[end + 4:].strip()
            m = _FM_DESC_RE.search(fm)
            if m:
                desc = m.group(1).strip().strip('"').strip("'")
            m = _FM_NAME_RE.search(fm)
            if m:
                name = m.group(1).strip()
    return {"name": name, "description": desc, "body": body}


def import_claude_memory() -> dict[str, Any]:
    """FORWARD: materialize Claude Code's curated memory files into a single
    know-me vault page (`from-claude-code.md`, tagged topic/know-me). The
    existing vault→store sync then carries those bullets into the agents pool,
    so Hermes's dev / System-Builder lanes recall what Claude learned.

    Idempotent: the page is regenerated wholesale each run. Study content and
    low-quality lines are gated out. The page deliberately omits the
    `hermes-memory-sync` origin so the reverse vault→store pass ingests it."""
    if not CLAUDE_PROJECTS.exists():
        return {"facts": 0, "page": False}
    seen: set[str] = set()
    bullets: list[str] = []
    for md in sorted(CLAUDE_PROJECTS.glob("*/memory/*.md")):
        if md.name == "MEMORY.md":  # that's the index, not a fact
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = _parse_claude_memory(text)
        fact = parsed["description"] or parsed["body"].splitlines()[0] if parsed["body"] else ""
        fact = fact.strip()
        if not fact or _is_noise(fact) or not _is_quality_fact(fact):
            continue
        if is_study(fact, path=str(md)):
            continue
        norm = re.sub(r"[^a-z0-9 ]", "", fact.lower())[:60]
        if norm in seen:
            continue
        seen.add(norm)
        bullets.append(f"- {fact}")

    WIKI_MEM_DIR.mkdir(parents=True, exist_ok=True)
    parts = [
        _fm("From Claude Code", "semantic"),
        "",
        "# From Claude Code",
        "",
        "_What the delegated Claude Code agent has learned about Dhairya's "
        f"projects and systems ({len(bullets)} facts), synced on {_today()}. "
        "These feed Hermes's shared know-me layer so both agents share one "
        "picture. Study content is excluded by policy._",
        "",
    ]
    parts.extend(bullets if bullets else ["_No Claude Code memory yet._"])
    parts.append("")
    # NOTE: _fm() stamps `tags: [topic/know-me, hermes/memory]`, so this page is
    # picked up by sync_vault_to_store(); but _fm also stamps origin:
    # hermes-memory-sync which that function SKIPS. We must strip that origin so
    # the page is treated as ingestable know-me content.
    page = "\n".join(parts).replace(f"origin: {SYNC_ORIGIN}\n", "")
    FROM_CLAUDE_PAGE.write_text(page, encoding="utf-8")
    return {"facts": len(bullets), "page": True}


def export_for_claude(facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """REVERSE: write the shared know-me file that the hermes-claude launcher
    injects into every delegated Claude run's system prompt, so Claude shares
    Hermes's picture of Dhairya and his systems.

    Sourced from `pull_store_facts()`, which EXCLUDES vault-sync facts — so
    Claude's own contributions (which land tagged vault-sync) are never fed
    back to it, and only Hermes-side knowledge flows in. Agents pool only;
    personal-pool facts are never read here."""
    if facts is None:
        facts = pull_store_facts()
    order = {"user_pref": 0, "project": 1, "general": 2, "vault": 3}
    label = {"user_pref": "Preferences & working style", "project": "Projects",
             "general": "General", "vault": "From the wiki"}
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        by_cat.setdefault(f.get("category", "general"), []).append(f)

    head = (
        "# What Hermes knows — shared context for delegated Claude Code runs\n\n"
        "_Auto-generated from Hermes's know-me layer and injected into your system "
        "prompt by the `hermes-claude` launcher. You are running as a sub-agent that "
        "Hermes delegated to; this is the same picture Hermes has of Dhairya and his "
        "systems, so you both share one context. Treat it as background knowledge — "
        "do not repeat it back verbatim._\n"
    )
    out = [head]
    used = len(head)
    for cat in sorted(by_cat, key=lambda c: order.get(c, 9)):
        rows = sorted(by_cat[cat], key=lambda x: -float(x.get("trust_score", 0) or 0))
        section = [f"\n## {label.get(cat, cat.title())}"]
        for f in rows:
            c = (f.get("content", "") or "").strip().replace("\n", " ")
            if not c:
                continue
            line = f"- {c}"
            if used + len(line) + 1 > KNOWME_FOR_CLAUDE_BUDGET:
                break
            section.append(line)
            used += len(line) + 1
        if len(section) > 1:
            out.append("\n".join(section))
        if used >= KNOWME_FOR_CLAUDE_BUDGET:
            break

    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    body = "\n".join(out).rstrip() + "\n"
    KNOWME_FOR_CLAUDE.write_text(body, encoding="utf-8")
    return {"chars": len(body), "path": str(KNOWME_FOR_CLAUDE)}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_all() -> dict[str, Any]:
    """Full bidirectional sync. Returns a JSON-able report."""
    facts = pull_store_facts()
    core = pull_core_durable()
    claude_in = import_claude_memory()      # Claude Code memory -> know-me vault page
    vault_report = materialize_to_vault(facts, core)
    profile_report = build_user_profile(facts)
    store_report = sync_vault_to_store()    # vault (incl. from-claude-code) -> agents pool
    # Re-pull AFTER vault→store so Claude's freshly-imported facts are included
    # in the picture exported back to Claude on the next delegated run.
    claude_out = export_for_claude()
    study = scan_study_items()
    return {
        "ok": True,
        "ts": _now().isoformat(),
        "vault": vault_report,
        "profile": profile_report,
        "vault_to_store": store_report,
        "claude_pairing": {
            "imported_from_claude": claude_in.get("facts", 0),
            "exported_for_claude_chars": claude_out.get("chars", 0),
        },
        "study_remaining_in_memory": {
            "memory_core": len(study["memory_core"]),
            "memory_store": len(study["memory_store"]),
        },
        "study_free": (len(study["memory_core"]) == 0 and len(study["memory_store"]) == 0),
    }


def status() -> dict[str, Any]:
    """Lightweight status for the dashboard (no writes)."""
    facts = pull_store_facts()
    core = pull_core_durable()
    study = scan_study_items()
    profile_present = USER_MD.exists() and PROFILE_BEGIN in USER_MD.read_text(encoding="utf-8")
    wiki_pages = len(list(VAULT.glob("wiki/**/*.md"))) if VAULT.exists() else 0
    return {
        "ok": True,
        "vault_exists": VAULT.exists(),
        "wiki_pages": wiki_pages,
        "memory_pages": len(list(WIKI_MEM_DIR.glob("*.md"))) if WIKI_MEM_DIR.exists() else 0,
        "knowme_facts": len(facts),
        "durable_captures": len(core),
        "profile_present": profile_present,
        "claude_export_present": KNOWME_FOR_CLAUDE.exists(),
        "claude_import_page_present": FROM_CLAUDE_PAGE.exists(),
        "study_in_memory_core": len(study["memory_core"]),
        "study_in_memory_store": len(study["memory_store"]),
        "study_free": (len(study["memory_core"]) == 0 and len(study["memory_store"]) == 0),
    }


# ---------------------------------------------------------------------------
# Unified search helpers (for the dashboard's combined recall view)
# ---------------------------------------------------------------------------
def search_knowme_facts(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search the holographic know-me fact store (study-free, quality-filtered)."""
    store = _memory_store()
    if store is None:
        return []
    try:
        res = store.search_facts(query, min_trust=0.0, limit=limit * 3)
    except Exception:
        return []
    finally:
        try:
            store.close()
        except Exception:
            pass
    out: list[dict[str, Any]] = []
    for f in res:
        c = f.get("content", "")
        if not _is_quality_fact(c) or is_study(c, tags=f.get("tags", "")):
            continue
        out.append({"content": c, "trust": f.get("trust_score"),
                    "category": f.get("category", "")})
        if len(out) >= limit:
            break
    return out


def search_vault(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Naive term-frequency search over the LLM Wiki (study pages excluded)."""
    toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1]
    if not toks or not VAULT.exists():
        return []
    hits: list[dict[str, Any]] = []
    for md in VAULT.glob("wiki/**/*.md"):
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(md.relative_to(VAULT))
        if is_study(text, path=rel):
            continue
        low = text.lower()
        score = sum(low.count(t) for t in toks)
        if score <= 0:
            continue
        snip = ""
        for line in text.splitlines():
            ls = line.strip()
            if not ls or ls.startswith(("---", "#")):
                continue
            if any(t in ls.lower() for t in toks):
                snip = ls[:180]
                break
        hits.append({"path": rel, "title": md.stem, "score": score, "snippet": snip})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        print(json.dumps(run_all(), indent=2))
    elif cmd == "pair":
        # Just the Claude Code ⇄ Hermes halves (no full vault rebuild).
        ci = import_claude_memory()
        st = sync_vault_to_store()
        co = export_for_claude()
        print(json.dumps({"imported_from_claude": ci, "vault_to_store": st,
                          "exported_for_claude": co}, indent=2))
    elif cmd == "profile":
        print(json.dumps(build_user_profile(pull_store_facts()), indent=2))
    elif cmd == "study-scan":
        s = scan_study_items()
        print(json.dumps({k: [i.get("content", "")[:120] for i in v]
                          for k, v in s.items()}, indent=2))
    elif cmd == "search":
        q = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps({"ok": True, "query": q,
                          "facts": search_knowme_facts(q, 8),
                          "vault": search_vault(q, 8)}))
    else:
        print(json.dumps(status()))
