# Architecture

A ~10k-line Python backend serving a ~9k-line single-page Alpine app. No build
step, no framework, no client-side router. The whole UI is one `index.html`
with `x-data="app()"` at the root; navigation is plain state mutation.

This doc is the orientation map. The README has the install/config story; this
one explains *how the pieces fit*.

## Request flow

```
Browser (Alpine SPA)
   │   loads /static/index.html + app.js (+ vendored libs, all same-origin)
   │
   ▼
FastAPI (main.py) at MC_HOST:MC_PORT
   │
   ├─ /api/overview, /api/agents/*, /api/research/*, /api/quiz/*, /api/tracker/*, …
   │     -> open one of HERMES_HOME/*.db (read-only, mode=ro)
   │     -> shape into JSON for the relevant Alpine tab
   │
   ├─ /api/upload, /api/agents/run, /api/research/run, …
   │     -> subprocess [HERMES_PYTHON, "-m", "hermes_cli", ...]
   │     -> stream stdout, persist task id, return for the Agents tab to follow
   │
   ├─ /api/obsidian/*  -> walk OBSIDIAN_VAULT, parse markdown, backlinks
   ├─ /api/notebook/*  -> proxy to Open-Notebook on :5055
   ├─ /api/terminal/ws -> WebSocket bridge to a pty (terminal tab)
   ├─ /api/tts/*       -> optional Kokoro TTS subprocess (read-aloud)
   └─ /api/system/health -> probe each DB, process, mount, integration
```

All external paths (DBs, vault, research dir, Hermes Python) come from
[`config.py`](../config.py), which reads env vars with sensible defaults. The
SPA never knows or cares — it just hits `/api/...`.

## The two-interpreter pattern

The dashboard runs alongside a self-hosted [Hermes](https://github.com) agent
install. Hermes ships frequent updates that rebuild *its* venv. To keep the web
server immune to that, two interpreters coexist:

- **Web server venv** — `<repo>/.venv`, built by `install.sh` from
  `requirements.txt`. Four runtime deps: `fastapi`, `uvicorn[standard]`,
  `aiofiles`, `httpx`, `python-multipart`.
- **Hermes venv** — `$HERMES_PYTHON`, used **only** as a subprocess target when
  the dashboard fires an agent oneshot (those need to `import hermes_cli`).

`mission-control.service.example` runs uvicorn from `.venv/bin/python` — a
`hermes update` cannot break the server.

## Graceful degradation

The dashboard is the front-end *over* Hermes; it is not Hermes. A clone on a
machine with no Hermes install should still start and render. The pattern:

```python
# config.py
def safe_connect(db_path, *, read_only=True):
    p = Path(db_path)
    if not p.is_file():
        return None
    ...

# in a route handler:
conn = safe_connect(config.AGENT_LOG_DB)
if conn is None:
    return {"agents": [], "totals": {...zeroed...}}
```

Every Hermes-backed endpoint guards its DB open with `safe_connect`; when the
file is absent it returns a zeroed payload, and the Alpine tab renders its
empty state. **No** module-level DB opens that crash startup.

Optional integrations (Open-Notebook on `:5055`, Kokoro TTS, Anki export) are
treated the same way: probe → degrade.

## The SPA shell

`static/index.html` defines five **groups**, each with a list of pages. Some
pages have sub-tabs. The shape lives in `static/app.js` (around line 67):

```js
this.nav = [
  { group: "Workspace", items: [ {id:"briefing",...}, {id:"overview",...}, ... ] },
  { group: "Study",     items: [ {id:"upload",...}, {id:"library", subTabs:[...]}, ... ] },
  { group: "Plan",      items: [ {id:"tracker", subTabs:[...]}, {id:"planner", subTabs:[...]} ] },
  { group: "Knowledge", items: [ {id:"notebook", subTabs:[...]}, {id:"obsidian", subTabs:[...]}, {id:"memory",...} ] },
  { group: "System",    items: [ {id:"system",...}, {id:"stats",...}, {id:"graph",...}, {id:"terminal",...}, {id:"design-reference",...} ] },
];
```

A pill-nav across the top selects the group; a subnav below it shows the
group's pages. Each Alpine tab is a `<section x-show="page === '<id>'">` in
`index.html`. State (current page, sub-tab, every tab's data) lives in the
single `app()` Alpine component, and `goTo(id)` is just `this.page = id`.

No router, no build, no JSX. The trade-off is one big `app.js` (~5200 lines)
and one big `index.html` (~4200 lines), but they are *just files*: open them
and read them in order.

## Volt — the design system

The visual language is "Volt · OLED Lime". One tokens block at the top of
`static/style.css` is the single source of truth; the in-app
`/design-reference` page renders live samples of every component.

**DNA:**

- **OLED-black canvas** (`#000`). Cards lift to `#101010` / `#161616`; hairline
  borders at `rgba(255,255,255,.09)`.
- **One lime card per view.** Lime `#c8ff00` is the focal anchor. There is
  exactly one fully-lime-filled card per page (e.g. the briefing's "Today" tile)
  with black ink on it. Multiplying it dilutes it.
- **Companion hues with a job.** Emerald `#2be08a` = positive / mastered.
  Cyan `#36d6e7` = info. Violet `#b08cff` = special / category. Amber
  `#ffc24b` = warning / due. Coral `#ff6b81` = danger / overdue. Never a sixth
  semantic hue.
- **Texture fills.** Diagonal **hatch** on inactive bars and dark-over-lime
  shapes; **dot stipple** on active/highlighted shapes. Bars are never flat.
- **Geometry.** Cards 18px radius, pill-rounded bars, circle buttons.
- **Type.** Inter for UI / body / numerals; Fraunces (editorial italic display)
  for hero headings and the sheen-overlay moments; JetBrains Mono for eyebrow
  labels and code. All self-hosted in `static/fonts/`.
- **Motion.** Ambient and GPU-cheap — drifting graticule, breathing contour
  isolines, parallax constellation canvas. All transform / opacity. Honors
  `prefers-reduced-motion` (freezes) and `prefers-reduced-transparency` (glass
  falls back to solid).

**Token rule.** Chart code reads token *names* at draw time
(`--bg`, `--ink`, `--ink-muted`, `--accent`, `--accent-hi`, `--soft-1`).
Renaming any of those will silently break the charts. Re-coloring the *values*
recolors the charts for free.

The full token table and live components are at `/design-reference`.

## The tracker

The **Plan · Tracker** tabs are a small study-planning engine, not just a UI.
`roadmap_spec.py` is a dict-of-dicts describing exam dates, phases, batch
windows, recurring tests, per-phase tasks, and a `LABELS` table for framing
strings. `tracker.py` turns that spec into a phase → week → day plan:

```
roadmap_spec.py (or roadmap_private.py override)
   │
   ▼
tracker.build_plan()
   │   computes day-by-day blocks (subjects · types · batch tags · tests · admin tasks)
   ▼
   plan.json (cached on disk under ~/.hermes or repo-local)
   │
   ▼
   /api/tracker/today  →  Plan · Tracker · Today
   /api/tracker/plan   →  Plan · Tracker · Roadmap
   /api/tracker/stats  →  Plan · Tracker · Stats  (adherence rings, per-subject readiness)
```

The shipped sample is generic (exam in 2027, four phases: warm-up → build →
sprint → finals). The override hook (`from roadmap_private import *`, gitignored)
lets the author keep a real personal plan local without forking the file.

## Codebase knowledge graph

`graph.py` and the **System · Knowledge Graph** tab read a small `codegraph`
SQLite DB (an AST extraction of this repo). Cytoscape force-laid in the
browser. Off in the default install (the DB is gitignored); generate one with
the codegraph tool of your choice and point `config.CODEGRAPH_DB` at it.

## Operational notes

- **Bind address.** `127.0.0.1` by default. The author runs it behind Tailscale
  (`MC_HOST=<tailnet-ip>`). Do **not** bind to `0.0.0.0` on an untrusted
  network — there is no auth layer; the in-browser terminal is a real shell.
- **Persistence.** The dashboard is mostly a read-only viewer over Hermes' DBs.
  The few write surfaces are: planner tasks, sticky notes, pomodoro counter,
  profile avatar — all in dashboard-local SQLite files under `$HERMES_HOME` or
  the repo dir. None of those are committed.
- **Logs.** uvicorn writes to stdout; under systemd that goes to the journal
  (`journalctl --user -u mission-control -f`).

## Files in this repo, by responsibility

| Concern | File(s) |
|---|---|
| Web server entry + all routes | `main.py` |
| Path / host / DB / interpreter config | `config.py` |
| Hermes-backed reads | `stats.py`, `system_health.py`, `tools.py`, `orchestrator.py`, `automation_hooks.py` |
| Tracker engine | `tracker.py`, `roadmap_spec.py` (`roadmap_private.py` override) |
| Memory / knowledge | `memory.py`, `memory_bridge.py`, `knowledge.py` |
| Notebook + research | `notebooklm.py`, `nlm_download.py` |
| Graph / stats endpoints | `graph.py`, `stats.py` |
| Terminal tab | `terminal.py` |
| Optional integrations | `tts.py`, `voice.py`, `anki.py`, `fetch_fonts.py` |
| SPA shell, state, styles | `static/index.html`, `static/app.js`, `static/style.css` |
| Ambient motion | `static/aurora.js` |
| Live design-system spec | `design-reference/`, served at `/design-reference` |
| Packaging | `requirements.txt`, `install.sh`, `mission-control.service.example`, `.env.example` |
| Durability of the upstream Hermes install | `hermes-customizations/` |
