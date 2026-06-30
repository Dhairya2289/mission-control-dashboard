/* ============================================================
   Mission Control — Alpine app() state
   - Theme toggle (dark default, light override) with localStorage
   - Group/sub-tab navigation with single activeTab string
   - Design Reference upload wired to existing FastAPI endpoints
   - Re-renders ApexCharts on theme change (colours bake at draw time)
   ============================================================ */

// Shared fetch helper used by every feature method below.
// Adds Accept header, JSON-encodes body, throws on non-2xx with body text,
// and never leaves the page hanging on network errors.
async function fetchWithAuth(url, opts) {
  opts = opts || {};
  const headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
  let body = opts.body;
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    headers['Content-Type'] = headers['Content-Type'] || 'application/json';
    body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, Object.assign({}, opts, { headers, body, credentials: 'same-origin' }));
  } catch (e) {
    throw new Error('network: ' + (e && e.message ? e.message : e));
  }
  if (!resp.ok) {
    let txt = '';
    try { txt = await resp.text(); } catch (_) {}
    throw new Error('http ' + resp.status + (txt ? ': ' + txt.slice(0, 300) : ''));
  }
  const ct = resp.headers.get('content-type') || '';
  if (ct.indexOf('application/json') !== -1) {
    return await resp.json();
  }
  return await resp.text();
}

// SVG icons for each agent (key → full <svg> string with currentColor stroke).
// Style matches the upload tab's flow-nodes: 16x16 viewBox, currentColor stroke.
const AGENT_ICONS = {
  bill:       '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polygon points="16 8 13 11 13 16 11 16 11 11 8 8 12 6" fill="currentColor" stroke="none"/></svg>',
  vault:      '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/><circle cx="14" cy="13" r="1.5" fill="currentColor" stroke="none"/></svg>',
  scholar:    '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><path d="M9 7h8M9 11h8M9 15h5" opacity="0.6"/></svg>',
  quizmaster: '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3" stroke-width="1.5"/></svg>',
  planner:    '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/><circle cx="12" cy="14.5" r="1.2" fill="currentColor" stroke="none"/></svg>',
  dev:        '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="8 6 2 12 8 18" stroke-width="2"/><polyline points="16 6 22 12 16 18" stroke-width="2"/><path d="m14 4-4 16" stroke-width="1.5" opacity="0.55"/></svg>',
  default:    '<svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/></svg>',
};

window.app = function () {
  return {
    // ---------- meta ----------
    version: "1.7.0",
    theme: "volt",          // Volt — the single lime/near-black look (legacy themes retired 2026-06-29).
    themes: [
      { id: "volt", label: "Volt", hint: "Lime on near-black" },
    ],
    gatewayOnline: true,    // pinged on init
    _vtActive: false,       // guards against overlapping View Transitions on nav
    _pendingNav: null,      // most-recent nav target queued during an active VT

    // ---------- nav model ----------
    // Each merged destination declares subTabs[]; the first is the default.
    // activeTab is the single source of truth for "what page are we on".
    nav: [
      {
        group: "Workspace",
        items: [
          { id: "briefing",  label: "Briefing",  icon: iconSun() },
          { id: "overview", label: "Overview", icon: iconHome() },
          { id: "agents",   label: "Agents",   icon: iconBolt() },
          { id: "chat",     label: "Chat",     icon: iconChat() },
        ],
      },
      {
        group: "Study",
        items: [
          { id: "upload", label: "Upload", icon: iconUpload() },
          {
            id: "library", label: "Library", icon: iconBook(),
            subTabs: [
              { id: "library-notes",         label: "Notes" },
              { id: "library-lecture-notes", label: "Lecture Notes" },
            ],
          },
          {
            id: "practice", label: "Practice", icon: iconTarget(),
            subTabs: [
              { id: "practice-quiz",       label: "Quiz" },
              { id: "practice-flashcards", label: "Flashcards" },
            ],
          },
          { id: "recall", label: "Recall", icon: iconRecall() },
        ],
      },
      {
        group: "Plan",
        items: [
          {
            id: "tracker", label: "Tracker", icon: iconTracker(),
            subTabs: [
              { id: "tracker-today",   label: "Today" },
              { id: "tracker-roadmap", label: "Roadmap" },
              { id: "tracker-stats",   label: "Stats" },
            ],
          },
          {
            id: "planner", label: "Planner", icon: iconCalendar(),
            subTabs: [
              { id: "planner-schedule", label: "Schedule" },
              { id: "planner-tasks",    label: "Tasks" },
              { id: "planner-focus",    label: "Focus" },
            ],
          },
        ],
      },
      {
        group: "Knowledge",
        items: [
          {
            id: "notebook", label: "NotebookLM", icon: iconNotebook(),
            subTabs: [
              { id: "research",      label: "Research" },
              { id: "nlm-notebooks", label: "Notebooks" },
              { id: "nlm-chat",      label: "Chat" },
              { id: "nlm-studio",    label: "Studio" },
            ],
          },
          {
            id: "obsidian", label: "Obsidian", icon: iconObsidian(),
            subTabs: [
              { id: "obsidian-vault",  label: "Vault" },
              { id: "obsidian-graph",  label: "Brain" },
              { id: "obsidian-search", label: "Search" },
            ],
          },
          { id: "memory", label: "Memory", icon: iconMemory() },
        ],
      },
      {
        group: "System",
        items: [
          { id: "system",           label: "System",          icon: iconPulse() },
          { id: "stats",            label: "Stats",           icon: iconStats() },
          { id: "graph",            label: "Knowledge Graph", icon: iconGraph() },
          { id: "terminal",         label: "Terminal",        icon: iconTerminal() },
          { id: "design-reference", label: "Design Reference", icon: iconLayers() },
        ],
      },
    ],

    activeTab: "overview",

    // Sidebar collapse state — persisted to localStorage.
    // false = full labels visible, true = icon-only with tooltips.
    sidebarCollapsed: false,

    // (i) Mobile off-canvas nav drawer. On phones the sidebar slides in over
    // the content; this flag drives the `nav-open` body class + backdrop.
    mobileNavOpen: false,

    // ---------- overview stats (placeholder until next prompt) ----------
    stats: { runs: "—", subjects: "—", uptime: "—", streak: 5, gatewayLabel: "Gateway online" },

    // ---------- live overview payload ----------
    overview: null,
    ovReadiness: [],            // per-subject readiness for the bento tile
    ovOpenAgent: null,          // which right-rail agent row is expanded

    // ---------- live agents analytics ----------
    agentsAnalytics: null,
    agentFilter: "all",

    // canonical agent palette — kept in sync with backend AGENT_REGISTRY
    AGENT_PALETTE: {
      bill:       "#c8ff00",
      vault:      "#36d6e7",
      scholar:    "#2be08a",
      quizmaster: "#b08cff",
      planner:    "#ffc24b",
      dev:        "#ff6b81",
    },

    // ---------- agent breakdown (legacy from earlier prompt; now driven by overview.agent_breakdown) ----------
    agentBreakdown: [],
    agentBreakdownAnimated: false,

    // ---------- design reference state ----------
    designRef: null,
    picked: [],
    dragOver: false,
    uploading: false,
    uploadResult: "",

    // ---------- upload page state ----------
    subjectOptions: [],
    uploadSubjectMode: "",
    uploadNewSubject: "",
    uploadFile: null,
    uploadDragging: false,
    uploadLoading: false,
    uploadMessage: "",
    uploadError: "",

    // ---------- Knowledge · NotebookLM (CLI-backed: /api/nlm/*) ----------
    nlmHealth: null,           // {ok, cli, auth, bin, version}
    nlmNotebooks: [],          // [{id, title, created_at}]
    nlmSelectedId: "",         // current notebook id
    nlmMeta: null,             // metadata payload for the selected notebook
    nlmSources: [],            // [{id, title, status, ...}]
    nlmArtifacts: [],          // [{id, title, type, status}]
    nlmLoading: false,
    nlmError: "",
    nlmNewTitle: "",
    nlmCreating: false,
    nlmAddKind: "url",         // url | research | youtube | text
    nlmAddValue: "",
    nlmAddMode: "fast",        // research depth: fast | deep
    nlmAdding: false,
    nlmAskInput: "",
    nlmAskLoading: false,
    nlmChatHistory: [],        // [{role, content, citations?, ts}]
    nlmGenType: "audio",
    nlmGenDesc: "",
    nlmGenOptions: {},         // {format, length, difficulty, quantity, ...}
    nlmGenLoading: false,
    nlmPollTimer: null,
    nlmPollCount: 0,
    nlmLanguages: [],
    nlmCurrentLanguage: "en",

    // ---------- Knowledge · Obsidian vault ----------
    obsStatus: null,           // {ok, path, name, note_count, folders}
    obsNotes: [],              // list result
    obsSelectedNote: null,     // current note (raw/html/path/frontmatter)
    obsLoading: false,
    obsError: "",
    obsSearchQ: "",
    obsSearchResults: [],
    obsSearchLoading: false,
    obsFolderFilter: "",
    obsNoteLimit: 60,
    obsView: "list",           // "list" or "reader"
    obsNotesQuery: "",         // client-side filter on the vault list

    // ---------- Knowledge · Memory (unified know-me layer: /api/memory/*) ----------
    memory: {
      knowledge: null,         // {ok, vault_exists, wiki_pages, memory_pages, knowme_facts, durable_captures, profile_present, study_in_memory_core, study_in_memory_store, study_free}
      query: "",
      results: [],             // [{source, kind, title, text, path?, score}]
      loading: false,
      syncing: false,
      searched: false,
      error: "",
    },

    // ---------- Daily Mission Briefing ----------
    dailyBriefing: null,       // {date, generated_at, markdown, stats}
    dailyBriefingLoading: false,
    dailyBriefingError: "",

    // ---------- Global ⌘K command palette ----------
    globalSearchOpen: false,
    globalSearchQ: "",
    globalSearchResults: [],
    globalSearchLoading: false,
    globalSearchError: "",
    _globalSearchTimer: null,
    paletteActiveIdx: 0,        // highlighted row (keyboard nav)
    _paletteActionsCache: null, // memoized local action registry
    _paletteReturnFocus: null,  // element to restore focus to on close (WCAG 2.4.3)
    uploadResponse: null,

    // ---------- research page state ----------
    researchTitle: "",
    researchQuery: "",
    researchLoading: false,
    researchMessage: "",
    researchError: "",
    researchItems: [],
    researchSelected: null,
    researchContent: "",
    researchPollTimer: null,
    researchQuizLoading: false,
    researchQuizMessage: "",
    // "Send to NotebookLM" bridge (Research ▸ pick a notebook ▸ add as source)
    researchSendOpen: false,
    researchSendLoading: false,
    researchSendBusy: false,
    researchSendNotebooks: [],
    researchSendMsg: "",
    researchSendErr: "",

    // ---------- notes library page state ----------
    notes: {
      subject: "",
      items: [],
      selectedId: "",
      content: null,
      html: "",
      loading: false,
      error: "",
    },

    // ---------- schedule page state ----------
    schedule: {
      data: null,
      loading: false,
      error: "",
    },

    // ---------- lecture notes page state ----------
    lecture: {
      items: [],
      subjectFilter: "all",
      selected: null,
    },

    // ---------- quiz page state ----------
    quiz: {
      phase: "select",        // select | active | results
      subjects: [],
      subject: "",
      quizzes: [],
      loading: false,
      generating: false,
      error: "",
      message: "",
      selectedQuiz: null,
      questions: [],
      currentIndex: 0,
      selectedOption: "",
      confirmed: false,
      answers: [],           // { correct: bool, selected: string, correctAnswer: string }
      score: 0,
      startTime: 0,
      elapsed: 0,
      timerInterval: null,
      attempts: [],
      averages: [],
      statsLoading: false,
    },

    // ---------- flashcard page state ----------
    flashcard: {
      phase: "select",        // select | read
      subjects: [],
      subject: "",
      decks: [],
      loading: false,
      generating: false,
      error: "",
      message: "",
      selectedDeck: null,
      cards: [],
      currentIndex: 0,
      shuffled: false,
      originalOrder: [],
    },

    // ---------- tasks board state ----------
    tasks: {
      items: [],
      counts: { todo: 0, in_progress: 0, done: 0 },
      newTitle: "",
      newSubject: "",
      saving: false,
      error: "",
      draggingId: null,
      dragOver: "",
    },
    taskColumns: [
      { id: "todo", label: "To Do", color: "#f97316", emptyIcon: "☰" },
      { id: "in_progress", label: "In Progress", color: "#6366f1", emptyIcon: "↻" },
      { id: "done", label: "Done", color: "#22c55e", emptyIcon: "✓" },
    ],

    // ---------- focus / pomodoro state ----------
    focus: {
      running: false,
      timer: null,
      mode: "focus",
      total: 1500,
      remaining: 1500,
      completedInCycle: 0,
      durations: { focus: 25, short: 5, long: 15 },
      todayCount: 0,
      stickies: [],
      newSticky: "",
      newStickyColor: "amber",
      stickySaving: false,
      error: "",
      stickyTimers: {},
    },
    focusModes: [
      { id: "focus", label: "Focus", color: "#f97316" },
      { id: "short", label: "Short Break", color: "#6366f1" },
      { id: "long", label: "Long Break", color: "#22c55e" },
    ],
    stickyColors: [
      { id: "amber", label: "Amber", hex: "#f59e0b" },
      { id: "indigo", label: "Indigo", hex: "#6366f1" },
      { id: "orange", label: "Orange", hex: "#f97316" },
      { id: "rose", label: "Rose", hex: "#f43f5e" },
      { id: "teal", label: "Teal", hex: "#14b8a6" },
    ],

    // ---------- chat state ----------
    chat: {
      agents: [],
      selectedAgent: "bill",
      history: [],
      isRunning: false,
      input: "",
      loading: false,
      error: "",
      pollTimer: null,
      pollMs: 2500,
      contextLimit: 40,
    },

    // ---------- voice input (local whisper.cpp STT) ----------
    voice: {
      recording: false, busy: false, target: null,
      mediaRecorder: null, chunks: [],
    },

    // ---------- recall (active recall engine) ----------
    recall: {
      stats: null, queue: [], idx: 0, readiness: [],
      loading: false, syncing: false, error: "",
      aiConfigured: false, aiModel: "",
      tutorQ: "", tutorSubject: "", tutorAnswer: "", tutorLoading: false, graded: 0,
    },

    // ---------- Tracker ----------
    tracker: {
      state: null, loading: false, error: "", viewDate: null,
      mcqAttempted: "", mcqCorrect: "", mcqSubject: "",
      sleepTime: "", wakeTime: "",
      productive: "", wasted: "",
      saving: false,
      roadmap: null, roadmapPhase: "", roadmapLoading: false,
      stats: null, statsLoading: false,
      meta: null, metaLoading: false,
    },

    // ---------- charts ----------
    charts: {},

    // ---------- screen wake lock (a) ----------
    wakeLock: { sentinel: null, supported: ("wakeLock" in (navigator || {})), active: false },

    // ---------- stats tab (b) — Observable Plot ----------
    statsTab: {
      loading: false, error: "", loaded: false,
      quiz: null, productivity: null, summary: null,
    },

    // ---------- library mind-map (c) ----------
    mindmap: { open: false, rendered: false },

    // ---------- knowledge graph tab (d) — Cytoscape ----------
    graph: {
      mode: "agents",          // 'agents' | 'concepts' | 'code'
      loading: false, error: "",
      cy: null, selected: null,
      counts: { nodes: 0, edges: 0 },
    },

    // ---------- Obsidian "Brain" — vault link graph as a neural net ----------
    // Reactive UI state only; the live simulation/render engine lives off-band
    // on this._brain (plain object, never proxied by Alpine) for speed.
    brain: {
      loading: false, error: "",
      paused: false,
      query: "",
      folders: [],
      stats: null,             // { notes, links, orphans }
      hover: null,             // { label, folder, deg, sx, sy } for the hover card
    },

    // ---------- profile avatar (replaces the "D" initial in the app bar) ----------
    profile: { avatarUrl: null, uploading: false },

    // ---------- system health panel (1) — /api/system/health ----------
    // Defensive: backend may not expose this yet (404) → clean empty-state,
    // never a crash. Polled every ~15s while the System tab is visible.
    system: {
      health: null,            // full payload (services/databases/disk/ram/last_backup)
      summary: null,           // small badge payload {up,total,status}
      loading: false,
      error: "",
      pollTimer: null,
      pollMs: 15000,
      unavailable: false,      // true once we see a 404 — drives the "not wired yet" note
    },

    // ---------- knowledge reindex affordance (4) ----------
    reindex: { busy: false, last: null, error: "" },

    // ---------- orchestrator self-knowledge + gaps ----------
    orch: { status: null, gaps: null, loading: false, error: "" },

    // ---------- voice converse loop (3) — STT → chat agent → TTS ----------
    // Chains the existing transcribe + speak plumbing around sendChatMessage.
    // Independent of the plain mic/speaker buttons so neither is broken.
    converse: {
      on: false,               // user has armed the loop
      phase: "idle",           // idle | recording | thinking | speaking
      mediaRecorder: null, chunks: [], stream: null,
      watching: false,         // polling chat for the reply to speak
      lastSpokenId: null,      // id of the last agent message we voiced
      error: "",
    },

    // ---------- transient toasts ----------
    toasts: [],                // [{id, kind:'ok'|'bad'|'info', text}]
    _toastSeq: 0,

    // ---------- terminal tab (e) — xterm.js over ws ----------
    term: {
      term: null, fit: null, ws: null,
      connected: false, error: "", fullscreen: false, booted: false,
    },

    // ---------- read-aloud (f) ----------
    speak: { busy: false, key: null, audio: null },

    // ---------- notifications (g) ----------
    notify: { permission: (typeof Notification !== "undefined" ? Notification.permission : "unsupported"), enabled: false },

    // ============================================================
    // INIT
    // ============================================================
    async init() {
      // theme — Volt (OLED lime, default) is the one true look; Terminal (phosphor)
      // is reserved for Dev-agent context. Nebula/Void/Light are fully retired and
      // any persisted legacy value is migrated to Volt by the pre-paint script.
      if (this.theme !== "terminal") this.theme = "volt";
      this.applyTheme();
      this.loadProfile();   // show the saved avatar in place of the "D" initial

      // sidebar collapse preference (default: expanded)
      this.sidebarCollapsed = localStorage.getItem("mc.sidebarCollapsed") === "1";

      // ping version + gateway
      try {
        const v = await (await fetch("/api/version")).json();
        if (v && v.version) this.version = v.version;
        this.gatewayOnline = true;
        this.stats.gatewayLabel = "Gateway online";
      } catch (e) {
        this.gatewayOnline = false;
        this.stats.gatewayLabel = "Gateway offline";
      }

      // pull live overview (totals, breakdown, daily, heatmap, recent, calendar)
      await this.loadOverview();
      // pull agents analytics
      await this.loadAgents();

      // (1) small system-health badge in the topbar — best-effort, non-blocking.
      this.loadSystemSummary();

      // subjects -> stats fallback
      if (!this.overview) {
        try {
          const s = await (await fetch("/api/subjects")).json();
          if (Array.isArray(s.subjects)) this.stats.subjects = s.subjects.length;
        } catch (e) {}
      }

      // design ref
      this.loadDesignRef();

      // subjects list for upload selector
      this.loadSubjects();

      // research history
      this.loadResearch();

      // mount reveal observer (taste-skill scroll animations)
      this.$nextTick(() => this.mountRevealObserver());

      // mount charts after DOM settles + Apex script is available
      this.$nextTick(() => {
        this.mountCharts();
        this.kickAgentBreakdownAnimation();
      });

      // re-mount charts when activeTab navigates back to overview
      this.$watch("activeTab", (val) => {
        if (val === "overview") {
          this.$nextTick(() => {
            this.mountCharts();
            this.kickAgentBreakdownAnimation();
            this.remountRevealObserver();
          });
        }
        if (val === "agents") {
          this.$nextTick(() => this.mountAgentsDonut());
        }
        if (val === "nlm-notebooks" || val === "nlm-chat" || val === "nlm-studio") {
          if (!this.nlmNotebooks.length && !this.nlmLoading) this.loadNotebookLM();
        }
        if (val === "obsidian-vault" || val === "obsidian-search") {
          if (!this.obsStatus && !this.obsLoading) this.loadObsidian();
        }
        if (val === "memory") {
          if (!this.memory.knowledge && !this.memory.loading) this.loadMemoryKnowledge();
        }
        if (val === "overview" || val === "briefing") {
          if (!this.dailyBriefing) this.loadDailyBriefing();
          // Briefing's "today" strip mirrors /api/overview hero — prefetch it so
          // the cells (and their count-ups) resolve even when Overview was skipped.
          if (!this.overview) this.loadOverview();
        }
      });


      // re-mount charts on theme flip (colours are baked at draw time)
      this.$watch("theme", () => this.$nextTick(() => {
        this.mountCharts();
        if (this.activeTab === "agents") this.mountAgentsDonut();
      }));

      // resize chart on window/sidebar layout changes (ApexCharts doesn't always catch container reflow)
      // Guard against double-binding if the root component is ever re-initialised.
      if (!this._onResize) {
        this._onResize = () => {
          if (this.charts.overview && this.activeTab === "overview") {
            try { this.charts.overview.updateOptions({}, false, true); } catch (e) {}
          }
          // Keep Stats charts and the terminal sized to their containers.
          if (this.activeTab === "stats") this.renderStatsCharts();
          if (this.activeTab === "terminal") this.fitTerminal();
        };
        window.addEventListener("resize", this._onResize, { passive: true });
      }

      // (a) Wake Lock: re-acquire after the tab returns to the foreground if a
      // focus session is still running (the lock is auto-released on hide).
      if (!this._onVisibility) {
        this._onVisibility = () => {
          if (document.visibilityState === "visible" && this.focus.running) {
            this.requestWakeLock();
          }
        };
        document.addEventListener("visibilitychange", this._onVisibility);
      }

      // (g) Notifications: reflect any previously-granted permission as enabled.
      try {
        if (typeof Notification !== "undefined") {
          this.notify.permission = Notification.permission;
          this.notify.enabled = (Notification.permission === "granted" &&
            localStorage.getItem("mc.notify") === "1");
        }
      } catch (_) {}

      // (h) PWA: register the service worker (scope: /static). Progressive —
      // a browser without SW support simply skips this.
      this.registerServiceWorker();

      // Re-render Mermaid / mind-map when a note finishes loading in the Library.
      this.$watch("notes.html", () => {
        if (this.activeTab === "library-notes") {
          this.mindmap.rendered = false;
          this.$nextTick(() => {
            this.renderMermaidIn("library-note-body");
            if (this.mindmap.open) this.renderMindmap();
          });
        }
      });
    },

    kickAgentBreakdownAnimation() {
      // toggle off then on so CSS transitions play every time the page mounts
      this.agentBreakdownAnimated = false;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => { this.agentBreakdownAnimated = true; });
      });
    },

    // ============================================================
    // THEME
    // ============================================================
    // True when the browser supports the View Transitions API and the user
    // has not asked for reduced motion. Used to progressively enhance tab
    // navigation and theme flips with a native crossfade.
    _canViewTransition() {
      try {
        return typeof document.startViewTransition === "function" &&
          !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      } catch (_) { return false; }
    },
    // Concentric dual progress ring as inline SVG: outer = accuracy,
    // inner = mastery/coverage. Values are 0..1. stroke-dashoffset animated by CSS.
    dualRingSvg(outer, inner, opts) {
      const o = Math.max(0, Math.min(1, outer || 0));
      const i = Math.max(0, Math.min(1, inner || 0));
      const oc = (opts && opts.outerColor) || "var(--accent)";      // accuracy → lime (brand)
      const ic = (opts && opts.innerColor) || "var(--c-emerald)";   // mastery → emerald (mastered)
      const C1 = 2 * Math.PI * 24, C2 = 2 * Math.PI * 16;
      return `
        <circle cx="32" cy="32" r="24" fill="none" stroke="var(--bg-4)" stroke-width="5"/>
        <circle cx="32" cy="32" r="16" fill="none" stroke="var(--bg-4)" stroke-width="5"/>
        <circle class="dr-outer" cx="32" cy="32" r="24" fill="none" stroke="${oc}" stroke-width="5"
          stroke-linecap="round" stroke-dasharray="${C1.toFixed(1)}"
          stroke-dashoffset="${(C1 * (1 - o)).toFixed(1)}" transform="rotate(-90 32 32)"/>
        <circle class="dr-inner" cx="32" cy="32" r="16" fill="none" stroke="${ic}" stroke-width="5"
          stroke-linecap="round" stroke-dasharray="${C2.toFixed(1)}"
          stroke-dashoffset="${(C2 * (1 - i)).toFixed(1)}" transform="rotate(-90 32 32)"/>`;
    },
    // Split-flap: render each character of a value as a .flap-digit cell.
    // aurora.js watches [data-flap] and adds .flipping to changed digits.
    splitFlapMarkup(value) {
      const s = String(value == null ? 0 : value);
      let out = "";
      for (const ch of s) out += '<span class="flap-digit"><span>' + ch + "</span></span>";
      return out;
    },
    // ApexCharts only understands 'dark' | 'light'; all themes render on a dark
    // canvas so they map to the dark chart palette.
    chartMode() { return "dark"; },   // Nebula, Terminal, Void all render on dark chart canvas
    setTheme(id, _auto) {
      if ((id !== "volt" && id !== "terminal") || id === this.theme) return;
      // A manual theme pick (from the picker / palette) cancels Dev auto-theming
      // so we don't later override the user's explicit choice.
      if (!_auto) { this._autoTerminal = false; this._themeBeforeDev = null; }
      // applyTheme() flips the data-theme attribute synchronously, so the View
      // Transition callback resolves immediately (no Alpine async DOM wait).
      // We update this.theme too; its reactive consumers (charts) settle after
      // the snapshot, which is fine — the crossfade is driven by
      // the attribute change the VT actually captures.
      const apply = () => { this.theme = id; this.applyTheme(); };
      if (this._canViewTransition()) {
        try {
          const vt = document.startViewTransition(() => { apply(); });
          // Swallow rejections from an interrupted theme transition so they
          // never surface as unhandled promise rejections (matches goTo()).
          if (vt.ready) vt.ready.catch(() => {});
          if (vt.updateCallbackDone) vt.updateCallbackDone.catch(() => {});
          if (vt.finished) vt.finished.catch(() => {});
          return;
        }
        catch (_) { /* fall through */ }
      }
      apply();
    },
    cycleTheme() {
      const order = ["volt"];
      const next = order[(order.indexOf(this.theme) + 1) % order.length];
      this.setTheme(next);
    },
    // Back-compat: the topbar button + ⌘K action call toggleTheme().
    toggleTheme() { this.cycleTheme(); },
    applyTheme() {
      document.documentElement.setAttribute("data-theme", this.theme);
      try { localStorage.setItem("mc.theme", this.theme); } catch (e) {}
    },

    // ============================================================
    // NAV
    // ============================================================
    isNavActive(item) {
      if (item.id === this.activeTab) return true;
      if (item.subTabs && item.subTabs.some((t) => t.id === this.activeTab)) return true;
      return false;
    },
    isGroupActive(group) {
      return group.items.some((it) => this.isNavActive(it));
    },
    // The nav group that owns the active tab — drives the contextual sidebar and
    // the active top-nav pill. Falls back to the first group.
    activeGroupObj() {
      return this.nav.find((g) => this.isGroupActive(g)) || this.nav[0];
    },
    // Top-nav pill click → jump into this group (its first item). If we're
    // already inside it, stay put on the current page.
    goToGroup(group) {
      if (this.isGroupActive(group)) return;
      this.goToNav(group.items[0]);
    },
    goToNav(item) {
      // Compute the target without pre-assigning activeTab, so goTo() can
      // still detect the change and run a view transition.
      const target = (item.subTabs && item.subTabs.length) ? item.subTabs[0].id : item.id;
      this.goTo(target);
      this.mobileNavOpen = false;   // (i) close the drawer after picking a page
    },
    toggleMobileNav() { this.mobileNavOpen = !this.mobileNavOpen; },
    goTo(id) {
      if (id === this.activeTab && !this._vtActive) { this._goToLoaders(id); return; }
      // Serialize navigation through the View Transition. If a nav arrives while
      // a transition is in flight, queue the latest target and apply it when the
      // current VT finishes — never mutate activeTab outside a running VT, which
      // is what previously skipped transitions and leaked rejections.
      if (this._vtActive) { this._pendingNav = id; return; }
      if (this._canViewTransition()) {
        try {
          this._vtActive = true;
          document.body.classList.add("vt-nav");
          const done = document.startViewTransition(() => {
            this.activeTab = id;
            return new Promise((resolve) => {
              let settled = false;
              const finish = () => { if (!settled) { settled = true; resolve(); } };
              this.$nextTick(finish);
              setTimeout(finish, 60);
            });
          });
          // Catch every promise the API exposes so an interrupted transition can
          // never escape as an unhandled rejection.
          if (done.ready) done.ready.catch(() => {});
          if (done.updateCallbackDone) done.updateCallbackDone.catch(() => {});
          done.finished.catch(() => {}).finally(() => {
            this._vtActive = false;
            document.body.classList.remove("vt-nav");
            // drain a queued nav (most recent wins)
            const next = this._pendingNav;
            this._pendingNav = null;
            if (next && next !== this.activeTab) this.goTo(next);
          });
          this._goToLoaders(id);
          return;
        } catch (_) {
          this._vtActive = false;
          document.body.classList.remove("vt-nav");
          /* fall through to direct switch */
        }
      }
      this.activeTab = id;
      this._goToLoaders(id);
    },
    _goToLoaders(id) {
      // Stop the agent-network animation whenever we leave the Agents page.
      if (id !== "agents" && this._agentNetRAF) {
        cancelAnimationFrame(this._agentNetRAF); this._agentNetRAF = null;
      }
      // Stop background polling when leaving the tabs that own it (avoid leaks).
      if (id !== "chat") this.stopChatPoll();
      if (id !== "research" && this.researchPollTimer) {
        clearInterval(this.researchPollTimer); this.researchPollTimer = null;
      }
      // Stop the quiz timer when leaving the quiz tab (it no-ops if not running).
      if (id !== "practice-quiz") this.stopQuizTimer();
      // Tear down the live terminal websocket when leaving the Terminal tab.
      if (id !== "terminal") this.teardownTerminal();
      // (1) System health: poll while visible, stop polling when we leave.
      if (id !== "system") this.stopSystemPoll();
      if (id === "system")   { this.startSystemPoll(); }
      if (id === "stats")    { this.$nextTick(() => this.loadStatsTab()); }
      if (id === "graph")    { this.$nextTick(() => this.loadGraph()); }
      if (id === "terminal") { this.$nextTick(() => this.bootTerminal()); }
      if (id === "agents") this.$nextTick(() => this.drawAgentNetwork());
      if (id === "research") this.loadResearch();
      if (id === "library-notes") this.loadNotesList();
      if (id === "library-lecture-notes") this.loadLectureNotes();
      if (id === "planner-schedule") this.loadSchedule();
      if (id === "planner-tasks") this.loadTasks();
      if (id === "planner-focus") { this.loadFocusDurations(); this.loadPomodoroToday(); this.loadStickies(); }
      if (id === "practice-quiz") this.loadQuizSubjects();
      if (id === "practice-flashcards") this.loadFlashcardSubjects();
      if (id === "chat") this.openChat();
      if (id === "nlm-notebooks") { this.loadNotebookLM(); }
      if (id === "nlm-chat")      { this.loadNotebookLM(); }
      if (id === "nlm-studio")    { this.loadNotebookLM(); }
      if (id !== "obsidian-graph")    { this.brainStop(); }
      if (id === "obsidian-vault")    { this.loadObsidian(); }
      if (id === "obsidian-graph")    { this.$nextTick(() => this.loadObsidianBrain()); }
      if (id === "obsidian-search")   { this.loadObsidian(); }
      if (id === "memory")            { this.loadMemoryKnowledge(); }
      if (id === "recall")            { this.loadRecall(); }
      if (id === "tracker-today")     { this.loadTracker(); }
      if (id === "tracker-roadmap")   { this.loadTrackerRoadmap(); }
      if (id === "tracker-stats")     { this.loadTrackerStats(); }
    },
    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      try { localStorage.setItem("mc.sidebarCollapsed", this.sidebarCollapsed ? "1" : "0"); } catch (e) {}
    },

    currentSubTabs() {
      for (const g of this.nav) {
        for (const it of g.items) {
          if (it.subTabs && it.subTabs.some((s) => s.id === this.activeTab)) {
            return it.subTabs;
          }
        }
      }
      return [];
    },

    // ============================================================
    // OVERVIEW
    // ============================================================
    async loadOverview() {
      try {
        const r = await fetch("/api/overview");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.overview = await r.json();
        // mirror into legacy stats for any leftover bindings
        this.stats.runs = this.overview.totals.agent_tasks;
        this.stats.subjects = this.overview.totals.subjects;
        this.stats.streak = this.overview.hero.day_streak;
        this.agentBreakdown = this.overview.agent_breakdown;
      } catch (e) {
        console.warn("overview load failed", e);
      }
      // Readiness (best-effort) for the bento readiness tile
      try {
        const rr = await fetch("/api/study/readiness");
        if (rr.ok) {
          const data = await rr.json();
          const items = (data.items || [])
            .filter((it) => it.readiness != null)
            .sort((a, b) => (b.urgency || 0) - (a.urgency || 0))
            .slice(0, 4);
          this.ovReadiness = items;
        }
      } catch (_) { /* readiness is optional */ }
      // Best-effort: pre-warm system health (state-only, no DOM deps) so the
      // right-rail "Active Tools" ChromaDB tile shows a real live/down status
      // instead of sitting permanently idle. Fetch once; the System tab reuses it.
      if (!this.system.health) { try { this.loadSystemHealth(); } catch (_) {} }
    },
    // Composite readiness across subjects with data (0..100).
    readinessComposite() {
      const xs = (this.ovReadiness || []).map((i) => i.readiness).filter((n) => n != null);
      if (!xs.length) return 0;
      return Math.round(xs.reduce((s, n) => s + n, 0) / xs.length);
    },

    // ---------- right rail: agent performance ----------
    // Joins agent_breakdown (name/role/color/count) with the 7d hourly heatmap
    // (per-agent hour-of-day profile → real sparkline + 7d query total).
    ovAgents() {
      if (!this.overview) return [];
      const hmap = (this.overview.heatmap && this.overview.heatmap.agents) || [];
      const byKey = {};
      for (const h of hmap) byKey[h.key] = h;
      return (this.overview.agent_breakdown || []).map((a) => {
        const src = byKey[a.key];
        const hours = (src && Array.isArray(src.hours)) ? src.hours.map((c) => c.count || 0) : [];
        const total7d = hours.reduce((s, n) => s + n, 0);
        return {
          key: a.key, name: a.name, role: a.role || "", color: a.color,
          count: a.count || 0, hours, total7d,
        };
      });
    },

    // Inline sparkline (24h profile, last 7d). Lime stroke + faint area fill.
    sparkSvg(values) {
      const w = 200, h = 36, pad = 3;
      const vals = (values && values.length) ? values : [0, 0];
      const max = Math.max(1, ...vals);
      const n = vals.length;
      const dx = n > 1 ? (w - pad * 2) / (n - 1) : 0;
      const pts = vals.map((v, i) => {
        const x = pad + i * dx;
        const y = h - pad - (v / max) * (h - pad * 2);
        return [Number(x.toFixed(1)), Number(y.toFixed(1))];
      });
      const line = pts.map((p, i) => (i === 0 ? "M" : "L") + p[0] + " " + p[1]).join(" ");
      const last = pts[pts.length - 1], first = pts[0];
      const area = line + " L" + last[0] + " " + (h - pad) + " L" + first[0] + " " + (h - pad) + " Z";
      const accent = this.cssVar("--accent") || "#c8ff00";
      const rgb = this.cssVar("--accent-rgb") || "200,255,0";
      return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
        + `<path d="${area}" fill="rgba(${rgb},0.12)"/>`
        + `<path d="${line}" fill="none" stroke="${accent}" stroke-width="1.6" `
        + `stroke-linejoin="round" stroke-linecap="round"/></svg>`;
    },

    // ---------- right rail: active tools ----------
    // Live status from REAL systemd units in /api/system/health (each is
    // {unit, active, enabled}). Each tile prefers the unit's real active state
    // and falls back to a reliable secondary signal when health hasn't loaded:
    //   Gateway → hermes-gateway unit  · fallback gatewayOnline (init ping)
    //   API     → mission-control unit · fallback !!overview (this payload proves it)
    //   Sync    → syncthing unit       · no fallback → idle until health loads
    ovTools() {
      const svcs = (this.system && this.system.health && this.system.health.services) || [];
      const unit = {};
      if (Array.isArray(svcs)) for (const s of svcs) unit[s.unit] = (s.active === "active");
      // health-derived status, else fallback signal, else neutral idle
      const st = (u, fb) => (u in unit ? (unit[u] ? "live" : "down")
                                       : (fb == null ? "idle" : (fb ? "live" : "down")));
      const ic = {
        bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h8l-1 8 10-12h-8z"/></svg>',
        brain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3a3 3 0 0 0-3 3 3 3 0 0 0-1 5.8A3 3 0 0 0 9 17V3z"/><path d="M15 3a3 3 0 0 1 3 3 3 3 0 0 1 1 5.8A3 3 0 0 1 15 17V3z"/></svg>',
        sync: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15.5-6.4L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.5 6.4L3 16"/><path d="M3 21v-5h5"/></svg>',
      };
      return [
        { key: "gateway", name: "Gateway", icon: ic.brain, status: st("hermes-gateway", this.gatewayOnline) },
        { key: "api",     name: "API",     icon: ic.bolt,  status: st("mission-control", !!this.overview) },
        { key: "sync",    name: "Sync",    icon: ic.sync,  status: st("syncthing", null) },
      ];
    },

    formatNumber(n) {
      if (n == null) return "—";
      if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
      if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
      return String(n);
    },

    formatRelative(iso) {
      if (!iso) return "";
      const t = new Date(iso).getTime();
      if (!Number.isFinite(t)) return "";
      const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (sec < 60)        return sec + "s ago";
      const min = Math.floor(sec / 60);
      if (min < 60)        return min + "m ago";
      const hr = Math.floor(min / 60);
      if (hr < 24)         return hr + "h ago";
      const d = Math.floor(hr / 24);
      if (d < 7)           return d + "d ago";
      return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    },

    // ============================================================
    // UPLOAD
    // ============================================================
    async loadSubjects() {
      try {
        const r = await fetch("/api/subjects");
        const j = await r.json();
        this.subjectOptions = Array.isArray(j.subjects) ? j.subjects : [];
      } catch (e) {
        this.subjectOptions = [];
      }
    },

    onUploadPick(ev) {
      const files = ev.target.files;
      if (files && files.length) this.uploadFile = files[0];
    },
    onUploadDrop(ev) {
      this.uploadDragging = false;
      const dt = ev.dataTransfer;
      if (dt && dt.files && dt.files.length) this.uploadFile = dt.files[0];
    },
    onUploadKey(ev) {
      // Esc clears the selected file
      if (ev.key === "Escape" && this.uploadFile && this.activeTab === "upload") {
        this.uploadFile = null;
        ev.preventDefault();
      }
      // Cmd/Ctrl + Enter submits
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && this.activeTab === "upload") {
        this.submitUpload();
        ev.preventDefault();
      }
    },
    formatFileSize(bytes) {
      if (bytes == null) return "";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + " MB";
      return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    },

    async submitUpload() {
      if (this.uploadLoading || !this.uploadFile) return;
      const subject = this.uploadSubjectMode === "__new" ? this.uploadNewSubject.trim() : this.uploadSubjectMode;
      if (!subject) {
        this.uploadError = "Please select or enter a subject.";
        return;
      }
      this.uploadLoading = true;
      this.uploadError = "";
      this.uploadMessage = "";
      this.uploadResponse = null;

      const fd = new FormData();
      fd.append("file", this.uploadFile, this.uploadFile.name);
      fd.append("subject", subject);

      try {
        const r = await fetch("/api/subjects/upload", { method: "POST", body: fd });
        const j = await r.json();
        if (!r.ok) {
          this.uploadError = j.detail || j.message || `HTTP ${r.status}`;
        } else {
          this.uploadResponse = j;
          this.uploadMessage = j.message || "Pipeline started.";
          this.uploadFile = null;
        }
      } catch (e) {
        this.uploadError = "Network error: " + String(e);
      } finally {
        this.uploadLoading = false;
      }
    },

    // ============================================================
    // RESEARCH
    // ============================================================
    async loadResearch() {
      try {
        const r = await fetch("/api/research");
        const j = await r.json();
        const oldMap = new Map(this.researchItems.map((i) => [i.id, i.status]));
        this.researchItems = j.items || [];
        // If any previously-researching item flipped to complete, auto-select and load
        for (const item of this.researchItems) {
          if (item.status === "complete" && oldMap.get(item.id) === "researching") {
            this.selectResearchItem(item);
            break;
          }
        }
        if (this.researchSelected) {
          // refresh selected item status
          const found = this.researchItems.find((i) => i.id === this.researchSelected.id);
          if (found) {
            this.researchSelected.status = found.status;
            this.researchSelected.ready = found.ready;
          }
        }
        // Keep polling while any item is researching
        const hasResearching = this.researchItems.some((i) => i.status === "researching");
        if (this.researchPollTimer) clearInterval(this.researchPollTimer);
        if (hasResearching) {
          this.researchPollTimer = setInterval(() => this.loadResearch(), 3000);
        }
      } catch (e) { /* silent */ }
    },

    selectResearchItem(item) {
      this.researchSelected = { ...item };
      this.researchContent = "";
      this.researchQuizMessage = "";
      this.researchError = "";
      this.researchSendOpen = false;
      this.researchSendMsg = "";
      this.researchSendErr = "";
      if (item.ready || item.status === "complete") {
        this.loadResearchContent(item.id);
      }
    },

    async loadResearchContent(id) {
      try {
        const r = await fetch("/api/research/" + id);
        const j = await r.json();
        if (!r.ok) return;
        this.researchContent = this.renderMarkdown(j.content || "");
        this.researchSelected.status = j.status;
        this.researchSelected.ready = j.ready;
      } catch (e) { /* silent */ }
    },

    async submitResearch() {
      if (this.researchLoading) return;
      const title = (this.researchTitle || "").trim();
      const query = (this.researchQuery || "").trim();
      if (!title || !query) {
        this.researchError = "Both title and query are required.";
        return;
      }
      this.researchLoading = true;
      this.researchError = "";
      this.researchMessage = "";
      try {
        const r = await fetch("/api/research", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, query }),
        });
        const j = await r.json();
        if (!r.ok) {
          this.researchError = j.detail || j.message || "HTTP " + r.status;
        } else {
          this.researchMessage = "Scholar is researching: " + title;
          this.researchTitle = "";
          this.researchQuery = "";
          this.loadResearch();
        }
      } catch (e) {
        this.researchError = "Network error: " + String(e);
      } finally {
        this.researchLoading = false;
      }
    },

    researchStatusClass(item) {
      return item.status === "researching" ? "researching" : "complete";
    },

    researchStatusLabel(item) {
      return item.status === "researching" ? "Researching…" : "Complete";
    },

    async makeResearchQuiz() {
      if (this.researchQuizLoading || !this.researchSelected) return;
      this.researchQuizLoading = true;
      this.researchQuizMessage = "";
      try {
        const r = await fetch("/api/research/" + this.researchSelected.id + "/quiz", {
          method: "POST",
        });
        const j = await r.json();
        if (!r.ok) {
          this.researchQuizMessage = j.detail || j.message || "HTTP " + r.status;
        } else {
          this.researchQuizMessage = j.message || "Quiz queued.";
        }
      } catch (e) {
        this.researchQuizMessage = "Network error: " + String(e);
      } finally {
        this.researchQuizLoading = false;
      }
    },

    // ---- Send to NotebookLM: open a notebook picker, then upload the doc ----
    async toggleResearchSend() {
      this.researchSendErr = "";
      this.researchSendOpen = !this.researchSendOpen;
      if (!this.researchSendOpen || !this.researchSelected) return;
      this.researchSendLoading = true;
      this.researchSendNotebooks = [];
      try {
        const r = await fetch("/api/nlm/notebooks").then((x) => x.json());
        if (!r || r.ok === false) {
          const reason = r && r.reason;
          this.researchSendErr =
            reason === "auth-missing" ? "Sign in to NotebookLM first (open NotebookLM ▸ Notebooks)."
            : reason === "cli-missing" ? "NotebookLM CLI isn't installed."
            : "Couldn't load notebooks.";
        } else {
          this.researchSendNotebooks = Array.isArray(r.notebooks) ? r.notebooks : [];
          if (!this.researchSendNotebooks.length) {
            this.researchSendErr = "No notebooks yet — create one in NotebookLM ▸ Notebooks.";
          }
        }
      } catch (e) {
        this.researchSendErr = "Couldn't reach NotebookLM.";
      } finally {
        this.researchSendLoading = false;
      }
    },
    async sendResearchToNotebook(notebookId) {
      if (!this.researchSelected || this.researchSendBusy) return;
      this.researchSendBusy = true;
      this.researchSendErr = "";
      this.researchSendMsg = "";
      try {
        const r = await fetch(
          "/api/nlm/notebooks/" + encodeURIComponent(notebookId) + "/sources/from-research",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ research_id: this.researchSelected.id }),
          }
        ).then((x) => x.json());
        if (r && r.ok) {
          this.researchSendMsg = "Added “" + (r.title || this.researchSelected.title) + "” to NotebookLM as a source.";
          this.researchSendOpen = false;
        } else {
          this.researchSendErr = (r && r.detail) ? String(r.detail).slice(0, 180) : "Send failed.";
        }
      } catch (e) {
        this.researchSendErr = "Send failed: " + String(e);
      } finally {
        this.researchSendBusy = false;
      }
    },

    renderMarkdown(md) {
      // marked.parse() emits raw HTML — any <script>/<img onerror=...> in the
      // source (model-returned text, scraped notes, vault markdown) would run
      // when bound via x-html. Sanitize through DOMPurify before returning.
      const text = md || "";
      if (typeof marked === "undefined") {
        return "<p>" + this.escapeHtml(text).replace(/\n/g, "<br>") + "</p>";
      }
      const html = marked.parse(text, { mangle: false, headerIds: false });
      return (typeof DOMPurify !== "undefined") ? DOMPurify.sanitize(html) : html;
    },

    renderChatText(text) {
      // Chat bubbles render user + agent text. Both are untrusted — agents
      // routinely return raw HTML in their responses. Escape every character
      // first, THEN turn newlines into <br>. No marked, no x-html footgun.
      return this.escapeHtml(text || "").replace(/\n/g, "<br>");
    },

    escapeHtml(s) {
      return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")  .replace(/"/g, "&quot;");
    },

    // ============================================================
    // NOTES LIBRARY
    // ============================================================
    async loadNotesList() {
      if (!this.notes.subject) {
        this.notes.items = [];
        this.notes.content = null;
        this.notes.html = "";
        this.notes.selectedId = "";
        return;
      }
      this.notes.loading = true;
      this.notes.error = "";
      try {
        const r = await fetch("/api/notes/" + encodeURIComponent(this.notes.subject));
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        this.notes.items = j.items || [];
        // If selected file no longer exists, clear viewer
        if (this.notes.selectedId &&
            !this.notes.items.find(i => i.id === this.notes.selectedId)) {
          this.notes.selectedId = "";
          this.notes.content = null;
          this.notes.html = "";
        }
      } catch (e) {
        this.notes.error = "Failed to load notes: " + String(e);
        this.notes.items = [];
      } finally {
        this.notes.loading = false;
      }
    },

    async selectNote(note) {
      if (!note || !this.notes.subject) return;
      this.notes.selectedId = note.id;
      this.notes.loading = true;
      this.notes.error = "";
      try {
        const url = "/api/notes/" + encodeURIComponent(this.notes.subject)
                  + "/" + note.id.split("/").map(encodeURIComponent).join("/");
        const r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        this.notes.content = j;
        this.notes.html = this.renderMarkdown(j.content || "");
      } catch (e) {
        this.notes.error = "Failed to read note: " + String(e);
        this.notes.content = null;
        this.notes.html = "";
      } finally {
        this.notes.loading = false;
      }
    },

    // ============================================================
    // SCHEDULE
    // ============================================================
    async loadSchedule() {
      this.schedule.loading = true;
      this.schedule.error = "";
      try {
        const r = await fetch("/api/schedule");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.schedule.data = await r.json();
      } catch (e) {
        this.schedule.error = "Failed to load schedule: " + String(e);
        this.schedule.data = null;
      } finally {
        this.schedule.loading = false;
      }
    },

    scheduleRows(bucket) {
      const d = this.schedule.data;
      if (!d || !Array.isArray(d[bucket])) return [];
      return d[bucket];
    },

    // ============================================================
    // LECTURE NOTES
    // ============================================================
    async loadLectureNotes() {
      try {
        const r = await fetch("/api/lectures");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const j = await r.json();
        this.lecture.items = j.items || [];
        if (this.lecture.selected &&
            !this.lecture.items.find(i => i.id === this.lecture.selected.id)) {
          this.lecture.selected = null;
          this.lecture.saveMessage = "";
          this.lecture.saveStatus = "";
        }
      } catch (e) {
        this.lecture.items = [];
        this.lecture.saveStatus = "bad";
        this.lecture.saveMessage = "Failed to load PDFs: " + String(e);
      }
    },

    lectureSubjects() {
      const set = new Set();
      for (const p of this.lecture.items) {
        if (p.subject) set.add(p.subject);
      }
      return Array.from(set).sort();
    },

    filteredLectureNotes() {
      const f = this.lecture.subjectFilter;
      if (!f || f === "all") return this.lecture.items;
      return this.lecture.items.filter(p => p.subject === f);
    },

    selectLecturePdf(pdf) {
      this.lecture.selected = pdf;
      this.lecture.saveStatus = "";
      this.lecture.saveMessage = "";
    },

    lectureViewerUrl() {
      if (!this.lecture.selected) return "about:blank";
      const fileUrl = "/api/lectures/" + encodeURI(this.lecture.selected.id);
      // Custom lecture viewer: prebuilt PDF.js wrapped with our save bridge.
      // The bridge uses `pdfId` to PUT annotated bytes back to the same file.
      return "/static/pdfjs/lecture-viewer.html?file="
        + encodeURIComponent(fileUrl)
        + "&pdfId=" + encodeURIComponent(this.lecture.selected.id);
    },

    fullscreenLectureViewer() {
      const frame = document.getElementById("lecturePdfFrame");
      if (!frame) return;
      const req = frame.requestFullscreen
                || frame.webkitRequestFullscreen
                || frame.mozRequestFullScreen
                || frame.msRequestFullscreen;
      if (req) req.call(frame);
    },

    // Called by the save bridge running inside the PDF.js iframe.
    // Saves the (possibly annotated) PDF bytes back to the same file.
    async _saveLectureFromViewer(pdfId, base64) {
      if (!pdfId || !base64) return;
      const binary = atob(base64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      this.lecture.saveStatus = "";
      this.lecture.saveMessage = "Saving annotations…";
      try {
        const r = await fetch("/api/lectures/" + encodeURI(pdfId), {
          method: "PUT",
          headers: { "Content-Type": "application/pdf" },
          body: bytes,
        });
        if (!r.ok) {
          const t = await r.text();
          throw new Error("HTTP " + r.status + " — " + t.slice(0, 120));
        }
        const j = await r.json();
        const kb = (j.size_bytes / 1024).toFixed(1);
        this.lecture.saveStatus = "ok";
        this.lecture.saveMessage = `Saved annotations back to ${pdfId} (${kb} KB)`;
        // Refresh list so the size_bytes + modified_at pick up the new write.
        await this.loadLectureNotes();
      } catch (e) {
        this.lecture.saveStatus = "bad";
        this.lecture.saveMessage = "Save failed: " + String(e);
      }
    },

    // ============================================================
    // AGENTS
    // ============================================================
    async loadAgents() {
      try {
        const r = await fetch("/api/agents/analytics");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        // Attach SVG icon (key → AGENT_ICONS map) for use in templates
        if (data.agents) {
          for (const a of data.agents) {
            a.iconSvg = AGENT_ICONS[a.key] || AGENT_ICONS.default;
          }
        }
        this.agentsAnalytics = data;
        // (re)draw the topology once the data + canvas exist
        this.$nextTick(() => this.drawAgentNetwork());
      } catch (e) {
        console.warn("agents analytics load failed", e);
      }
    },

    // ── Agent network canvas ────────────────────────────────────────────────
    // Hexagonal mesh: Bill at the hub, the other five on a ring, edges to Bill,
    // node radius scaled by task share, color from AGENT_PALETTE / agent.color.
    drawAgentNetwork() {
      const cv = document.getElementById("agentNetwork");
      if (!cv || !this.agentsAnalytics || !Array.isArray(this.agentsAnalytics.agents)) return;
      if (this._agentNetRAF) { cancelAnimationFrame(this._agentNetRAF); this._agentNetRAF = null; }

      const ctx = cv.getContext("2d");
      if (!ctx) return;
      // crisp on HiDPI
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cssW = cv.clientWidth || 900, cssH = 320;
      cv.width = Math.round(cssW * dpr); cv.height = Math.round(cssH * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      const ink = css("--ink-2") || "#ccc";
      const faint = css("--ink-4") || "rgba(255,255,255,0.3)";
      const line = css("--line-2") || "rgba(255,255,255,0.14)";
      const accentRgb = css("--accent-rgb") || "200,255,0";

      const agents = this.agentsAnalytics.agents.slice(0, 6);
      // task volume lives under totals.tasks (a.tasks/.task_count were always
      // undefined, so every node used to collapse to the minimum radius)
      const taskCount = (a) => (a.totals && a.totals.tasks) || a.tasks || a.task_count || 0;
      const maxTasks = Math.max(1, ...agents.map(taskCount));
      const cx = cssW / 2, cy = cssH / 2;
      const ringR = Math.min(cssW, cssH) * 0.36;

      // Bill is the hub if present; otherwise the first agent.
      let hubIdx = agents.findIndex((a) => (a.key || a.name || "").toLowerCase().includes("bill"));
      if (hubIdx < 0) hubIdx = 0;
      const ring = agents.filter((_, i) => i !== hubIdx);

      // Dense array: hub first, then ring nodes (a sparse array left holes when
      // the hub agent was not index 0, silently dropping nodes/edges).
      const nodes = [{ a: agents[hubIdx], x: cx, y: cy, hub: true }];
      ring.forEach((a, k) => {
        const ang = (-Math.PI / 2) + (k / ring.length) * Math.PI * 2;
        nodes.push({ a, x: cx + Math.cos(ang) * ringR, y: cy + Math.sin(ang) * ringR, hub: false });
      });

      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const hub = nodes[0];

      const nodeColor = (a) => a.color || this.AGENT_PALETTE?.[(a.key || a.name || "").toLowerCase()] || "#818cf8";
      const nodeR = (a) => 9 + 20 * (taskCount(a) / maxTasks);

      const render = (t) => {
        ctx.clearRect(0, 0, cssW, cssH);

        // edges: every ring node to the hub, with a travelling pulse
        nodes.forEach((n) => {
          if (n === hub) return;
          ctx.strokeStyle = "rgba(" + accentRgb + ",0.16)"; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(hub.x, hub.y); ctx.lineTo(n.x, n.y); ctx.stroke();
          if (!reduce) {
            const p = ((t / 1600) + (n.x + n.y) / 900) % 1;
            const px = hub.x + (n.x - hub.x) * p, py = hub.y + (n.y - hub.y) * p;
            ctx.fillStyle = nodeColor(n.a);
            ctx.globalAlpha = 0.8;
            ctx.beginPath(); ctx.arc(px, py, 2.4, 0, Math.PI * 2); ctx.fill();
            ctx.globalAlpha = 1;
          }
        });

        // nodes
        nodes.forEach((n) => {
          const col = nodeColor(n.a);
          const r = n.hub ? nodeR(n.a) + 6 : nodeR(n.a);
          // glow
          ctx.save();
          ctx.shadowColor = col; ctx.shadowBlur = n.hub ? 24 : 14;
          ctx.fillStyle = col; ctx.globalAlpha = 0.18;
          ctx.beginPath(); ctx.arc(n.x, n.y, r + 4, 0, Math.PI * 2); ctx.fill();
          ctx.restore();
          // disc
          ctx.fillStyle = col; ctx.globalAlpha = (n.a.status === "live" || n.hub) ? 1 : (n.a.status === "idle" ? 0.4 : 0.7);
          ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
          ctx.globalAlpha = 1;
          // ring outline for hub
          if (n.hub) { ctx.strokeStyle = "rgba(255,255,255,0.6)"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(n.x, n.y, r + 3, 0, Math.PI * 2); ctx.stroke(); }
          // label
          ctx.fillStyle = ink; ctx.font = "600 11px Inter, system-ui, sans-serif";
          ctx.textAlign = "center"; ctx.textBaseline = "top";
          ctx.fillText((n.a.name || n.a.key || "").toString(), n.x, n.y + r + 5);
          // task count
          ctx.fillStyle = faint; ctx.font = "10px 'JetBrains Mono', monospace";
          ctx.fillText(String(taskCount(n.a)), n.x, n.y + r + 19);
        });

        if (!reduce) this._agentNetRAF = requestAnimationFrame(render);
      };
      // seed t with a fixed value (Date.now is unavailable in some sandboxes but
      // fine in the browser; performance.now keeps the pulse smooth)
      render(typeof performance !== "undefined" ? performance.now() : 0);
    },

    agentDistribution() {
      return this.agentsAnalytics?.task_statistics?.distribution || [];
    },

    filteredAgentRecent() {
      const rows = (this.agentsAnalytics && this.agentsAnalytics.recent_activity) ? this.agentsAnalytics.recent_activity : [];
      if (!Array.isArray(rows)) return [];
      if (!this.agentFilter || this.agentFilter === "all") return rows;
      return rows.filter(r => r && r.agent === this.agentFilter);
    },

    // ---- task distribution donut ----
    mountAgentsDonut() {
      if (typeof ApexCharts === "undefined") return;
      const el = document.getElementById("agentDistributionChart");
      if (!el) return;
      if (this.charts.agentsDonut) {
        try { this.charts.agentsDonut.destroy(); } catch (e) {}
        this.charts.agentsDonut = null;
      }
      const dist = this.agentDistribution();
      if (!dist.length) return;
      const labels = dist.map(d => d.name);
      const series = dist.map(d => d.count);
      const colors = dist.map(d => d.color);
      const ink   = this.cssVar("--ink");
      const muted = this.cssVar("--ink-muted");
      const total = series.reduce((a, b) => a + b, 0);

      this.charts.agentsDonut = new ApexCharts(el, {
        chart: { type: "donut", height: 240, background: "transparent",
                 fontFamily: "Inter, system-ui, sans-serif",
                 animations: { enabled: true, speed: 400 } },
        series, labels, colors,
        theme: { mode: this.chartMode() },
        legend: { show: false },
        dataLabels: { enabled: false },
        stroke: { width: 2, colors: [this.cssVar("--bg")] },
        plotOptions: {
          pie: {
            donut: {
              size: "70%",
              labels: {
                show: true,
                name:  { color: muted, fontSize: "11px", fontWeight: 700, offsetY: -2 },
                value: { color: ink,   fontSize: "26px", fontWeight: 900, offsetY: 6,
                         formatter: (v) => this.formatNumber(v) },
                total: { show: true, label: "TASKS", color: muted,
                         fontSize: "10px", fontWeight: 800, letterSpacing: "0.18em",
                         formatter: () => this.formatNumber(total) },
              },
            },
          },
        },
        tooltip: { theme: this.chartMode(), y: { formatter: (v) => v + " tasks" } },
      });
      this.charts.agentsDonut.render();
    },
    ringSegments() {
      const total = this.overview ? this.overview.totals.agent_tasks : 0;
      const breakdown = this.overview ? this.overview.agent_breakdown : [];
      const TOTAL_BLOCKS = 64;
      const cx = 120, cy = 120;
      const radius = 92;        // ring centerline
      const blockW = 3.5;       // capsule short axis (thinner = more refined)
      const blockH = 16;        // capsule long axis

      // Decide how many lit blocks each agent gets (largest-remainder so totals add up,
      // and every agent with any tasks gets at least one block).
      const lit = breakdown.length;
      const gapBetween = 2;       // mandated 2-block gap between agents
      const totalGap = lit * gapBetween;
      const litCapacity = TOTAL_BLOCKS - totalGap;

      let allocated = breakdown.map((a) => {
        if (!total) return 0;
        const exact = (a.count / total) * litCapacity;
        return { agent: a, exact, base: Math.floor(exact), frac: exact - Math.floor(exact) };
      });
      let used = allocated.reduce((s, x) => s + (x.base || 0), 0);
      let remaining = litCapacity - used;
      // give leftover blocks to highest fractional remainders
      allocated.sort((a, b) => b.frac - a.frac);
      for (let i = 0; i < allocated.length && remaining > 0; i++) {
        allocated[i].base += 1;
        remaining -= 1;
      }
      // ensure agents with non-zero tasks get at least one block (steal from largest if needed)
      for (const slot of allocated) {
        if (slot.agent.count > 0 && slot.base === 0) {
          const donor = allocated.slice().sort((a, b) => b.base - a.base)[0];
          if (donor && donor.base > 1) {
            donor.base -= 1;
            slot.base = 1;
          }
        }
      }
      // reorder back to registry order
      const byKey = new Map(allocated.map(x => [x.agent.key, x]));
      const ordered = breakdown.map(a => byKey.get(a.key));

      // Walk the 64 slots and tag each one
      const segs = [];
      let cursor = 0;
      for (let i = 0; i < TOTAL_BLOCKS; i++) {
        const angle = (i / TOTAL_BLOCKS) * 2 * Math.PI - Math.PI / 2; // start at 12 o'clock
        const x = cx + Math.cos(angle) * radius;
        const y = cy + Math.sin(angle) * radius;
        segs.push({
          index: i, angle: (angle * 180) / Math.PI, x, y, blockW, blockH,
          active: false, color: null, key: null,
        });
      }
      // fill consecutive runs per agent with 2-slot gaps
      let i = 0;
      for (const slot of ordered) {
        if (!slot) continue;
        for (let n = 0; n < slot.base; n++) {
          const s = segs[i % TOTAL_BLOCKS];
          s.active = true;
          s.color = slot.agent.color;
          s.key = slot.agent.key;
          i++;
        }
        i += gapBetween;
      }
      return segs;
    },

    ringSvg() {
      const segs = this.ringSegments();
      const total = this.overview ? this.overview.totals.agent_tasks : 0;
      // Minimal donut — flat fills, no glow filters, no shadows.
      const blocks = segs.map(seg => {
        const x = seg.x - seg.blockW / 2;
        const y = seg.y - seg.blockH / 2;
        const fill = seg.active ? seg.color : "rgba(255,255,255,0.06)";
        return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${seg.blockW}" height="${seg.blockH}" rx="2" ry="2"
          transform="rotate(${(seg.angle + 90).toFixed(2)} ${seg.x.toFixed(2)} ${seg.y.toFixed(2)})"
          fill="${fill}" class="${seg.active ? 'ring-block on' : 'ring-block'}"></rect>`;
      }).join("");
      const totalText = this.formatNumber(total);
      return `${blocks}` +
        `<circle cx="120" cy="120" r="62" class="ring-core"></circle>` +
        `<text x="120" y="116" text-anchor="middle" class="ring-total">${totalText}</text>` +
        `<text x="120" y="138" text-anchor="middle" class="ring-caption">TASKS</text>`;
    },

    // ============================================================
    // HEATMAP cell styling — opacity scaled by row count vs grid max.
    // No glow, no shadow — clean tonal mapping only.
    // ============================================================
    cellStyle(row, cell) {
      if (!cell || cell.count <= 0) return {}; // dark cell stays dark
      const max = Math.max(1, this.overview ? this.overview.heatmap.max_count : 1);
      const t = Math.min(1, cell.count / max);
      const eased = 0.20 + Math.pow(t, 0.55) * 0.80;
      return {
        background: row.color,
        opacity: eased.toFixed(3),
        borderColor: "transparent",
      };
    },
    mountCharts() {
      // 30d activity chart — only when visible and ApexCharts has loaded
      if (typeof ApexCharts === "undefined") return;
      if (this.activeTab !== "overview") return;
      const el = document.getElementById("overviewChart");
      if (!el) return;

      // tear down previous instance (theme flip etc.)
      if (this.charts.overview) {
        try { this.charts.overview.destroy(); } catch (e) {}
        this.charts.overview = null;
      }

      const muted    = this.cssVar("--ink-muted");
      const accent   = this.cssVar("--accent")    || "#c8ff00";
      const emerald  = this.cssVar("--c-emerald") || "#2be08a";
      const accentRgb = this.cssVar("--accent-rgb") || "200, 255, 0";
      const grid     = this.cssVar("--soft-1");
      const dim      = `rgba(${accentRgb}, 0.42)`;   // dimmed lime → reads as the "inactive" hatched bars

      // 30d series → pill bars; the single hottest day is the lime focal bar.
      const data = (this.overview ? this.overview.daily_activity : []).map(d => ({ x: d.date, y: d.count }));
      const peak = data.length ? data.reduce((m, d, i) => (d.y > data[m].y ? i : m), 0) : -1;
      // distributed per-bar colours: bright lime on the peak, dim lime hatch on the rest
      const barColors = data.map((d, i) => (i === peak ? accent : dim));

      this.charts.overview = new ApexCharts(el, {
        chart: {
          type: "bar", height: 240, toolbar: { show: false }, background: "transparent",
          fontFamily: "Inter, system-ui, sans-serif",
          animations: { enabled: true, easing: "easeinout", speed: 600,
                        animateGradually: { enabled: true, delay: 90 },
                        dynamicAnimation: { enabled: true, speed: 350 } },
          parentHeightOffset: 0, sparkline: { enabled: false },
        },
        theme: { mode: this.chartMode() },
        series: [{ name: "Tasks", data }],
        plotOptions: {
          bar: {
            distributed: true,          // per-bar colours
            columnWidth: "56%",
            borderRadius: 6,
            borderRadiusApplication: "end",   // rounded pill tops
          },
        },
        colors: barColors,
        // Textured bars (reference DNA): a uniform diagonal hatch. ApexCharts
        // resolves pattern.style per-series (not per-bar), so a single distributed
        // series can't mix styles — the peak still reads as the focal via its
        // bright lime colour + the floating callout; the rest are dim-lime hatch.
        fill: {
          type: "pattern",
          opacity: 1,
          pattern: { style: "slantedLines", width: 7, height: 7, strokeWidth: 1.6 },
        },
        xaxis: {
          type: "datetime",
          labels: { style: { colors: muted, fontWeight: 600, fontSize: "10.5px" }, datetimeUTC: false,
                    format: "MMM d" },
          axisBorder: { color: grid }, axisTicks: { color: grid },
        },
        yaxis: {
          labels: { style: { colors: muted, fontWeight: 600, fontSize: "10.5px" }, formatter: (v) => Math.round(v) },
          tickAmount: 4,
        },
        grid: { borderColor: grid, strokeDashArray: 4, padding: { left: 6, right: 6 } },
        legend: { show: false },          // distributed bars try to legend every date
        dataLabels: { enabled: false },
        states: { hover: { filter: { type: "lighten", value: 0.12 } } },
        annotations: peak >= 0 ? {
          points: [{
            x: new Date(data[peak].x).getTime(),
            y: data[peak].y,
            marker: { size: 0 },
            label: {
              text: "▲ " + data[peak].y + (data[peak].y === 1 ? " task" : " tasks"),
              borderColor: accent, borderWidth: 0, offsetY: -4,
              style: { background: accent, color: "#000", fontWeight: 800,
                       fontSize: "11px", fontFamily: "Inter, system-ui, sans-serif",
                       padding: { left: 8, right: 8, top: 3, bottom: 3 } },
            },
          }],
        } : {},
        tooltip: {
          theme: "dark",
          x: { format: "MMM d" },
          y: { formatter: (v) => v + (v === 1 ? " task" : " tasks") },
          marker: { show: false },
        },
      });
      this.charts.overview.render();
    },

    cssVar(name) {
      return getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim() || "#888";
    },

    // ============================================================
    // DESIGN REFERENCE
    // ============================================================
    async loadDesignRef() {
      try {
        const r = await fetch("/api/design-reference");
        this.designRef = await r.json();
      } catch (e) {
        this.designRef = { error: String(e), root: "?", template: null,
                           screenshots: [], archive: [] };
      }
    },

    onPick(ev) {
      this.picked = [...(ev.target.files || [])];
    },
    onDrop(ev) {
      const dt = ev.dataTransfer;
      if (!dt) return;
      this.picked = [...(dt.files || [])];
    },

    async uploadPicked() {
      if (!this.picked.length || this.uploading) return;
      this.uploading = true;
      this.uploadResult = "";
      const fd = new FormData();
      for (const f of this.picked) fd.append("files", f, f.name);
      try {
        const r = await fetch("/api/design-reference/upload", { method: "POST", body: fd });
        const j = await r.json();
        this.uploadResult = JSON.stringify(j, null, 2);
        this.picked = [];
        if (this.$refs.fileInput) this.$refs.fileInput.value = "";
        await this.loadDesignRef();
      } catch (e) {
        this.uploadResult = "Error: " + String(e);
      } finally {
        this.uploading = false;
      }
    },

    // ============================================================
    // QUIZ
    // ============================================================
    async loadQuizSubjects() {
      try {
        const r = await fetch("/api/subjects");
        const j = await r.json();
        this.quiz.subjects = j.subjects || [];
        if (this.quiz.subject && !this.quiz.subjects.includes(this.quiz.subject)) {
          this.quiz.subject = "";
        }
      } catch (e) {
        this.quiz.error = "Failed to load subjects: " + String(e);
      }
    },

    async loadQuizList() {
      if (!this.quiz.subject) { this.quiz.quizzes = []; return; }
      this.quiz.loading = true;
      this.quiz.error = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.quiz.subject) + "/quiz");
        const j = await r.json();
        this.quiz.quizzes = j.items || [];
        await this.loadQuizAttempts();
      } catch (e) {
        this.quiz.error = "Failed to load quizzes: " + String(e);
      } finally {
        this.quiz.loading = false;
      }
    },

    async generateQuiz() {
      if (!this.quiz.subject || this.quiz.generating) return;
      this.quiz.generating = true;
      this.quiz.error = "";
      this.quiz.message = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.quiz.subject) + "/quiz/generate", { method: "POST" });
        const j = await r.json();
        if (!r.ok) {
          this.quiz.error = j.detail || j.message || "HTTP " + r.status;
        } else {
          this.quiz.message = j.message || "Generating quiz...";
          setTimeout(() => this.loadQuizList(), 4000);
        }
      } catch (e) {
        this.quiz.error = "Network error: " + String(e);
      } finally {
        this.quiz.generating = false;
      }
    },

    async selectQuiz(q) {
      if (!q || !this.quiz.subject) return;
      this.quiz.loading = true;
      this.quiz.error = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.quiz.subject) + "/quiz/" + encodeURIComponent(q.filename));
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "HTTP " + r.status);
        this.quiz.selectedQuiz = q;
        this.quiz.questions = j.questions || [];
        this.quiz.currentIndex = 0;
        this.quiz.selectedOption = "";
        this.quiz.confirmed = false;
        this.quiz.answers = [];
        this.quiz.score = 0;
        this.quiz.startTime = 0;
        this.quiz.elapsed = 0;
        this.quiz.phase = "active";
        this.startQuizTimer();
      } catch (e) {
        this.quiz.error = "Failed to load quiz: " + String(e);
      } finally {
        this.quiz.loading = false;
      }
    },

    startQuizTimer() {
      this.quiz.startTime = Date.now();
      this.quiz.elapsed = 0;
      if (this.quiz.timerInterval) clearInterval(this.quiz.timerInterval);
      this.quiz.timerInterval = setInterval(() => {
        this.quiz.elapsed = Math.floor((Date.now() - this.quiz.startTime) / 1000);
      }, 1000);
    },

    stopQuizTimer() {
      if (this.quiz.timerInterval) {
        clearInterval(this.quiz.timerInterval);
        this.quiz.timerInterval = null;
      }
    },

    selectOption(opt) {
      if (this.quiz.confirmed) return;
      this.quiz.selectedOption = opt;
    },

    confirmAnswer() {
      if (!this.quiz.selectedOption || this.quiz.confirmed) return;
      const q = this.quiz.questions[this.quiz.currentIndex];
      const correct = String(q.correct).trim();
      const selected = String(this.quiz.selectedOption).trim();
      const isCorrect = selected.toLowerCase() === correct.toLowerCase();
      this.quiz.answers.push({
        question: q.question,
        selected: selected,
        correctAnswer: correct,
        correct: isCorrect,
        explanation: q.explanation || "",
      });
      if (isCorrect) this.quiz.score += 1;
      this.quiz.confirmed = true;
    },

    nextQuestion() {
      if (!this.quiz.confirmed) return;
      if (this.quiz.currentIndex + 1 >= this.quiz.questions.length) {
        this.finishQuiz();
        return;
      }
      this.quiz.currentIndex += 1;
      this.quiz.selectedOption = "";
      this.quiz.confirmed = false;
    },

    finishQuiz() {
      this.stopQuizTimer();
      this.quiz.phase = "results";
      this.saveAttempt();
    },

    async saveAttempt() {
      if (!this.quiz.selectedQuiz || !this.quiz.subject) return;
      try {
        await fetch("/api/quiz/attempt", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            subject: this.quiz.subject,
            filename: this.quiz.selectedQuiz.filename,
            score: this.quiz.score,
            total: this.quiz.questions.length,
            time_seconds: this.quiz.elapsed,
          }),
        });
      } catch (e) { /* silent */ }
    },

    async loadQuizAttempts() {
      this.quiz.statsLoading = true;
      try {
        const url = this.quiz.subject
          ? "/api/quiz/attempts?subject=" + encodeURIComponent(this.quiz.subject)
          : "/api/quiz/attempts";
        const r = await fetch(url);
        const j = await r.json();
        this.quiz.attempts = j.items || [];
        this.quiz.averages = j.averages || [];
      } catch (e) {
        this.quiz.attempts = [];
        this.quiz.averages = [];
      } finally {
        this.quiz.statsLoading = false;
      }
    },

    retakeQuiz() {
      this.quiz.currentIndex = 0;
      this.quiz.selectedOption = "";
      this.quiz.confirmed = false;
      this.quiz.answers = [];
      this.quiz.score = 0;
      this.quiz.phase = "active";
      this.startQuizTimer();
    },

    resetQuiz() {
      this.stopQuizTimer();
      this.quiz.phase = "select";
      this.quiz.selectedQuiz = null;
      this.quiz.questions = [];
      this.quiz.currentIndex = 0;
      this.quiz.selectedOption = "";
      this.quiz.confirmed = false;
      this.quiz.answers = [];
      this.quiz.score = 0;
      this.quiz.elapsed = 0;
      this.loadQuizList();
    },

    quizProgressPct() {
      const total = this.quiz.questions.length || 1;
      return Math.round(((this.quiz.currentIndex + (this.quiz.confirmed ? 1 : 0)) / total) * 100);
    },

    formatTime(sec) {
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return m + ":" + (s < 10 ? "0" + s : s);
    },

    formatDate(iso) {
      if (!iso) return "";
      try {
        return new Date(iso).toLocaleString(undefined, {
          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
        });
      } catch { return iso; }
    },

    quizResultMessage() {
      const pct = this.quiz.questions.length ? (this.quiz.score / this.quiz.questions.length) : 0;
      if (pct >= 0.9) return "Outstanding! You mastered this material.";
      if (pct >= 0.7) return "Great work! Solid understanding.";
      if (pct >= 0.5) return "Good effort. Review the missed topics.";
      return "Keep practicing. You'll improve with each attempt.";
    },

    // ============================================================
    // FLASHCARDS
    // ============================================================
    async loadFlashcardSubjects() {
      try {
        const r = await fetch("/api/subjects");
        const j = await r.json();
        this.flashcard.subjects = j.subjects || [];
        if (this.flashcard.subject && !this.flashcard.subjects.includes(this.flashcard.subject)) {
          this.flashcard.subject = "";
        }
      } catch (e) {
        this.flashcard.error = "Failed to load subjects: " + String(e);
      }
    },

    async loadFlashcardList() {
      if (!this.flashcard.subject) { this.flashcard.decks = []; return; }
      this.flashcard.loading = true;
      this.flashcard.error = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.flashcard.subject) + "/flashcard");
        const j = await r.json();
        this.flashcard.decks = j.items || [];
      } catch (e) {
        this.flashcard.error = "Failed to load decks: " + String(e);
      } finally {
        this.flashcard.loading = false;
      }
    },

    async generateFlashcards() {
      if (!this.flashcard.subject || this.flashcard.generating) return;
      this.flashcard.generating = true;
      this.flashcard.error = "";
      this.flashcard.message = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.flashcard.subject) + "/flashcard/generate", { method: "POST" });
        const j = await r.json();
        if (!r.ok) {
          this.flashcard.error = j.detail || j.message || "HTTP " + r.status;
        } else {
          this.flashcard.message = j.message || "Generating deck...";
          setTimeout(() => this.loadFlashcardList(), 4000);
        }
      } catch (e) {
        this.flashcard.error = "Network error: " + String(e);
      } finally {
        this.flashcard.generating = false;
      }
    },

    async selectDeck(d) {
      if (!d || !this.flashcard.subject) return;
      this.flashcard.loading = true;
      this.flashcard.error = "";
      try {
        const r = await fetch("/api/subjects/" + encodeURIComponent(this.flashcard.subject) + "/flashcard/" + encodeURIComponent(d.filename));
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "HTTP " + r.status);
        this.flashcard.selectedDeck = d;
        this.flashcard.cards = j.cards || [];
        this.flashcard.originalOrder = j.cards ? j.cards.slice() : [];
        this.flashcard.currentIndex = 0;
        this.flashcard.shuffled = false;
        this.flashcard.phase = "read";
      } catch (e) {
        this.flashcard.error = "Failed to load deck: " + String(e);
      } finally {
        this.flashcard.loading = false;
      }
    },

    nextFlashcard() {
      if (!this.flashcard.cards.length) return;
      this.flashcard.currentIndex = (this.flashcard.currentIndex + 1) % this.flashcard.cards.length;
    },

    prevFlashcard() {
      if (!this.flashcard.cards.length) return;
      this.flashcard.currentIndex = (this.flashcard.currentIndex - 1 + this.flashcard.cards.length) % this.flashcard.cards.length;
    },

    shuffleFlashcards() {
      if (!this.flashcard.cards.length) return;
      const arr = this.flashcard.cards.slice();
      for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      this.flashcard.cards = arr;
      this.flashcard.currentIndex = 0;
      this.flashcard.shuffled = true;
    },

    unshuffleFlashcards() {
      if (!this.flashcard.originalOrder.length) return;
      this.flashcard.cards = this.flashcard.originalOrder.slice();
      this.flashcard.currentIndex = 0;
      this.flashcard.shuffled = false;
    },

    resetFlashcards() {
      this.flashcard.phase = "select";
      this.flashcard.selectedDeck = null;
      this.flashcard.cards = [];
      this.flashcard.originalOrder = [];
      this.flashcard.currentIndex = 0;
      this.flashcard.shuffled = false;
      this.loadFlashcardList();
    },

    flashcardProgressPct() {
      const total = this.flashcard.cards.length || 1;
      return Math.round(((this.flashcard.currentIndex + 1) / total) * 100);
    },

    // ============================================================
    // TASKS BOARD
    // ============================================================
    tasksByStatus(status) {
      return this.tasks.items.filter((t) => t.status === status).sort((a, b) => (a.position || 0) - (b.position || 0));
    },

    async loadTasks() {
      try {
        const r = await fetch("/api/tasks");
        const j = await r.json();
        this.tasks.items = j.items || [];
        this.tasks.counts = j.counts || { todo: 0, in_progress: 0, done: 0 };
      } catch (e) {
        this.tasks.error = "Failed to load tasks: " + String(e);
      }
    },

    async addTask() {
      const title = this.tasks.newTitle.trim();
      if (!title || this.tasks.saving) return;
      this.tasks.saving = true;
      this.tasks.error = "";
      try {
        const r = await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title,
            subject: this.tasks.newSubject.trim(),
            status: "todo",
            position: Date.now(),
          }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || "HTTP " + r.status);
        this.tasks.newTitle = "";
        this.tasks.newSubject = "";
        await this.loadTasks();
      } catch (e) {
        this.tasks.error = String(e);
      } finally {
        this.tasks.saving = false;
      }
    },

    async deleteTask(task) {
      if (!task || !task.id) return;
      try {
        const r = await fetch("/api/tasks/" + encodeURIComponent(task.id), { method: "DELETE" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        await this.loadTasks();
      } catch (e) {
        this.tasks.error = String(e);
      }
    },

    dragTask(task, event) {
      this.tasks.draggingId = task.id;
      if (event && event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", task.id);
      }
    },

    endTaskDrag() {
      this.tasks.draggingId = null;
      this.tasks.dragOver = "";
    },

    async dropTask(status) {
      const id = this.tasks.draggingId;
      this.tasks.dragOver = "";
      this.tasks.draggingId = null;
      if (!id) return;
      const idx = this.tasks.items.findIndex((t) => t.id === id);
      if (idx === -1) return;
      const task = this.tasks.items[idx];
      if (task.status === status) return;
      task.status = status;
      task.position = Date.now();
      try {
        await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: task.id,
            title: task.title,
            subject: task.subject,
            status: task.status,
            position: task.position,
            created_at: task.created_at,
          }),
        });
        await this.loadTasks();
      } catch (e) {
        this.tasks.error = String(e);
      }
    },
    // Keyboard/AT path for moving a task between columns (drag-and-drop has no
    // non-pointer equivalent). x-model has already written task.status; persist
    // it exactly like dropTask so the move survives loadTasks().
    async changeTaskStatus(task) {
      task.position = Date.now();
      try {
        await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: task.id,
            title: task.title,
            subject: task.subject,
            status: task.status,
            position: task.position,
            created_at: task.created_at,
          }),
        });
        await this.loadTasks();
      } catch (e) {
        this.tasks.error = String(e);
      }
    },

    // ============================================================
    // KNOWLEDGE — NotebookLM (Google, via local notebooklm CLI -> /api/nlm/*)
    // ============================================================
    nlmReady() { return !!(this.nlmHealth && this.nlmHealth.cli && this.nlmHealth.auth); },
    nlmNotebookCount() { return this.nlmNotebooks.length; },
    nlmSourceCount() { return this.nlmSources.length; },
    nlmArtifactCount() { return this.nlmArtifacts.length; },
    nlmSelectedTitle() {
      const n = this.nlmNotebooks.find(x => x.id === this.nlmSelectedId);
      return n ? (n.title || n.name || "Untitled") : "\u2014";
    },
    nlmGenTypes() { return ["audio","video","report","quiz","flashcards","infographic","mind-map","data-table","slide-deck"]; },

    async loadNotebookLM() {
      this.nlmLoading = true;
      this.nlmError = "";
      try {
        const health = await fetchWithAuth("/api/nlm/health");
        this.nlmHealth = health;
        if (!health || !health.cli || !health.auth) { this.nlmNotebooks = []; return; }
        const resp = await fetchWithAuth("/api/nlm/notebooks");
        this.nlmNotebooks = (resp && resp.ok && Array.isArray(resp.notebooks)) ? resp.notebooks : [];
        if (this.nlmNotebooks.length && !this.nlmSelectedId) {
          await this.selectNlmNotebook(this.nlmNotebooks[0].id);
        }
        if (!this.nlmLanguages.length) this.loadNlmLanguages();
      } catch (e) {
        this.nlmError = String(e);
      } finally {
        this.nlmLoading = false;
      }
    },

    async refreshNlmNotebooks() {
      try {
        const resp = await fetchWithAuth("/api/nlm/notebooks");
        this.nlmNotebooks = (resp && resp.ok && Array.isArray(resp.notebooks)) ? resp.notebooks : [];
      } catch (e) { this.nlmError = String(e); }
    },

    async loadNlmLanguages() {
      try {
        const r = await fetchWithAuth("/api/nlm/languages");
        if (r && r.ok) {
          let langs = r.languages || [];
          if (langs && !Array.isArray(langs) && typeof langs === "object") {
            langs = Object.keys(langs).map(k => ({ code: k, name: langs[k] }));
          }
          this.nlmLanguages = langs;
          this.nlmCurrentLanguage = r.current || "en";
        }
      } catch (e) { /* non-fatal */ }
    },

    async selectNlmNotebook(id) {
      if (!id) return;
      this.nlmSelectedId = id;
      this.nlmError = "";
      try {
        const [meta, src, arts] = await Promise.all([
          fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(id)}/metadata`),
          fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(id)}/sources`),
          fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(id)}/artifacts`),
        ]);
        this.nlmMeta = (meta && meta.ok) ? meta.metadata : null;
        this.nlmSources = (src && src.ok && Array.isArray(src.sources)) ? src.sources : [];
        this.nlmArtifacts = (arts && arts.ok && Array.isArray(arts.artifacts)) ? arts.artifacts : [];
      } catch (e) { this.nlmError = String(e); }
    },

    async createNlmNotebook() {
      const title = (this.nlmNewTitle || "").trim();
      if (!title) return;
      this.nlmCreating = true; this.nlmError = "";
      try {
        const r = await fetchWithAuth("/api/nlm/notebooks", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        if (r && r.ok) {
          this.nlmNewTitle = "";
          await this.refreshNlmNotebooks();
          const id = r.notebook && (r.notebook.id || r.notebook.notebook_id);
          if (id) await this.selectNlmNotebook(id);
        } else {
          this.nlmError = (r && (r.detail || r.reason)) || "create failed";
        }
      } catch (e) { this.nlmError = String(e); }
      finally { this.nlmCreating = false; }
    },

    async addNlmSource() {
      const value = (this.nlmAddValue || "").trim();
      if (!value || !this.nlmSelectedId) return;
      this.nlmAdding = true; this.nlmError = "";
      try {
        const body = { kind: this.nlmAddKind, value };
        if (this.nlmAddKind === "research") body.mode = this.nlmAddMode;
        const r = await fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(this.nlmSelectedId)}/sources`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r && r.ok) { this.nlmAddValue = ""; await this.selectNlmNotebook(this.nlmSelectedId); }
        else this.nlmError = (r && (r.detail || r.reason)) || "add source failed";
      } catch (e) { this.nlmError = String(e); }
      finally { this.nlmAdding = false; }
    },

    async askNLM() {
      const q = (this.nlmAskInput || "").trim();
      if (!q || !this.nlmSelectedId) return;
      this.nlmAskLoading = true;
      this.nlmChatHistory.push({ role: "user", content: q, ts: Date.now() });
      this.nlmAskInput = "";
      try {
        const r = await fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(this.nlmSelectedId)}/ask`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q }),
        });
        if (r && r.ok) {
          this.nlmChatHistory.push({ role: "assistant", content: r.answer || "(no answer)", citations: r.citations || [], ts: Date.now() });
        } else {
          this.nlmChatHistory.push({ role: "assistant", content: "Error: " + ((r && (r.detail || r.reason)) || "ask failed"), citations: [], ts: Date.now() });
        }
        if (this.nlmChatHistory.length > 40) this.nlmChatHistory = this.nlmChatHistory.slice(-40);
      } catch (e) {
        this.nlmChatHistory.push({ role: "assistant", content: "Error: " + String(e), citations: [], ts: Date.now() });
      } finally {
        this.nlmAskLoading = false;
        this.$nextTick(() => { const el = this.$refs.nlmChatHistory; if (el) el.scrollTop = el.scrollHeight; });
      }
    },

    clearNlmChat() { this.nlmChatHistory = []; },

    async generateNlm() {
      if (!this.nlmSelectedId) return;
      const msg = `Generate a ${this.nlmGenType} for "${this.nlmSelectedTitle()}"? This can take several minutes and may hit Google rate limits.`;
      if (!window.confirm(msg)) return;
      this.nlmGenLoading = true; this.nlmError = "";
      try {
        const body = { type: this.nlmGenType, description: (this.nlmGenDesc || "").trim(), options: this.nlmGenOptions || {} };
        const r = await fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(this.nlmSelectedId)}/generate`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r && r.ok) {
          this.nlmGenDesc = "";
          await this.refreshNlmArtifacts();
          this.startNlmArtifactPolling();
        } else {
          this.nlmError = (r && (r.detail || r.reason)) || "generation failed";
        }
      } catch (e) { this.nlmError = String(e); }
      finally { this.nlmGenLoading = false; }
    },

    async refreshNlmArtifacts() {
      if (!this.nlmSelectedId) return;
      try {
        const r = await fetchWithAuth(`/api/nlm/notebooks/${encodeURIComponent(this.nlmSelectedId)}/artifacts`);
        if (r && r.ok && Array.isArray(r.artifacts)) this.nlmArtifacts = r.artifacts;
      } catch (e) { /* keep last good */ }
    },

    nlmArtifactTerminal(a) {
      const sv = (a && a.status ? a.status : "").toString().toLowerCase();
      return ["completed","complete","ready","done","error","failed","success"].some(x => sv.includes(x));
    },

    startNlmArtifactPolling() {
      if (this.nlmPollTimer) { clearInterval(this.nlmPollTimer); this.nlmPollTimer = null; }
      this.nlmPollCount = 0;
      this.nlmPollTimer = setInterval(async () => {
        if (!String(this.activeTab).startsWith("nlm-")) {
          clearInterval(this.nlmPollTimer); this.nlmPollTimer = null; return;
        }
        this.nlmPollCount++;
        await this.refreshNlmArtifacts();
        const pending = (this.nlmArtifacts || []).some(a => !this.nlmArtifactTerminal(a));
        if (!pending || this.nlmPollCount >= 30) { clearInterval(this.nlmPollTimer); this.nlmPollTimer = null; }
      }, 20000);
    },

    nlmArtifactDlType(art) {
      const raw = (art && (art.type || art.artifact_type) ? (art.type || art.artifact_type) : "").toString().toLowerCase();
      const map = {
        "audio": "audio", "audio overview": "audio", "podcast": "audio",
        "video": "video", "video overview": "video", "cinematic-video": "video",
        "report": "report", "briefing": "report", "study guide": "report",
        "quiz": "quiz", "flashcards": "flashcards", "flashcard": "flashcards",
        "infographic": "infographic", "mind-map": "mind-map", "mind map": "mind-map",
        "data-table": "data-table", "data table": "data-table",
        "slide-deck": "slide-deck", "slide deck": "slide-deck",
      };
      if (map[raw]) return map[raw];
      for (const k in map) { if (raw.includes(k)) return map[k]; }
      return "";
    },

    downloadNlmArtifact(art, fmt) {
      const t = this.nlmArtifactDlType(art);
      if (!t) { this.nlmError = "This artifact type cannot be downloaded yet."; return; }
      const id = art && (art.id || art.artifact_id || art.task_id);
      if (!id) { this.nlmError = "Artifact has no id."; return; }
      let url = `/api/nlm/notebooks/${encodeURIComponent(this.nlmSelectedId)}/artifacts/${encodeURIComponent(id)}/download?type=${encodeURIComponent(t)}`;
      if (fmt) url += `&format=${encodeURIComponent(fmt)}`;
      const a = document.createElement("a");
      a.href = url; a.rel = "noopener";
      document.body.appendChild(a); a.click(); a.remove();
    },

    async setNlmLanguage(code) {
      if (!code) return;
      try {
        const r = await fetchWithAuth("/api/nlm/languages", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        if (r && r.ok) this.nlmCurrentLanguage = code;
      } catch (e) { this.nlmError = String(e); }
    },

    // ============================================================
    // KNOWLEDGE — Obsidian vault (read-only filesystem access)
    // ============================================================
    async loadObsidian() {
      this.obsLoading = true;
      this.obsError = "";
      try {
        this.obsStatus = await fetchWithAuth("/api/obsidian/status");
        if (!this.obsStatus.ok) {
          this.obsNotes = [];
          return;
        }
        const params = new URLSearchParams({ limit: String(this.obsNoteLimit) });
        if (this.obsFolderFilter) params.set("folder", this.obsFolderFilter);
        const list = await fetchWithAuth(`/api/obsidian/notes?${params.toString()}`);
        this.obsNotes = list.notes || [];
      } catch (e) {
        this.obsError = String(e);
      } finally {
        this.obsLoading = false;
      }
    },

    async selectObsNote(path) {
      this.obsLoading = true;
      this.obsError = "";
      try {
        const note = await fetchWithAuth(`/api/obsidian/notes/${encodeURIComponent(path)}`);
        this.obsSelectedNote = note;
        this.obsView = "reader";
      } catch (e) {
        this.obsError = String(e);
      } finally {
        this.obsLoading = false;
      }
    },

    backToObsList() {
      this.obsView = "list";
      this.obsSelectedNote = null;
    },

    // ============================================================
    // PROFILE — a single uploadable avatar photo (replaces the "D" initial).
    // ============================================================
    async loadProfile() {
      try {
        const p = await fetchWithAuth("/api/profile");
        this.profile.avatarUrl = (p && p.avatar_url) || null;
      } catch (_) { /* keep the "D" initial fallback */ }
    },
    async uploadProfileAvatar(ev) {
      const input = ev && ev.target;
      const file = input && input.files && input.files[0];
      if (!file) return;
      if (!/^image\//.test(file.type || "")) { this.toast("Choose an image file", "bad"); if (input) input.value = ""; return; }
      if (file.size > 8 * 1024 * 1024) { this.toast("Image too large (max 8 MB)", "bad"); if (input) input.value = ""; return; }
      this.profile.uploading = true;
      try {
        const fd = new FormData();
        fd.append("file", file);
        const j = await fetchWithAuth("/api/profile/avatar", { method: "POST", body: fd });
        this.profile.avatarUrl = (j && j.avatar_url) || null;
        this.toast("Profile photo updated", "ok");
      } catch (e) {
        this.toast("Upload failed: " + (e && e.message ? e.message : e), "bad");
      } finally {
        this.profile.uploading = false;
        if (input) input.value = "";   // allow re-picking the same file
      }
    },

    // ============================================================
    // OBSIDIAN · BRAIN — vault link graph rendered as a living neural net.
    // Notes = neurons (size ∝ links), [[wikilinks]] = synapses, thought fires
    // along the connections. Custom canvas + a compact force sim; the live
    // engine is stashed off-band on the canvas element (cv.__brain) so Alpine
    // never proxies the per-frame simulation arrays.
    // ============================================================
    _brainPalette() {
      // Volt companion hues — each folder cluster takes the next jewel-tone.
      return ["#c8ff00", "#2be08a", "#36d6e7", "#b08cff", "#ffc24b", "#ff6b81",
              "#5ff0b0", "#7fe6f2", "#cdb8ff", "#ff9f45", "#d8ff45", "#ff9caa"];
    },
    brainFolderColor(f) {
      const pal = this._brainPalette();
      const i = this.brain.folders.indexOf(f);
      return pal[(i < 0 ? 0 : i) % pal.length];
    },
    brainCardStyle() {
      const h = this.brain.hover;
      if (!h) return "display:none";
      const eng = this.$refs.brainCanvas && this.$refs.brainCanvas.__brain;
      const W = eng ? eng.W : 99999, H = eng ? eng.H : 99999;
      const CW = 296, CH = 60;
      let left = h.sx + 16, top = h.sy + 16;
      if (left + CW > W) left = Math.max(6, h.sx - CW);   // flip left near the right edge
      if (top + CH > H) top = Math.max(6, h.sy - CH);     // flip up near the bottom edge
      return `left:${Math.round(left)}px; top:${Math.round(top)}px`;
    },
    async loadObsidianBrain() {
      const cv = this.$refs.brainCanvas;
      if (!cv) return;
      this.brain.loading = true;
      this.brain.error = "";
      try {
        const data = await fetchWithAuth("/api/obsidian/graph");
        if (!data || data.ok === false) {
          this.brain.error = (data && data.error) ? `Vault: ${data.error}` : "Vault not found.";
          this.brain.stats = { notes: 0, links: 0, orphans: 0 };
          this.brain.folders = [];
          this.brainStop();
          return;
        }
        this.brain.folders = data.folders || [];
        this.brain.stats = data.stats || {
          notes: (data.nodes || []).length, links: (data.links || []).length, orphans: 0,
        };
        this._brainSetup(data);
      } catch (e) {
        this.brain.error = String(e);
      } finally {
        this.brain.loading = false;
      }
    },
    brainStop() {
      const cv = this.$refs && this.$refs.brainCanvas;
      const eng = cv && cv.__brain;
      if (!eng) return;
      if (eng.raf) cancelAnimationFrame(eng.raf);
      if (eng.ro) { try { eng.ro.disconnect(); } catch (_) {} }
      const h = eng.handlers || {};
      if (h.onMove) cv.removeEventListener("pointermove", h.onMove);
      if (h.onDown) cv.removeEventListener("pointerdown", h.onDown);
      if (h.onUp) cv.removeEventListener("pointerup", h.onUp);
      if (h.onCancel) { cv.removeEventListener("pointercancel", h.onCancel); cv.removeEventListener("lostpointercapture", h.onCancel); }
      if (h.onLeave) cv.removeEventListener("pointerleave", h.onLeave);
      if (h.onWheel) cv.removeEventListener("wheel", h.onWheel);
      if (h.onResize) window.removeEventListener("resize", h.onResize);
      cv.__brain = null;
      this.brain.hover = null;
    },
    brainTogglePause() {
      this.brain.paused = !this.brain.paused;
      const cv = this.$refs.brainCanvas;
      if (!this.brain.paused && cv && cv.__brain) { cv.__brain.alpha = 0.4; this._brainReSettle(); }  // kick on resume
    },
    brainOnQuery() {
      const cv = this.$refs.brainCanvas;
      const eng = cv && cv.__brain;
      if (!eng) return;
      const q = (this.brain.query || "").trim().toLowerCase();
      eng.queryHits = q
        ? new Set(eng.nodes.filter((n) => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)).map((n) => n.id))
        : null;
    },
    brainFocusFolder(f) {
      const cv = this.$refs.brainCanvas;
      const eng = cv && cv.__brain;
      if (!eng) return;
      eng.queryHits = new Set(eng.nodes.filter((n) => n.folder === f).map((n) => n.id));
      eng.alpha = 0.5;
      this.brain.query = "";
      this._brainReSettle();
    },
    _brainResize() {
      const cv = this.$refs.brainCanvas;
      const eng = cv && cv.__brain;
      if (!eng) return;
      // Re-read devicePixelRatio every time — it CHANGES with browser zoom
      // (100→125→150%). clientWidth/Height are CSS px and also shift with zoom,
      // so recomputing both keeps the backing store matched to the displayed
      // size at any scale (no stretch, no hit-test drift).
      const dpr = Math.max(1, Math.min(2.5, window.devicePixelRatio || 1));
      const par = cv.parentElement;
      const W = cv.clientWidth || (par && par.clientWidth) || 800;
      const H = cv.clientHeight || (par && par.clientHeight) || 520;
      const changed = Math.abs(W - eng.W) > 1 || Math.abs(H - eng.H) > 1 || dpr !== eng.dpr;
      cv.width = Math.max(1, Math.round(W * dpr));
      cv.height = Math.max(1, Math.round(H * dpr));
      eng.dpr = dpr; eng.W = W; eng.H = H;
      if (changed) {
        eng.alpha = Math.max(eng.alpha, 0.4);     // re-energise so it recenters in the new frame
        if (eng.reduce) this._brainReSettle();    // no live loop under reduced-motion → hand-crank
        else this._brainDraw(eng);                // redraw now so it never flashes blank/stale
      }
    },
    _brainReSettle() {
      // Reduced-motion has no autonomous loop, so interactions that move nodes
      // (drag-release, folder focus, resume, resize) must hand-crank a short
      // settle + redraw — otherwise released nodes freeze mid-air.
      const cv = this.$refs.brainCanvas;
      const eng = cv && cv.__brain;
      if (!eng || !eng.reduce) return;
      for (let i = 0; i < 48; i++) this._brainStep(eng, true);
      this._brainDraw(eng);
    },
    _brainSetup(data) {
      this.brainStop();
      const cv = this.$refs.brainCanvas;
      if (!cv) return;
      const ctx = cv.getContext("2d");
      const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      const pal = this._brainPalette();
      const folders = this.brain.folders;
      const W = cv.clientWidth || 800, H = cv.clientHeight || 520;
      const cx = W / 2, cy = H / 2;
      const ns = data.nodes || [];
      const nodes = ns.map((n, i) => {
        const ang = (i / Math.max(1, ns.length)) * Math.PI * 2;
        const rad = 40 + Math.random() * Math.min(W, H) * 0.36;
        const fi = folders.indexOf(n.folder);
        return {
          id: n.id, label: n.label || n.id, folder: n.folder, deg: n.deg || 0,
          color: pal[(fi < 0 ? 0 : fi) % pal.length],
          r: 3.0 + Math.sqrt(n.deg || 0) * 3.6,
          x: cx + Math.cos(ang) * rad, y: cy + Math.sin(ang) * rad,
          vx: 0, vy: 0, phase: Math.random() * Math.PI * 2,
        };
      });
      const byId = {};
      nodes.forEach((n) => { byId[n.id] = n; });
      const links = (data.links || [])
        .map((l) => ({ s: byId[l.source], t: byId[l.target] }))
        .filter((l) => l.s && l.t);
      const adj = {};
      nodes.forEach((n) => { adj[n.id] = new Set(); });
      links.forEach((l) => { adj[l.s.id].add(l.t.id); adj[l.t.id].add(l.s.id); });

      const engine = {
        ctx, nodes, links, byId, adj,
        W, H, dpr: 1, reduce,
        cam: { x: 0, y: 0, zoom: 1 },
        alpha: 1, pulses: [],
        t0: (typeof performance !== "undefined" ? performance.now() : 0),
        raf: null, drag: null, panning: null, hoverId: null, queryHits: null,
        handlers: {},
      };
      cv.__brain = engine;
      this._brainResize();
      this._brainBindEvents(cv);

      // Pre-settle so the first painted frame is already organised, not a ring.
      // Scale iterations DOWN as the graph grows so a large vault never freezes
      // the main thread on open (the O(n²) step × fixed iters would stall).
      const base = reduce ? 300 : 70;
      const presettle = ns.length > 500 ? 14 : (ns.length > 200 ? 32 : base);
      for (let i = 0; i < presettle; i++) this._brainStep(engine, true);

      const self = this;
      const loop = () => {
        const eng = cv.__brain;
        if (!eng) return;                         // stopped → end the loop
        eng.raf = requestAnimationFrame(loop);
        if (self.activeTab !== "obsidian-graph" || document.hidden) return;  // idle off-screen
        if (!self.brain.paused && !eng.reduce) self._brainStep(eng, false);
        self._brainDraw(eng);
      };
      engine.raf = requestAnimationFrame(loop);
    },
    _brainStep(eng, settle) {
      const nodes = eng.nodes, links = eng.links, n = nodes.length;
      if (!n) return;
      if (settle) eng.alpha = 0.6;
      else eng.alpha += (0.05 - eng.alpha) * 0.02;   // relax toward a small idle warmth (stays alive)
      const a = eng.alpha;
      const W = eng.W, H = eng.H, cx = W / 2, cy = H / 2;
      const REPULSE = 5200, SPRING = 0.02, REST = 62, CENTER = 0.013, DAMP = 0.85;

      for (let i = 0; i < n; i++) {
        const p = nodes[i];
        for (let j = i + 1; j < n; j++) {
          const q = nodes[j];
          let dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
          // Soften the 1/d² so a near-coincident pair can't spike to a 5-figure
          // impulse and fling a node off-canvas (paired with the velocity clamp).
          const d = Math.sqrt(d2), f = (REPULSE * a) / Math.max(d2, 16);
          const fx = (dx / d) * f, fy = (dy / d) * f;
          p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
        }
      }
      for (const l of links) {
        const p = l.s, q = l.t;
        let dx = q.x - p.x, dy = q.y - p.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = SPRING * a * (d - REST);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        p.vx += fx; p.vy += fy; q.vx -= fx; q.vy -= fy;
      }
      const drag = eng.drag;
      for (let i = 0; i < n; i++) {
        const p = nodes[i];
        p.vx += (cx - p.x) * CENTER * a;
        p.vy += (cy - p.y) * CENTER * a;
        p.vx *= DAMP; p.vy *= DAMP;
        const sp = Math.hypot(p.vx, p.vy);          // hard speed clamp → never explodes
        if (sp > 40) { const k = 40 / sp; p.vx *= k; p.vy *= k; }
        if (drag && drag.node === p) continue;     // pinned to cursor while dragged
        p.x += p.vx; p.y += p.vy;
      }

      // Fire action potentials along synapses — the "live" neural sparking.
      if (!settle && links.length && eng.pulses.length < 44 && Math.random() < 0.32) {
        const l = links[(Math.random() * links.length) | 0];
        const fwd = Math.random() < 0.5;
        const s = fwd ? l.s : l.t, t = fwd ? l.t : l.s;
        eng.pulses.push({ s, t, p: 0, speed: 0.012 + Math.random() * 0.022, color: s.color });
      }
      for (const pu of eng.pulses) pu.p += pu.speed;
      eng.pulses = eng.pulses.filter((pu) => pu.p < 1);
    },
    _brainDraw(eng) {
      const ctx = eng.ctx, dpr = eng.dpr, W = eng.W, H = eng.H, cam = eng.cam;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.save();
      ctx.translate(cam.x, cam.y);
      ctx.scale(cam.zoom, cam.zoom);

      const time = ((typeof performance !== "undefined" ? performance.now() : 0) - eng.t0) / 1000;
      const hoverId = eng.hoverId;
      const hoverAdj = hoverId ? eng.adj[hoverId] : null;
      const hits = eng.queryHits;
      // Glow + per-link gradients are the costly bits; switch them off above a
      // node budget so a large vault degrades to flat-but-smooth instead of janky.
      const glow = eng.nodes.length <= 400;

      // synapses
      for (const l of eng.links) {
        const active = hoverId && (l.s.id === hoverId || l.t.id === hoverId);
        const dim = hoverId && !active;
        if (glow) {
          const grad = ctx.createLinearGradient(l.s.x, l.s.y, l.t.x, l.t.y);
          grad.addColorStop(0, l.s.color);
          grad.addColorStop(1, l.t.color);
          ctx.strokeStyle = grad;
        } else {
          ctx.strokeStyle = l.s.color;
        }
        ctx.globalAlpha = active ? 0.75 : (dim ? 0.04 : 0.15);
        ctx.lineWidth = active ? 1.8 : 1;
        ctx.beginPath();
        ctx.moveTo(l.s.x, l.s.y);
        ctx.lineTo(l.t.x, l.t.y);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;

      // firing pulses
      for (const pu of eng.pulses) {
        const x = pu.s.x + (pu.t.x - pu.s.x) * pu.p;
        const y = pu.s.y + (pu.t.y - pu.s.y) * pu.p;
        ctx.globalAlpha = Math.sin(pu.p * Math.PI);   // brightest mid-flight
        ctx.fillStyle = pu.color;
        ctx.shadowColor = pu.color; ctx.shadowBlur = 11;
        ctx.beginPath(); ctx.arc(x, y, 2.3, 0, Math.PI * 2); ctx.fill();
      }
      ctx.shadowBlur = 0; ctx.globalAlpha = 1;

      // neurons
      for (const p of eng.nodes) {
        const isHover = hoverId && p.id === hoverId;
        const isNeighbor = hoverAdj && hoverAdj.has(p.id);
        const isHit = hits ? hits.has(p.id) : false;
        const dim = (hoverId && !isHover && !isNeighbor) || (hits && !isHit);
        const breathe = eng.reduce ? 1 : (1 + Math.sin(time * 1.6 + p.phase) * 0.10);
        const r = p.r * breathe * (isHover ? 1.5 : 1);
        ctx.globalAlpha = dim ? 0.16 : 1;
        ctx.shadowColor = p.color;
        // cap the blur kernel (a deg-400 hub would otherwise blur ~82px) and drop
        // glow entirely above the node budget, except the single hovered neuron.
        ctx.shadowBlur = dim ? 0 : (isHover ? 26 : (glow ? Math.min(18, 7 + p.r) : 0));
        const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, Math.max(0.5, r));
        g.addColorStop(0, "#ffffff");
        g.addColorStop(0.38, p.color);
        g.addColorStop(1, p.color);
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(0.5, r), 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
        if (!dim && (isHover || isHit || p.r > 7)) {
          ctx.globalAlpha = isHover ? 1 : 0.82;
          ctx.fillStyle = isHover ? "#ffffff" : "rgba(255,255,255,0.72)";
          ctx.font = `${isHover ? 13 : 11}px Inter, system-ui, sans-serif`;
          ctx.textAlign = "center";
          ctx.fillText(p.label, p.x, p.y - r - 6);
        }
      }
      ctx.globalAlpha = 1; ctx.shadowBlur = 0;
      ctx.restore();
    },
    _brainBindEvents(cv) {
      const self = this;
      const eng = cv.__brain;
      if (!eng) return;
      const world = (e) => {
        const rect = cv.getBoundingClientRect();
        const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
        return { sx, sy, wx: (sx - eng.cam.x) / eng.cam.zoom, wy: (sy - eng.cam.y) / eng.cam.zoom };
      };
      const pick = (wx, wy) => {
        let best = null, bestD = Infinity;
        for (const p of eng.nodes) {
          const dx = p.x - wx, dy = p.y - wy, d = Math.sqrt(dx * dx + dy * dy);
          if (d < p.r + 7 && d < bestD) { best = p; bestD = d; }
        }
        return best;
      };
      const onMove = (e) => {
        const { sx, sy, wx, wy } = world(e);
        if (eng.drag) {
          eng.drag.node.x = wx; eng.drag.node.y = wy;
          eng.drag.node.vx = 0; eng.drag.node.vy = 0;
          eng.drag.moved = true; eng.alpha = 0.5; return;
        }
        if (eng.panning) {
          eng.cam.x = eng.panning.camx + (e.clientX - eng.panning.x);
          eng.cam.y = eng.panning.camy + (e.clientY - eng.panning.y);
          return;
        }
        const hit = pick(wx, wy);
        eng.hoverId = hit ? hit.id : null;
        if (hit) {
          self.brain.hover = { label: hit.label, folder: hit.folder, deg: hit.deg, sx, sy };
          cv.style.cursor = "pointer";
        } else {
          if (self.brain.hover) self.brain.hover = null;
          cv.style.cursor = "grab";
        }
      };
      const onDown = (e) => {
        const { wx, wy } = world(e);
        const hit = pick(wx, wy);
        if (hit) eng.drag = { node: hit, moved: false };
        else eng.panning = { x: e.clientX, y: e.clientY, camx: eng.cam.x, camy: eng.cam.y };
        cv.style.cursor = "grabbing";
        if (cv.setPointerCapture && e.pointerId != null) { try { cv.setPointerCapture(e.pointerId); } catch (_) {} }
      };
      const onUp = () => {
        if (eng.drag && !eng.drag.moved) {              // a click (no drag) → open the note
          const id = eng.drag.node.id;
          self.brainStop();
          self.activeTab = "obsidian-vault";
          self.selectObsNote(id);
          return;
        }
        eng.drag = null; eng.panning = null;
        cv.style.cursor = "grab";
        self._brainReSettle();                          // reduced-motion: relax the released node
      };
      const onLeave = () => {
        eng.hoverId = null; eng.panning = null;
        if (self.brain.hover) self.brain.hover = null;
      };
      const onWheel = (e) => {
        e.preventDefault();
        const { sx, sy } = world(e);
        const z0 = eng.cam.zoom;
        const z1 = Math.max(0.3, Math.min(4, z0 * (e.deltaY < 0 ? 1.12 : 0.89)));
        eng.cam.x = sx - (sx - eng.cam.x) * (z1 / z0);
        eng.cam.y = sy - (sy - eng.cam.y) * (z1 / z0);
        eng.cam.zoom = z1;
      };
      const onCancel = () => {            // touch interruption / OS gesture / lost capture
        eng.drag = null; eng.panning = null;
        cv.style.cursor = "grab";
      };
      const onResize = () => self._brainResize();

      eng.handlers = { onMove, onDown, onUp, onCancel, onLeave, onWheel, onResize };
      cv.addEventListener("pointermove", onMove);
      cv.addEventListener("pointerdown", onDown);
      cv.addEventListener("pointerup", onUp);
      cv.addEventListener("pointercancel", onCancel);
      cv.addEventListener("lostpointercapture", onCancel);
      cv.addEventListener("pointerleave", onLeave);
      cv.addEventListener("wheel", onWheel, { passive: false });
      window.addEventListener("resize", onResize);
      // A ResizeObserver catches EVERY rendered-size change — browser zoom
      // (125%/150%), window resize, sidebar collapse — and re-syncs the canvas
      // backing store + DPR, so the graph never breaks or misaligns at any scale.
      if (typeof ResizeObserver !== "undefined") {
        try {
          const ro = new ResizeObserver(() => self._brainResize());
          ro.observe(cv);
          eng.ro = ro;
        } catch (_) {}
      }
      cv.style.cursor = "grab";
    },

    async obsDoSearch() {
      const q = (this.obsSearchQ || "").trim();
      if (!q) {
        this.obsSearchResults = [];
        return;
      }
      this.obsSearchLoading = true;
      try {
        const resp = await fetchWithAuth(`/api/obsidian/search?q=${encodeURIComponent(q)}&limit=20`);
        this.obsSearchResults = resp.results || [];
      } catch (e) {
        this.obsError = String(e);
      } finally {
        this.obsSearchLoading = false;
      }
    },

    obsFormatSize(b) {
      if (!b && b !== 0) return "—";
      if (b < 1024) return `${b}B`;
      if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)}KB`;
      return `${(b / (1024 * 1024)).toFixed(1)}MB`;
    },
    obsFormatDate(ts) {
      if (!ts) return "—";
      const d = new Date(ts * 1000);
      const now = Date.now() / 1000;
      const diff = (now - ts) / 86400;
      if (diff < 1) return `today, ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      if (diff < 2) return "yesterday";
      if (diff < 7) return `${Math.floor(diff)}d ago`;
      return d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
    },

    // ============================================================
    // KNOWLEDGE — Memory (unified know-me layer: /api/memory/*)
    // ============================================================
    async loadMemoryKnowledge() {
      this.memory.loading = true;
      this.memory.error = "";
      try {
        const r = await fetchWithAuth("/api/memory/knowledge");
        this.memory.knowledge = (r && r.ok) ? r : (r || null);
      } catch (e) {
        this.memory.error = String(e);
      } finally {
        this.memory.loading = false;
      }
    },

    async searchMemory() {
      const q = (this.memory.query || "").trim();
      if (!q) { this.memory.results = []; return; }
      this.memory.loading = true;
      this.memory.error = "";
      try {
        const r = await fetchWithAuth(`/api/memory/unified?q=${encodeURIComponent(q)}&limit=8`);
        this.memory.results = (r && Array.isArray(r.results)) ? r.results : [];
        this.memory.searched = true;
      } catch (e) {
        this.memory.error = String(e);
        this.memory.results = [];
        this.memory.searched = true;
      } finally {
        this.memory.loading = false;
      }
    },

    async syncMemory() {
      this.memory.syncing = true;
      this.memory.error = "";
      try {
        const r = await fetchWithAuth("/api/memory/sync", { method: "POST" });
        if (r && r.ok) {
          await this.loadMemoryKnowledge();
        } else {
          this.memory.error = (r && (r.detail || r.reason)) || "sync failed";
        }
      } catch (e) {
        this.memory.error = String(e);
      } finally {
        this.memory.syncing = false;
      }
    },

    memorySourceLabel(source) {
      return ({ memory: "Memory", fact: "Hermes fact", wiki: "Wiki" })[source] || (source || "Result");
    },

    // ============================================================
    // Daily Mission Briefing
    // ============================================================
    async loadDailyBriefing() {
      this.dailyBriefingLoading = true;
      this.dailyBriefingError = "";
      try {
        this.dailyBriefing = await fetchWithAuth("/api/briefing/today");
      } catch (e) {
        this.dailyBriefingError = String(e);
      } finally {
        this.dailyBriefingLoading = false;
      }
    },
    async refreshDailyBriefing() {
      this.dailyBriefingLoading = true;
      this.dailyBriefingError = "";
      try {
        this.dailyBriefing = await fetchWithAuth("/api/briefing/today?refresh=1");
      } catch (e) {
        this.dailyBriefingError = String(e);
      } finally {
        this.dailyBriefingLoading = false;
      }
    },
    briefingMarkdownToHtml(md) {
      if (!md) return "";
      // Tiny markdown pass: paragraphs, lists, bold, italic. The backend
      // already controls the bullet structure (it sends "- " lines).
      const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const lines = md.split("\n");
      const out = [];
      let inList = false;
      for (const raw of lines) {
        const line = raw.trimEnd();
        if (!line.trim()) { if (inList) { out.push("</ul>"); inList = false; } out.push(""); continue; }
        if (/^[-*]\s+/.test(line)) {
          if (!inList) { out.push("<ul class='briefing-list'>"); inList = true; }
          out.push("<li>" + esc(line.replace(/^[-*]\s+/, ""))
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/\*(.+?)\*/g, "<em>$1</em>")
            + "</li>");
        } else {
          if (inList) { out.push("</ul>"); inList = false; }
          out.push("<p>" + esc(line)
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/\*(.+?)\*/g, "<em>$1</em>") + "</p>");
        }
      }
      if (inList) out.push("</ul>");
      return out.join("\n");
    },

    // ============================================================
    // Global ⌘K search palette
    // ============================================================
    openGlobalSearch() {
      // WCAG 2.4.3: remember what was focused so we can restore it on close.
      this._paletteReturnFocus = document.activeElement;
      this.globalSearchOpen = true;
      this.paletteActiveIdx = 0;
      // Toggle visibility via class so we don't need x-show (which was
      // triggering Alpine transition errors during page init).
      const el = document.querySelector(".global-search-backdrop");
      if (el) el.classList.add("is-open");
      this.$nextTick(() => {
        const inp = this.$refs.globalSearchInput;
        if (inp) inp.focus();
      });
    },
    closeGlobalSearch() {
      this.globalSearchOpen = false;
      this.globalSearchQ = "";
      this.globalSearchResults = [];
      this.globalSearchError = "";
      this.paletteActiveIdx = 0;
      const el = document.querySelector(".global-search-backdrop");
      if (el) el.classList.remove("is-open");
      // WCAG 2.4.3: return focus to the element that opened the palette so it
      // doesn't fall back to <body> (the input is now display:none).
      const ret = this._paletteReturnFocus;
      this._paletteReturnFocus = null;
      if (ret && typeof ret.focus === "function" && document.contains(ret)) {
        this.$nextTick(() => { try { ret.focus(); } catch (_) {} });
      }
    },
    onGlobalSearchInput() {
      if (this._globalSearchTimer) clearTimeout(this._globalSearchTimer);
      this.paletteActiveIdx = 0;
      const q = (this.globalSearchQ || "").trim();
      if (!q) {
        this.globalSearchResults = [];
        this.globalSearchError = "";
        return;
      }
      this._globalSearchTimer = setTimeout(() => this.runGlobalSearch(), 220);
    },
    async runGlobalSearch() {
      const q = (this.globalSearchQ || "").trim();
      if (!q) return;
      this.globalSearchLoading = true;
      this.globalSearchError = "";
      try {
        const r = await fetchWithAuth(`/api/search/global?q=${encodeURIComponent(q)}&limit=20`);
        this.globalSearchResults = r.results || [];
      } catch (e) {
        this.globalSearchError = String(e);
      } finally {
        this.globalSearchLoading = false;
      }
    },
    activateGlobalResult(r) {
      if (!r) return;
      this.closeGlobalSearch();
      if (r.deep_link) this.goTo(r.deep_link);
      // If the result is an obsidian note, open the reader
      if (r.kind === "obsidian") {
        this.$nextTick(() => this.selectObsNote(r.id));
      }
    },
    globalSearchKindLabel(kind) {
      return ({
        "obsidian": "Obsidian",
        "subject-note": "Note",
      })[kind] || kind;
    },

    // ----- Command palette: local action registry (nav + agents + quick actions) -----
    _paletteActions() {
      if (this._paletteActionsCache) return this._paletteActionsCache;
      const out = [];
      const pushNav = (id, title, group) => out.push({
        kind: "nav", id: "nav:" + id, title, subtitle: "Go to " + group, run: () => this.goTo(id),
      });
      (this.nav || []).forEach((grp) => (grp.items || []).forEach((it) => {
        if (it.subTabs && it.subTabs.length) {
          it.subTabs.forEach((s) => pushNav(s.id, it.label + " · " + s.label, grp.group));
        } else {
          pushNav(it.id, it.label, grp.group);
        }
      }));
      Object.keys(this.AGENT_PALETTE || {}).forEach((name) => out.push({
        kind: "agent", id: "agent:" + name,
        title: "Chat with " + name.charAt(0).toUpperCase() + name.slice(1),
        subtitle: "Open the team chat", run: () => this.goTo("chat"),
      }));
      const qa = [
        ["Upload a file", "Add notes or PDFs", () => this.goTo("upload")],
        ["New research", "Start a Scholar research run", () => this.goTo("research")],
        ["Take a quiz", "Practice with Quizmaster", () => this.goTo("practice-quiz")],
        ["Review flashcards", "Spaced repetition", () => this.goTo("practice-flashcards")],
        ["Recall review", "Active-recall queue", () => this.goTo("recall")],
        ["Today's tracker", "Today's study plan", () => this.goTo("tracker-today")],
        ["Start a Pomodoro", "Focus timer", () => { this.goTo("planner-focus"); this.$nextTick(() => { try { this.startPomodoro(); } catch (_) {} }); }],
        ["Toggle theme", "Reset to the Volt theme", () => this.toggleTheme()],
        ["Toggle sidebar", "Collapse / expand the nav", () => this.toggleSidebar()],
      ];
      qa.forEach(([title, subtitle, run]) => out.push({ kind: "action", id: "act:" + title, title, subtitle, run }));
      this._paletteActionsCache = out;
      return out;
    },
    // subsequence/substring scorer: prefix > word-start > substring > subsequence
    _fuzzyScore(q, text) {
      if (!q) return 0;
      text = (text || "").toLowerCase();
      const idx = text.indexOf(q);
      if (idx === 0) return 100;
      if (idx > 0) return text[idx - 1] === " " ? 80 : 50;
      let ti = 0, qi = 0;
      while (ti < text.length && qi < q.length) { if (text[ti] === q[qi]) qi++; ti++; }
      return qi === q.length ? 20 : -1;
    },
    // merged, filtered, capped list driving both the rendered rows and keyboard nav
    paletteItems() {
      const q = (this.globalSearchQ || "").trim().toLowerCase();
      const acts = [];
      this._paletteActions().forEach((a) => {
        const score = this._fuzzyScore(q, a.title + " " + (a.subtitle || ""));
        if (score >= 0) acts.push({ ...a, _score: score });
      });
      acts.sort((x, y) => y._score - x._score);
      const remote = q ? (this.globalSearchResults || []) : [];
      return [...acts, ...remote].slice(0, 40);
    },
    paletteKindLabel(item) {
      if (!item) return "";
      if (typeof item.run === "function") {
        return item.kind === "nav" ? "Go" : (item.kind === "agent" ? "Agent" : "Action");
      }
      return this.globalSearchKindLabel(item.kind);
    },
    activatePaletteItem(item) {
      if (!item) return;
      if (typeof item.run === "function") { this.closeGlobalSearch(); item.run(); return; }
      this.activateGlobalResult(item);
    },
    paletteMove(delta) {
      const n = this.paletteItems().length;
      if (!n) return;
      this.paletteActiveIdx = ((this.paletteActiveIdx + delta) % n + n) % n;
      this.$nextTick(() => {
        const el = document.querySelector(".global-search-result.active");
        if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
      });
    },
    paletteKey(ev) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); this.paletteMove(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); this.paletteMove(-1); }
      else if (ev.key === "Enter") { ev.preventDefault(); this.activatePaletteItem(this.paletteItems()[this.paletteActiveIdx]); }
    },

    // ============================================================
    // FOCUS / POMODORO & STICKIES
    // ============================================================
    loadFocusDurations() {
      try {
        const saved = JSON.parse(localStorage.getItem("mission-control-focus-durations") || "{}");
        this.focus.durations = { ...this.focus.durations, ...saved };
      } catch (_) {}
      this.resetPomodoro();
    },
    async loadPomodoroToday() {
      try {
        const data = await fetch("/api/pomodoro-today").then((r) => r.json());
        this.focus.todayCount = data.count || 0;
      } catch (e) { this.focus.error = String(e); }
    },
    saveFocusDurations() {
      ["focus", "short", "long"].forEach((k) => {
        const val = Number(this.focus.durations[k]) || (k === "focus" ? 25 : k === "short" ? 5 : 15);
        this.focus.durations[k] = Math.max(1, Math.min(180, Math.round(val)));
      });
      localStorage.setItem("mission-control-focus-durations", JSON.stringify(this.focus.durations));
    },
    currentFocusMode() { return this.focusModes.find((m) => m.id === this.focus.mode) || this.focusModes[0]; },
    setFocusMode(mode) { this.focus.mode = mode; this.resetPomodoro(); },
    resetPomodoro() {
      this.pausePomodoro();
      this.saveFocusDurations();
      this.focus.total = (this.focus.durations[this.focus.mode] || 25) * 60;
      this.focus.remaining = this.focus.total;
    },
    startPomodoro() {
      if (this.focus.running) return;
      this.focus.running = true;
      this.focus.timer = setInterval(() => this.tickPomodoro(), 1000);
      // (a) Keep the screen awake for the duration of the session.
      this.requestWakeLock();
    },
    pausePomodoro() {
      if (this.focus.timer) clearInterval(this.focus.timer);
      this.focus.timer = null;
      this.focus.running = false;
      // (a) Release the wake lock whenever the timer stops running.
      this.releaseWakeLock();
    },
    tickPomodoro() {
      if (this.focus.remaining > 0) { this.focus.remaining -= 1; return; }
      this.completePomodoroSession();
    },
    async completePomodoroSession() {
      const mode = this.focus.mode;
      this.pausePomodoro();
      this.playFocusSound();
      // (g) Desktop notification when a session ends.
      if (mode === "focus") {
        this.fireNotification("Focus session complete", { body: "Nice work — time for a break.", tag: "pomodoro" });
      } else {
        this.fireNotification("Break over", { body: "Back to focus.", tag: "pomodoro" });
      }
      if (mode === "focus") {
        this.focus.completedInCycle = (this.focus.completedInCycle + 1) % 4;
        try {
          const data = await fetch("/api/pomodoro-today/increment", { method: "POST" }).then((r) => r.json());
          this.focus.todayCount = data.count || this.focus.todayCount + 1;
        } catch (e) { this.focus.error = String(e); }
        this.focus.mode = this.focus.completedInCycle === 0 ? "long" : "short";
      } else {
        this.focus.mode = "focus";
      }
      this.focus.total = (this.focus.durations[this.focus.mode] || 25) * 60;
      this.focus.remaining = this.focus.total;
      this.startPomodoro();
    },
    skipPomodoro() { this.focus.remaining = 0; this.completePomodoroSession(); },
    formatPomodoroTime(seconds) {
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    },
    pomodoroRing() {
      const radius = 96, circumference = 2 * Math.PI * radius;
      const pct = this.focus.total ? this.focus.remaining / this.focus.total : 1;
      const dash = Math.max(0, Math.min(circumference, circumference * pct));
      const color = this.currentFocusMode().color;
      const time = this.formatPomodoroTime(this.focus.remaining);
      const mode = this.currentFocusMode().label.toUpperCase();
      return `<svg viewBox="0 0 240 240" role="timer" aria-label="${mode} timer: ${time} remaining">
        <defs>
          <filter id="pomoGlow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="4" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
          <linearGradient id="pomoTextGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#ffffff"/><stop offset="100%" stop-color="${color}"/></linearGradient>
        </defs>
        <circle cx="120" cy="120" r="96" fill="none" stroke="rgba(255,255,255,.055)" stroke-width="16"/>
        <circle cx="120" cy="120" r="96" fill="none" stroke="${color}" stroke-width="16" stroke-linecap="round" stroke-dasharray="${dash} ${circumference}" transform="rotate(-90 120 120)" filter="url(#pomoGlow)"/>
        <circle cx="120" cy="120" r="68" fill="rgba(255,255,255,.028)" stroke="rgba(255,255,255,.075)"/>
        <text x="120" y="112" text-anchor="middle" dominant-baseline="middle" fill="url(#pomoTextGradient)" font-family="Inter, system-ui, sans-serif" font-size="36" font-weight="900" letter-spacing="-1.8">${time}</text>
        <text x="120" y="145" text-anchor="middle" dominant-baseline="middle" fill="rgba(241,243,250,.62)" font-family="Inter, system-ui, sans-serif" font-size="10" font-weight="900" letter-spacing="1.5">${mode}</text>
      </svg>`;
    },
    playFocusSound() {
      try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const gain = ctx.createGain(); gain.gain.value = 0.0001; gain.connect(ctx.destination);
        [523.25, 659.25, 783.99].forEach((freq, idx) => {
          const osc = ctx.createOscillator(); osc.type = "sine"; osc.frequency.value = freq; osc.connect(gain);
          const t = ctx.currentTime + idx * 0.12;
          gain.gain.exponentialRampToValueAtTime(0.045, t + 0.03);
          gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
          osc.start(t); osc.stop(t + 0.22);
        });
      } catch (_) {}
    },
    async loadStickies() {
      try {
        const data = await fetch("/api/stickies").then((r) => r.json());
        this.focus.stickies = data.items || [];
      } catch (e) { this.focus.error = String(e); }
    },
    async addSticky() {
      const content = this.focus.newSticky.trim();
      if (!content) return;
      this.focus.stickySaving = true; this.focus.error = "";
      try {
        const res = await fetch("/api/stickies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, color: this.focus.newStickyColor, position: this.focus.stickies.length }) });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Sticky save failed.");
        this.focus.newSticky = "";
        await this.loadStickies();
      } catch (e) { this.focus.error = String(e); }
      finally { this.focus.stickySaving = false; }
    },
    debouncedSaveSticky(note) {
      clearTimeout(this.focus.stickyTimers[note.id]);
      this.focus.stickyTimers[note.id] = setTimeout(() => this.saveSticky(note), 650);
    },
    async saveSticky(note) {
      const content = (note.content || "").trim();
      if (!content) return;
      try {
        const res = await fetch("/api/stickies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: note.id, content, color: note.color, position: note.position }) });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Sticky update failed.");
        Object.assign(note, data.sticky || {});
      } catch (e) { this.focus.error = String(e); }
    },
    async deleteSticky(note) {
      try {
        const res = await fetch(`/api/stickies/${encodeURIComponent(note.id)}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Sticky delete failed.");
        this.focus.stickies = this.focus.stickies.filter((s) => s.id !== note.id);
      } catch (e) { this.focus.error = String(e); }
    },
    stickyTilt(index) { return [-2.4, 1.8, -1.2, 2.2, -0.8, 1.1][index % 6]; },

    // ============================================================
    // CHAT
    // ============================================================
    async openChat() {
      if (!this.chat.agents.length) await this.loadChatAgents();
      this.loadChatHistory();
    },
    async loadChatAgents() {
      try {
        const r = await fetch("/api/agents");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.chat.agents = (data.agents || []).map((a) => ({
          key: a.key,
          name: a.name,
          emoji: a.emoji,
          color: a.color,
          role: a.role,
          icon: AGENT_ICONS[a.key] || AGENT_ICONS.default,
        }));
      } catch (e) { this.chat.error = String(e); }
    },
    selectChatAgent(key) {
      this.chat.selectedAgent = key;
      this.chat.history = [];
      this.chat.error = "";
      this.stopChatPoll();
      this.loadChatHistory();
      this._applyDevContextTheme(key);
    },
    // Directive L7: Terminal theme auto-activates when the Dev agent is the
    // focused context. We remember the prior theme and restore it on leaving
    // Dev — but only if the user hasn't manually changed theme in between.
    _applyDevContextTheme(agentKey) {
      const isDev = agentKey === "dev";
      if (isDev && this.theme !== "terminal") {
        this._themeBeforeDev = this.theme;
        this._autoTerminal = true;
        this.setTheme("terminal", true);
      } else if (!isDev && this._autoTerminal && this.theme === "terminal") {
        const restore = this._themeBeforeDev || "volt";
        this._autoTerminal = false;
        this._themeBeforeDev = null;
        this.setTheme(restore, true);
      }
    },
    async loadChatHistory() {
      const agent = this.chat.selectedAgent;
      if (!agent) return;
      try {
        const r = await fetch("/api/chat/" + encodeURIComponent(agent));
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.chat.history = data.history || [];
        this.chat.isRunning = data.is_running || false;
        this.chat.error = "";
        this.startChatPoll();
        this.$nextTick(() => this.scrollChatToBottom());
      } catch (e) { this.chat.error = String(e); }
    },
    startChatPoll() {
      this.stopChatPoll();
      this.chat.pollTimer = setInterval(() => this.pollChat(), this.chat.pollMs);
    },
    stopChatPoll() {
      if (this.chat.pollTimer) { clearInterval(this.chat.pollTimer); this.chat.pollTimer = null; }
    },
    async pollChat() {
      const agent = this.chat.selectedAgent;
      if (!agent || this.chat.loading) return;
      try {
        const r = await fetch("/api/chat/" + encodeURIComponent(agent));
        if (!r.ok) return;
        const data = await r.json();
        const prevRunning = this.chat.isRunning;
        const prevLen = this.chat.history.length;
        this.chat.history = data.history || [];
        this.chat.isRunning = data.is_running || false;
        if (this.chat.history.length !== prevLen || prevRunning !== this.chat.isRunning) {
          this.$nextTick(() => this.scrollChatToBottom());
        }
        // (g) Agent finished: running flipped true→false with a fresh reply.
        // Notify only if the document is hidden so we don't double-signal a
        // user who is already watching the conversation.
        if (prevRunning && !this.chat.isRunning && this.chat.history.length > prevLen && document.hidden) {
          const last = this.chat.history[this.chat.history.length - 1] || {};
          const name = (this.chat.selectedAgent || "Agent");
          this.fireNotification(name.charAt(0).toUpperCase() + name.slice(1) + " replied",
            { body: (last.text || "").slice(0, 140), tag: "chat-" + name });
        }
        // keep polling while on chat tab; user may send another message.
      } catch (e) { /* silent on poll */ }
    },
    async sendChatMessage() {
      const agent = this.chat.selectedAgent;
      const text = this.chat.input.trim();
      if (!agent || !text) return;
      this.chat.loading = true; this.chat.error = "";
      try {
        const r = await fetch("/api/chat/" + encodeURIComponent(agent), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        this.chat.input = "";
        this.chat.history.push(data.message);
        this.chat.isRunning = true;
        this.startChatPoll();
        this.$nextTick(() => this.scrollChatToBottom());
      } catch (e) { this.chat.error = String(e); }
      finally { this.chat.loading = false; }
    },
    onChatInputKey(e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this.sendChatMessage();
      }
      if (e.key === "Escape") {
        e.target.blur();
      }
    },
    scrollChatToBottom() {
      const el = document.querySelector(".chat-feed");
      if (el) el.scrollTop = el.scrollHeight;
    },
    chatBubbleClass(role) {
      return role === "user" ? "chat-bubble-user" : "chat-bubble-agent";
    },
    chatDateDivider(ts) {
      const d = new Date(ts);
      return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    },
    isNewChatDate(msg, index) {
      if (index === 0) return true;
      const prev = this.chat.history[index - 1];
      if (!prev || !prev.created_at) return true;
      return this.chatDateDivider(msg.created_at) !== this.chatDateDivider(prev.created_at);
    },
    async resetChat() {
      const agent = this.chat.selectedAgent;
      if (!agent) return;
      if (!confirm("Reset conversation with " + agent + "?")) return;
      try {
        const r = await fetch("/api/chat/" + encodeURIComponent(agent) + "/reset", { method: "POST" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.chat.history = [];
        this.chat.isRunning = false;
      } catch (e) { this.chat.error = String(e); }
    },
    chatContextPct() {
      return Math.min(100, Math.round((this.chat.history.length / this.chat.contextLimit) * 100));
    },

    formatBytes(n) {
      if (n == null) return "";
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
      return (n / 1024 / 1024).toFixed(2) + " MB";
    },

    // ============================================================
    // TASTE-SKILL ANIMATIONS (scroll reveal, stagger, parallax)
    // Uses IntersectionObserver per minimalist-ui §7 (no scroll events)
    // ============================================================
    mountRevealObserver() {
      if (!("IntersectionObserver" in window)) {
        // graceful fallback: just show everything
        document.querySelectorAll(".reveal").forEach((el) => el.classList.add("in"));
        return;
      }
      if (this._revealObserver) this._revealObserver.disconnect();
      this._revealObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add("in");
              this._revealObserver.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
      );
      this._observeReveals();
    },
    remountRevealObserver() {
      this.$nextTick(() => this._observeReveals());
    },
    _observeReveals() {
      if (!this._revealObserver) return;
      // Auto-tag common card/page sections with .reveal so the whole
      // site animates in without per-element HTML edits.
      const AUTO_SELECTOR = [
        ".glass-card",
        ".card",
        ".bento-cell",
        ".task-card",
        ".sticky-note",
        ".subject-card",
        ".lib-card",
        ".page-head",
        ".stat-tile",
        ".stat-card",
        ".ov-stat-hero",     // Overview stat strip — lime focal
        ".ov-stat-card",     // Overview stat strip — dark cards
        ".ov-rail-card",     // Overview right rail — agent perf + tools
        ".today-cell",       // Briefing — today-at-a-glance strip
        ".briefing-card",    // Briefing — the daily dispatch
        ".quick-card",       // Briefing — quick-action tiles
        ".activity-panel",   // Briefing — recent-activity timeline
        ".hero-display",
        ".hero-sub",
        ".library-card",
        ".upload-card",
        ".pipeline-flow li",
        ".pipeline-strip > div",
        ".quiz-card",
        ".flashcard-card",
        ".focus-card",
        ".chat-feed > div",
      ].join(", ");
      document.querySelectorAll(AUTO_SELECTOR).forEach((el) => {
        if (!el.classList.contains("reveal")) el.classList.add("reveal");
      });
      document.querySelectorAll(".reveal:not(.in)").forEach((el) => {
        this._revealObserver.observe(el);
      });
      // Auto-stagger for any list of .reveal inside .stagger-list
      document.querySelectorAll(".stagger-list").forEach((list) => {
        let i = 0;
        list.querySelectorAll(":scope > .reveal").forEach((el) => {
          el.style.setProperty("--stagger", i++);
        });
      });
      // Also auto-stagger top-level grid children (common dashboard pattern)
      document.querySelectorAll(".stat-grid, .pipeline-strip, .quick-grid, .overview-grid, .ov-stat-strip, .ov-bento, .ov-rail").forEach((grid) => {
        let i = 0;
        grid.querySelectorAll(":scope > *").forEach((el) => {
          el.style.setProperty("--stagger", i++);
        });
      });
      // Overview "main row" choreography: the bento reveals as a left block (stagger
      // 0..5); push the rail's cards to a later base so they cascade in afterwards.
      document.querySelectorAll(".ov-rail > .ov-rail-card").forEach((el, i) => {
        el.style.setProperty("--stagger", 6 + i);
      });
    },

    todayLabel() {
      const d = new Date();
      return d.toLocaleDateString(undefined, {
        weekday: "long", year: "numeric", month: "long", day: "numeric"
      });
    },

    // ============================================================
    // RECALL — active recall engine (FSRS spaced repetition + AI tutor)
    // ============================================================
    async loadRecall() {
      this.recall.loading = true;
      this.recall.error = "";
      try {
        const [stats, queue, readiness, ai] = await Promise.all([
          fetch("/api/study/stats").then((r) => r.json()).catch(() => null),
          fetch("/api/study/queue?limit=30").then((r) => r.json()).catch(() => null),
          fetch("/api/study/readiness").then((r) => r.json()).catch(() => null),
          fetch("/api/ai/health").then((r) => r.json()).catch(() => null),
        ]);
        this.recall.stats = stats || null;
        this.recall.queue = (queue && queue.queue) || [];
        this.recall.idx = 0;
        this.recall.readiness = (readiness && readiness.items) || [];
        this.recall.aiConfigured = !!(ai && ai.configured);
        this.recall.aiModel = (ai && ai.model) || "";
      } catch (e) {
        this.recall.error = String(e);
      } finally {
        this.recall.loading = false;
      }
    },
    async syncRecall() {
      if (this.recall.syncing) return;
      this.recall.syncing = true;
      try {
        await fetch("/api/study/sync", { method: "POST" });
        await this.loadRecall();
      } catch (e) {
        this.recall.error = String(e);
      } finally {
        this.recall.syncing = false;
      }
    },
    recallCurrent() {
      return this.recall.queue[this.recall.idx] || null;
    },
    async gradeRecall(rating) {
      const card = this.recallCurrent();
      if (!card || ![1, 2, 3, 4].includes(rating)) return;
      try {
        await fetch("/api/study/grade", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ card_id: card.id, rating }),
        });
      } catch (e) {
        this.recall.error = String(e);
      }
      this.recall.idx += 1;
      this.recall.graded += 1;
      fetch("/api/study/stats").then((r) => r.json()).then((s) => { this.recall.stats = s; }).catch(() => {});
      if (this.recall.idx >= this.recall.queue.length) {
        try {
          const q = await fetch("/api/study/queue?limit=30").then((r) => r.json());
          this.recall.queue = (q && q.queue) || [];
          this.recall.idx = 0;
        } catch (e) { /* keep current */ }
      }
    },
    readinessColor(score) {
      if (score == null) return "var(--ink-4)";
      if (score >= 75) return "var(--c-emerald)";
      if (score >= 50) return "var(--c-amber)";
      return "var(--c-coral)";
    },
    async askTutor() {
      const q = (this.recall.tutorQ || "").trim();
      if (!q || this.recall.tutorLoading) return;
      this.recall.tutorLoading = true;
      this.recall.tutorAnswer = "";
      try {
        const r = await fetch("/api/ai/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, subject: this.recall.tutorSubject || "" }),
        });
        const j = await r.json();
        if (!r.ok) {
          this.recall.tutorAnswer = "<p class='muted'>" + this.escapeHtml(j.detail || ("Error " + r.status)) + "</p>";
        } else {
          this.recall.tutorAnswer = this.renderMarkdown(j.answer || "");
        }
      } catch (e) {
        this.recall.tutorAnswer = "<p class='muted'>" + this.escapeHtml(String(e)) + "</p>";
      } finally {
        this.recall.tutorLoading = false;
      }
    },

    // ============================================================
    // VOICE INPUT — local speech-to-text via whisper.cpp (/api/voice)
    // Reusable across any text field: pass the x-model path, e.g.
    //   @click="toggleVoice('recall.tutorQ')"
    // Records with MediaRecorder, transcribes locally, appends the text.
    // ============================================================
    async toggleVoice(targetPath) {
      if (this.voice.recording) { this._voiceStop(); return; }
      if (this.voice.busy) return;
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        alert("Voice input isn't supported in this browser.");
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mr = new MediaRecorder(stream);
        this.voice.chunks = [];
        mr.ondataavailable = (e) => { if (e.data && e.data.size) this.voice.chunks.push(e.data); };
        mr.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop());
          const blob = new Blob(this.voice.chunks, { type: mr.mimeType || "audio/webm" });
          this.voice.chunks = [];
          await this._voiceTranscribe(blob, targetPath);
        };
        this.voice.mediaRecorder = mr;
        this.voice.target = targetPath;
        this.voice.recording = true;
        mr.start();
      } catch (e) {
        this.voice.recording = false;
        alert("Microphone unavailable or permission denied.");
      }
    },
    _voiceStop() {
      const mr = this.voice.mediaRecorder;
      this.voice.recording = false;
      if (mr && mr.state !== "inactive") mr.stop();
    },
    async _voiceTranscribe(blob, targetPath) {
      this.voice.busy = true;
      try {
        const ext = (blob.type || "").includes("ogg") ? "ogg" : "webm";
        const fd = new FormData();
        fd.append("audio", blob, "clip." + ext);
        const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.text) {
          this._voiceSetField(targetPath, j.text);
        } else if (!r.ok) {
          alert(j.detail || ("Transcription failed (" + r.status + ")"));
        }
        // r.ok with empty text == heard nothing; stay silent.
      } catch (e) {
        alert("Transcription error: " + e);
      } finally {
        this.voice.busy = false;
        this.voice.target = null;
      }
    },
    _voiceSetField(path, text) {
      if (!text) return;
      const parts = path.split(".");
      let obj = this;
      for (let i = 0; i < parts.length - 1; i++) obj = obj[parts[i]];
      const key = parts[parts.length - 1];
      const cur = (obj[key] || "").trim();
      obj[key] = cur ? cur + " " + text : text;
    },

    // ============================================================
    // (a) SCREEN WAKE LOCK — keeps the display awake during a focus session.
    // Gracefully no-ops where the Wake Lock API is unsupported.
    // ============================================================
    async requestWakeLock() {
      if (!("wakeLock" in navigator)) { this.wakeLock.supported = false; return; }
      this.wakeLock.supported = true;
      if (this.wakeLock.sentinel) return;             // already held
      try {
        const sentinel = await navigator.wakeLock.request("screen");
        this.wakeLock.sentinel = sentinel;
        this.wakeLock.active = true;
        sentinel.addEventListener("release", () => {
          this.wakeLock.active = false;
          this.wakeLock.sentinel = null;
        });
      } catch (_) {
        // Permission denied / not allowed (e.g. background tab) — silent.
        this.wakeLock.active = false;
        this.wakeLock.sentinel = null;
      }
    },
    async releaseWakeLock() {
      const s = this.wakeLock.sentinel;
      this.wakeLock.sentinel = null;
      this.wakeLock.active = false;
      if (s) { try { await s.release(); } catch (_) {} }
    },

    // ============================================================
    // (g) NOTIFICATIONS — request on a user gesture, fire on Pomodoro end and
    // on agent/chat completion. No SSE stream exists; chat completion is
    // detected by the existing poll (see _maybeNotifyChatDone).
    // ============================================================
    async toggleNotifications() {
      if (typeof Notification === "undefined") {
        this.notify.permission = "unsupported";
        return;
      }
      if (this.notify.enabled) {
        // User is switching the preference off; permission can't be revoked
        // programmatically, so we just stop firing.
        this.notify.enabled = false;
        try { localStorage.setItem("mc.notify", "0"); } catch (_) {}
        return;
      }
      let perm = Notification.permission;
      if (perm === "default") {
        try { perm = await Notification.requestPermission(); } catch (_) { perm = Notification.permission; }
      }
      this.notify.permission = perm;
      this.notify.enabled = perm === "granted";
      try { localStorage.setItem("mc.notify", this.notify.enabled ? "1" : "0"); } catch (_) {}
    },
    fireNotification(title, opts) {
      try {
        if (!this.notify.enabled || typeof Notification === "undefined") return;
        if (Notification.permission !== "granted") return;
        const n = new Notification(title, {
          icon: "/static/icon-192.png",
          badge: "/static/favicon.png",
          ...(opts || {}),
        });
        n.onclick = () => { try { window.focus(); n.close(); } catch (_) {} };
      } catch (_) {}
    },

    // ============================================================
    // (f) READ-ALOUD — POST {text} to /api/voice/speak, play the returned WAV.
    // `key` identifies the button so its busy spinner can be shown.
    // ============================================================
    async readAloud(text, key) {
      const clean = (text || "").toString().trim();
      if (!clean) return;
      // Toggle: a second click on the playing item stops it.
      if (this.speak.busy && this.speak.key === key) { this.stopReadAloud(); return; }
      this.stopReadAloud();
      this.speak.busy = true;
      this.speak.key = key;
      try {
        const r = await fetch("/api/voice/speak", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: clean.slice(0, 6000) }),
        });
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.detail || ("TTS failed (" + r.status + ")"));
        }
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        this.speak.audio = audio;
        audio.onended = () => { this.stopReadAloud(); };
        audio.onerror = () => { this.stopReadAloud(); };
        await audio.play();
        // play() resolved → keep busy until ended; the spinner reflects key.
      } catch (e) {
        this.stopReadAloud();
        alert("Read-aloud error: " + e);
      }
    },
    // Strip HTML to plain text so we read the rendered note/answer, not tags.
    readAloudHtml(html, key) {
      const tmp = document.createElement("div");
      tmp.innerHTML = html || "";
      this.readAloud(tmp.textContent || tmp.innerText || "", key);
    },
    stopReadAloud() {
      const a = this.speak.audio;
      this.speak.audio = null;
      this.speak.busy = false;
      this.speak.key = null;
      if (a) {
        try { a.pause(); } catch (_) {}
        if (a.src && a.src.startsWith("blob:")) { try { URL.revokeObjectURL(a.src); } catch (_) {} }
      }
    },

    // ============================================================
    // (b) STATS TAB — Observable Plot charts from the stats endpoints.
    // Endpoints (degrade gracefully if absent / empty):
    //   GET /api/stats/quiz         -> { rows:[{date,subject,topic,score,target}] }
    //   GET /api/stats/productivity -> { rows:[{hour,minutes,date}] }
    //   GET /api/stats/summary      -> { subjects:[{subject,score,target}] }
    // The exact field names are read defensively (multiple aliases) so the
    // charts survive minor backend shape differences.
    // ============================================================
    async loadStatsTab() {
      if (this.statsTab.loading) return;
      this.statsTab.loading = true;
      this.statsTab.error = "";
      const get = async (path) => {
        try {
          const r = await fetch(path);
          if (!r.ok) return null;
          return await r.json();
        } catch (_) { return null; }
      };
      const [quiz, productivity, summary] = await Promise.all([
        get("/api/stats/quiz"),
        get("/api/stats/productivity"),
        get("/api/stats/summary"),
      ]);
      this.statsTab.quiz = quiz;
      this.statsTab.productivity = productivity;
      this.statsTab.summary = summary;
      this.statsTab.loaded = true;
      this.statsTab.loading = false;
      this.$nextTick(() => this.renderStatsCharts());
    },
    _statsRows(payload, keys) {
      // Pull an array of rows from a payload that might be {rows:[...]},
      // {data:[...]}, a bare array, or keyed by one of `keys`.
      if (!payload) return [];
      if (Array.isArray(payload)) return payload;
      for (const k of keys) if (Array.isArray(payload[k])) return payload[k];
      for (const k of ["rows", "data", "items", "points"]) if (Array.isArray(payload[k])) return payload[k];
      return [];
    },
    _num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; },
    _placeholder(elId, msg) {
      const el = document.getElementById(elId);
      if (!el) return;
      el.innerHTML = '<div class="stats-empty">' + this.escapeHtml(msg) + "</div>";
    },
    renderStatsCharts() {
      if (typeof Plot === "undefined") {
        ["statsDot", "statsBox", "statsDensity", "statsThreshold"].forEach((id) =>
          this._placeholder(id, "Charts library unavailable."));
        return;
      }
      this.renderStatsDot();
      this.renderStatsBox();
      this.renderStatsDensity();
      this.renderStatsThreshold();
    },
    _mountPlot(elId, fig) {
      const el = document.getElementById(elId);
      if (!el) return;
      el.innerHTML = "";
      el.append(fig);
    },
    _plotAccent() {
      // Read live theme tokens so charts follow Terminal/Void.
      const cs = getComputedStyle(document.documentElement);
      return {
        accent: (cs.getPropertyValue("--accent") || "#2dd4bf").trim(),
        copper: (cs.getPropertyValue("--copper") || "#e0915a").trim(),
        ink:    (cs.getPropertyValue("--ink-2") || "#cbd5e1").trim(),
        line:   "rgba(" + (cs.getPropertyValue("--accent-rgb") || "45,212,191").trim() + ",0.16)",
      };
    },
    renderStatsDot() {
      const id = "statsDot";
      const rows = this._statsRows(this.statsTab.quiz, ["quiz", "attempts"]).map((r) => ({
        date: new Date(r.date || r.created_at || r.day || r.t),
        topic: r.topic || r.subject || r.deck || "—",
        subject: r.subject || r.topic || "—",
        score: this._num(r.score ?? r.accuracy ?? r.percent ?? r.pct),
      })).filter((r) => r.score != null && !isNaN(+r.date));
      if (!rows.length) { this._placeholder(id, "No quiz scores yet. Take a quiz to see score-per-topic over time."); return; }
      const c = this._plotAccent();
      const fig = Plot.plot({
        marginLeft: 54, marginBottom: 38, height: 300,
        style: { background: "transparent", color: c.ink, fontFamily: "Inter, system-ui, sans-serif" },
        x: { type: "utc", label: "Date", grid: true },
        y: { label: "Score %", domain: [0, 100], grid: true },
        color: { legend: true, scheme: "tableau10" },
        marks: [
          Plot.ruleY([0]),
          Plot.dot(rows, { x: "date", y: "score", stroke: "topic", fill: "topic", r: 4, fillOpacity: 0.85, tip: true,
            title: (d) => `${d.topic}\n${d.score}%\n${d.date.toLocaleDateString()}` }),
          Plot.linearRegressionY(rows, { x: "date", y: "score", stroke: c.accent, strokeWidth: 1.2, strokeOpacity: 0.5 }),
        ],
      });
      this._mountPlot(id, fig);
    },
    renderStatsBox() {
      const id = "statsBox";
      const rows = this._statsRows(this.statsTab.quiz, ["quiz", "attempts"]).map((r) => ({
        subject: r.subject || r.topic || "—",
        score: this._num(r.score ?? r.accuracy ?? r.percent ?? r.pct),
      })).filter((r) => r.score != null);
      if (!rows.length) { this._placeholder(id, "No score distribution yet. Quiz scores populate this box plot per subject."); return; }
      const c = this._plotAccent();
      const fig = Plot.plot({
        marginLeft: 70, marginBottom: 38, height: 300,
        style: { background: "transparent", color: c.ink, fontFamily: "Inter, system-ui, sans-serif" },
        x: { label: "Score %", domain: [0, 100], grid: true },
        y: { label: null },
        marks: [
          Plot.boxX(rows, { x: "score", y: "subject", fill: c.accent, fillOpacity: 0.28, stroke: c.accent, tip: true }),
        ],
      });
      this._mountPlot(id, fig);
    },
    renderStatsDensity() {
      const id = "statsDensity";
      const rows = this._statsRows(this.statsTab.productivity, ["productivity", "sessions"]).map((r) => {
        let hour = this._num(r.hour ?? r.hour_of_day);
        if (hour == null && (r.time || r.timestamp || r.created_at)) {
          const d = new Date(r.time || r.timestamp || r.created_at);
          if (!isNaN(+d)) hour = d.getHours() + d.getMinutes() / 60;
        }
        return { hour, weight: this._num(r.minutes ?? r.count ?? r.value) || 1 };
      }).filter((r) => r.hour != null);
      if (!rows.length) { this._placeholder(id, "No study sessions logged yet. Time-of-day density shows when you study."); return; }
      const c = this._plotAccent();
      const fig = Plot.plot({
        marginLeft: 48, marginBottom: 38, height: 300,
        style: { background: "transparent", color: c.ink, fontFamily: "Inter, system-ui, sans-serif" },
        x: { label: "Hour of day", domain: [0, 24], ticks: [0, 4, 8, 12, 16, 20, 24], grid: true },
        y: { label: "Study density", grid: true },
        marks: [
          Plot.areaY(rows, Plot.binX({ y: "sum" }, { x: "hour", y: "weight", fill: c.accent, fillOpacity: 0.22, curve: "natural", thresholds: 24 })),
          Plot.lineY(rows, Plot.binX({ y: "sum" }, { x: "hour", y: "weight", stroke: c.accent, strokeWidth: 1.6, curve: "natural", thresholds: 24, tip: true })),
          Plot.ruleY([0]),
        ],
      });
      this._mountPlot(id, fig);
    },
    renderStatsThreshold() {
      const id = "statsThreshold";
      // Prefer per-date series from quiz; otherwise per-subject from summary.
      let rows = this._statsRows(this.statsTab.quiz, ["quiz", "attempts"]).map((r) => ({
        date: new Date(r.date || r.created_at || r.day || r.t),
        score: this._num(r.score ?? r.accuracy ?? r.percent ?? r.pct),
        target: this._num(r.target ?? r.goal) ?? 75,
      })).filter((r) => r.score != null && !isNaN(+r.date)).sort((a, b) => a.date - b.date);
      const c = this._plotAccent();
      if (rows.length) {
        const fig = Plot.plot({
          marginLeft: 54, marginBottom: 38, height: 300,
          style: { background: "transparent", color: c.ink, fontFamily: "Inter, system-ui, sans-serif" },
          x: { type: "utc", label: "Date", grid: true },
          y: { label: "Score %", domain: [0, 100], grid: true },
          marks: [
            // Threshold area: green above target, copper below.
            Plot.areaY(rows, { x: "date", y1: (d) => Math.max(d.score, d.target), y2: "target", fill: c.accent, fillOpacity: 0.18, curve: "step" }),
            Plot.areaY(rows, { x: "date", y1: "target", y2: (d) => Math.min(d.score, d.target), fill: c.copper, fillOpacity: 0.18, curve: "step" }),
            Plot.lineY(rows, { x: "date", y: "score", stroke: c.accent, strokeWidth: 1.8, curve: "step", tip: true }),
            Plot.ruleY(rows, { y: "target", stroke: c.copper, strokeDasharray: "4 4", strokeOpacity: 0.8 }),
          ],
        });
        this._mountPlot(id, fig);
        return;
      }
      // Summary fallback: bars of score vs target threshold per subject.
      const subs = this._statsRows(this.statsTab.summary, ["subjects", "summary"]).map((r) => ({
        subject: r.subject || r.name || "—",
        score: this._num(r.score ?? r.readiness ?? r.accuracy) ?? 0,
        target: this._num(r.target ?? r.goal) ?? 75,
      })).filter((r) => r.subject);
      if (!subs.length) { this._placeholder(id, "No score-vs-target data yet."); return; }
      const fig = Plot.plot({
        marginLeft: 80, marginBottom: 38, height: 300,
        style: { background: "transparent", color: c.ink, fontFamily: "Inter, system-ui, sans-serif" },
        x: { label: "Score %", domain: [0, 100], grid: true },
        y: { label: null },
        marks: [
          Plot.barX(subs, { x: "score", y: "subject", fill: (d) => d.score >= d.target ? c.accent : c.copper, fillOpacity: 0.5, tip: true }),
          Plot.tickX(subs, { x: "target", y: "subject", stroke: c.copper, strokeWidth: 2 }),
        ],
      });
      this._mountPlot(id, fig);
    },

    // ============================================================
    // (c) LIBRARY — Mermaid diagrams + Markmap mind-map of the current note.
    // ============================================================
    renderMermaidIn(containerId) {
      try {
        if (typeof mermaid === "undefined") return;
        const root = document.getElementById(containerId);
        if (!root) return;
        // marked renders fenced ```mermaid as <pre><code class="language-mermaid">.
        const blocks = root.querySelectorAll("code.language-mermaid, code.lang-mermaid");
        const nodes = [];
        blocks.forEach((code) => {
          const pre = code.closest("pre") || code;
          const div = document.createElement("div");
          div.className = "mermaid";
          div.textContent = code.textContent || "";
          pre.replaceWith(div);
          nodes.push(div);
        });
        if (nodes.length) {
          mermaid.run({ nodes }).catch(() => {});
        }
      } catch (_) {}
    },
    toggleMindmap() {
      this.mindmap.open = !this.mindmap.open;
      if (this.mindmap.open) {
        this.$nextTick(() => this.renderMindmap());
      }
    },
    async renderMindmap() {
      const host = document.getElementById("mindmapSvg");
      const md = (this.notes.content && this.notes.content.content) || "";
      if (!host) return;
      if (!md.trim()) { host.parentElement && (host.parentElement.dataset.empty = "1"); return; }
      try {
        // markmap globals attach to window.markmap (vendored lib + view).
        const mk = window.markmap;
        if (!mk || !mk.Transformer || !mk.Markmap) {
          host.outerHTML = '<div class="stats-empty" id="mindmapSvg">Mind-map library unavailable.</div>';
          return;
        }
        const transformer = new mk.Transformer();
        const { root } = transformer.transform(md);
        // Recreate a clean SVG each time to avoid stale Markmap instances.
        const fresh = host.cloneNode(false);
        host.replaceWith(fresh);
        const mm = mk.Markmap.create(fresh, { autoFit: true, embedGlobalCSS: true });
        mm.setData(root);
        mm.fit();
        this.mindmap.rendered = true;
      } catch (e) {
        host.innerHTML = "";
        this.mindmap.rendered = false;
      }
    },

    // ============================================================
    // (d) KNOWLEDGE GRAPH — Cytoscape over /api/graph/{agents,concepts}.
    // ============================================================
    setGraphMode(mode) {
      if (this.graph.mode === mode) return;
      this.graph.mode = mode;
      this.graph.selected = null;
      this.loadGraph();
    },
    _graphPalette(group) {
      // Map node groups to the chip palette (accent / copper / teal family).
      const cs = getComputedStyle(document.documentElement);
      const accent = (cs.getPropertyValue("--accent") || "#2dd4bf").trim();
      const copper = (cs.getPropertyValue("--copper") || "#e0915a").trim();
      const teal = (cs.getPropertyValue("--teal") || "#2dd4bf").trim();
      const map = {
        agent: accent, model: copper,
        function: accent, method: "#60a5fa", class: copper, interface: "#a78bfa",
        type: teal, route: "#f472b6", constant: "#fbbf24", variable: "#94a3b8",
      };
      return map[group] || teal;
    },
    // Friendly per-mode empty-state copy. Concepts == study knowledge.
    graphEmptyMessage() {
      if (this.graph.error) return this.graph.error;
      if (this.graph.mode === "concepts") return "No notes yet — add markdown to ~/subjects, then Reindex knowledge.";
      if (this.graph.mode === "code") return "No code graph yet — the dashboard architecture index is empty.";
      return "No graph data for this view yet.";
    },
    async loadGraph() {
      if (typeof cytoscape === "undefined") { this.graph.error = "Graph library unavailable."; return; }
      this.graph.loading = true;
      this.graph.error = "";
      this.graph.selected = null;
      let data = { nodes: [], edges: [] };
      try {
        const r = await fetch("/api/graph/" + this.graph.mode);
        if (r.ok) {
          data = await r.json();
        } else if (r.status === 404) {
          // Endpoint not wired yet (e.g. /api/graph/code) — treat as empty, not an error.
          this.graph.error = "";
        } else {
          this.graph.error = "Graph unavailable (HTTP " + r.status + ").";
        }
      } catch (e) { this.graph.error = String(e); }
      const nodes = (data.nodes || []);
      const edges = (data.edges || []);
      this.graph.counts = { nodes: nodes.length, edges: edges.length };
      const host = document.getElementById("graphCanvas");
      if (!host) { this.graph.loading = false; return; }
      if (this.graph.cy) { try { this.graph.cy.destroy(); } catch (_) {} this.graph.cy = null; }
      if (!nodes.length) { this.graph.loading = false; return; }

      const counts = nodes.map((n) => n.count || 1);
      const maxC = Math.max(1, ...counts);
      const els = [];
      nodes.forEach((n) => els.push({ data: {
        id: String(n.id), label: n.label || String(n.id), group: n.group || "node",
        count: n.count || 0, color: this._graphPalette(n.group),
        size: 18 + 34 * Math.sqrt((n.count || 1) / maxC),
        meta: n,
      }}));
      const ids = new Set(nodes.map((n) => String(n.id)));
      edges.forEach((e, i) => {
        const s = String(e.source), t = String(e.target);
        if (ids.has(s) && ids.has(t)) els.push({ data: { id: "e" + i, source: s, target: t, weight: e.weight || 1, kind: e.kind || "" } });
      });

      const cy = cytoscape({
        container: host,
        elements: els,
        wheelSensitivity: 0.25,
        style: [
          { selector: "node", style: {
            "background-color": "data(color)", "label": "data(label)",
            "width": "data(size)", "height": "data(size)",
            "font-size": "9px", "color": "rgba(226,232,240,0.85)",
            "text-valign": "bottom", "text-margin-y": 3, "text-max-width": "90px",
            "text-wrap": "ellipsis", "border-width": 1.5, "border-color": "data(color)",
            "border-opacity": 0.5, "background-opacity": 0.85,
          }},
          { selector: "node:selected", style: { "border-width": 3, "border-opacity": 1, "border-color": "#ffffff" } },
          { selector: "edge", style: {
            "width": "mapData(weight, 1, 20, 0.6, 3.2)", "line-color": "rgba(148,163,184,0.30)",
            "curve-style": "bezier", "target-arrow-shape": "triangle",
            "target-arrow-color": "rgba(148,163,184,0.30)", "arrow-scale": 0.7,
          }},
          { selector: "edge:selected", style: { "line-color": "rgba(255,255,255,0.7)" } },
        ],
        layout: { name: "cose", animate: false, idealEdgeLength: 90, nodeRepulsion: 9000, padding: 24 },
      });
      cy.on("tap", "node", (evt) => {
        const d = evt.target.data();
        this.graph.selected = { id: d.id, label: d.label, group: d.group, count: d.count, meta: d.meta || {} };
      });
      cy.on("tap", (evt) => { if (evt.target === cy) this.graph.selected = null; });
      this.graph.cy = cy;
      this.graph.loading = false;
    },

    // ============================================================
    // TRANSIENT TOASTS — used by reindex + converse for non-blocking feedback.
    // ============================================================
    toast(text, kind, ms) {
      const id = ++this._toastSeq;
      this.toasts.push({ id, kind: kind || "info", text: String(text || "") });
      const ttl = ms || (kind === "bad" ? 6000 : 3600);
      setTimeout(() => this.dismissToast(id), ttl);
      return id;
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter((t) => t.id !== id);
    },

    // ============================================================
    // (1) SYSTEM HEALTH — GET /api/system/health (board) + /summary (badge).
    // Defensive everywhere: a 404 means the backend aggregator isn't wired yet,
    // so we show a calm "not available" state rather than an error. Polled every
    // ~15s only while the System tab is visible.
    // ============================================================
    async loadSystemSummary() {
      try {
        const r = await fetch("/api/system/health/summary");
        if (r.status === 404) { this.system.unavailable = true; this.system.summary = null; return; }
        if (!r.ok) return;
        this.system.summary = await r.json();
        this.system.unavailable = false;
      } catch (e) { /* silent — badge just stays hidden */ }
    },
    async loadSystemHealth() {
      this.system.loading = true;
      this.system.error = "";
      try {
        const r = await fetch("/api/system/health");
        if (r.status === 404) {
          this.system.unavailable = true;
          this.system.health = null;
        } else if (r.ok) {
          this.system.unavailable = false;
          this.system.health = await r.json();
          // keep the badge in sync from the full payload when we have it
          this.loadSystemSummary();
        } else {
          this.system.error = "Health check failed (HTTP " + r.status + ").";
        }
      } catch (e) {
        this.system.error = String(e);
      } finally {
        this.system.loading = false;
      }
    },
    startSystemPoll() {
      this.loadSystemHealth();
      this.loadOrchestrator();   // self-knowledge + gaps (cached; load once per open)
      this.stopSystemPoll();
      this.system.pollTimer = setInterval(() => {
        // Only poll while the tab is actually visible to the user.
        if (this.activeTab === "system" && !document.hidden) this.loadSystemHealth();
      }, this.system.pollMs);
    },
    async loadOrchestrator() {
      this.orch.loading = true;
      this.orch.error = "";
      try {
        const [s, g] = await Promise.all([
          fetch("/api/orchestrator/status").then(r => r.ok ? r.json() : null),
          fetch("/api/orchestrator/gaps").then(r => r.ok ? r.json() : null),
        ]);
        if (s && !s.error) this.orch.status = s;
        else this.orch.error = (s && s.error) || "orchestrator status unavailable";
        this.orch.gaps = (g && g.gaps) || [];
      } catch (e) {
        this.orch.error = String(e);
      } finally {
        this.orch.loading = false;
      }
    },
    stopSystemPoll() {
      if (this.system.pollTimer) { clearInterval(this.system.pollTimer); this.system.pollTimer = null; }
    },
    // ---- health view helpers (tolerant of several backend field spellings) ----
    systemServices() {
      const h = this.system.health;
      if (!h) return [];
      // Accept either {services:[...]} or {services:{...}} map; merge listeners.
      let out = [];
      const norm = (s, fallbackKind) => {
        if (!s) return null;
        const status = (s.status || s.state || s.active || (s.up === true ? "up" : s.up === false ? "down" : "")).toString().toLowerCase();
        return {
          name: s.label || s.name || s.id || s.unit || "service",
          status: status || "unknown",
          detail: s.detail || s.message || s.note || "",
          port: s.port != null ? s.port : null,
          kind: s.kind || fallbackKind || "service",
        };
      };
      const collect = (val, kind) => {
        if (!val) return;
        if (Array.isArray(val)) { val.forEach((s) => { const n = norm(s, kind); if (n) out.push(n); }); }
        else if (typeof val === "object") {
          Object.keys(val).forEach((k) => {
            const s = val[k];
            const n = norm(typeof s === "object" ? Object.assign({ name: k }, s) : { name: k, status: s }, kind);
            if (n) out.push(n);
          });
        }
      };
      collect(h.services, "service");
      collect(h.listeners, "listener");
      return out;
    },
    // Map any status string to one of our three pill states.
    systemPillClass(status) {
      const s = (status || "").toString().toLowerCase();
      if (["up", "ok", "online", "healthy", "active", "running", "green", "idle", "exited"].includes(s)) return "ok";
      if (["degraded", "warn", "warning", "amber", "partial", "slow", "starting", "activating", "reloading"].includes(s)) return "warn";
      if (["down", "fail", "failed", "offline", "error", "red", "dead", "inactive"].includes(s)) return "bad";
      return "muted";   // missing / not-found / unknown
    },
    systemDatabases() {
      const h = this.system.health;
      if (!h) return [];
      const dbs = h.databases || h.dbs || h.db_rows || [];
      if (Array.isArray(dbs)) {
        return dbs.map((d) => ({
          name: d.label || d.name || d.db || "db",
          rows: (d.rows != null ? d.rows : (d.row_count != null ? d.row_count : (d.count != null ? d.count : null))),
          exists: d.exists !== false,
        }));
      }
      if (typeof dbs === "object") {
        return Object.keys(dbs).map((k) => {
          const v = dbs[k];
          const rows = (v && typeof v === "object") ? (v.rows != null ? v.rows : v.count) : v;
          return { name: k, rows: (rows == null ? null : rows), exists: !(v && v.exists === false) };
        });
      }
      return [];
    },
    // Pull a {used,total,pct} bar from either bytes or a precomputed percentage.
    systemGauge(which) {
      const h = this.system.health;
      if (!h) return null;
      const g = h[which] || (h.resources && h.resources[which]) || null;
      if (!g) return null;
      let pct = null;
      if (g.pct != null) pct = Number(g.pct);
      else if (g.percent != null) pct = Number(g.percent);
      else if (g.used != null && g.total) pct = (Number(g.used) / Number(g.total)) * 100;
      if (pct == null || isNaN(pct)) return null;
      pct = Math.max(0, Math.min(100, Math.round(pct)));
      return {
        pct,
        used: g.used != null ? g.used : null,
        total: g.total != null ? g.total : null,
        label: g.label || (g.used != null && g.total != null ? this.formatBytes(g.used) + " / " + this.formatBytes(g.total) : pct + "%"),
        cls: pct >= 90 ? "bad" : (pct >= 75 ? "warn" : "ok"),
      };
    },
    systemLastBackup() {
      const h = this.system.health;
      if (!h) return "—";
      const b = h.last_backup || h.lastBackup || h.backup;
      if (!b) return "—";
      if (typeof b === "string") return this.formatRelative(b) || b;
      const at = b.at || b.time || b.timestamp || b.created_at;
      if (b.relative) return b.relative;
      if (at) return this.formatRelative(at) || at;
      return "—";
    },
    systemBadge() {
      const s = this.system.summary;
      if (!s) return null;
      const up = (s.up != null ? s.up : (s.healthy != null ? s.healthy : s.online));
      const total = (s.total != null ? s.total : (s.count != null ? s.count : s.services));
      if (up == null || total == null) {
        // fall back to deriving from the full board if present
        const svc = this.systemServices();
        if (!svc.length) return null;
        const u = svc.filter((x) => this.systemPillClass(x.status) === "ok").length;
        return { up: u, total: svc.length, cls: u === svc.length ? "ok" : (u === 0 ? "bad" : "warn") };
      }
      const cls = up >= total ? "ok" : (up <= 0 ? "bad" : "warn");
      return { up, total, cls };
    },

    // ============================================================
    // (4) REINDEX KNOWLEDGE — POST /api/knowledge/reindex, toast {notes,concepts,edges}.
    // ============================================================
    async reindexKnowledge() {
      if (this.reindex.busy) return;
      this.reindex.busy = true;
      this.reindex.error = "";
      try {
        const r = await fetch("/api/knowledge/reindex", { method: "POST" });
        const j = await r.json().catch(() => ({}));
        if (r.status === 404) {
          this.reindex.error = "Reindex endpoint not wired yet.";
          this.toast("Reindex not available yet (backend not wired).", "bad");
          return;
        }
        if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
        this.reindex.last = j;
        const n = j.notes != null ? j.notes : "—";
        const c = j.concepts != null ? j.concepts : "—";
        const e = j.edges != null ? j.edges : "—";
        this.toast("Reindexed: " + n + " notes · " + c + " concepts · " + e + " edges", "ok");
        // If the concept graph is open, refresh it so new nodes light up.
        if (this.activeTab === "graph" && (this.graph.mode === "concepts" || this.graph.mode === "code")) {
          this.$nextTick(() => this.loadGraph());
        }
      } catch (err) {
        this.reindex.error = String(err);
        this.toast("Reindex failed: " + err, "bad");
      } finally {
        this.reindex.busy = false;
      }
    },

    // ============================================================
    // (3) VOICE CONVERSE LOOP — record → transcribe → auto-send to chat agent →
    // speak the reply. Reuses /api/voice/transcribe, sendChatMessage, the chat
    // poll, and /api/voice/speak. Independent of the plain mic + speaker buttons.
    //
    //   phase: idle → recording → thinking → speaking → idle
    //
    // Toggling the button while recording stops+sends; while speaking stops the
    // audio; otherwise it disarms the loop entirely.
    // ============================================================
    async toggleConverse() {
      if (this.converse.phase === "recording") { this._converseStopRecording(); return; }
      if (this.converse.phase === "thinking") { return; }            // wait for reply
      if (this.converse.phase === "speaking") { this.stopConverse(); return; }
      // idle → start a new turn
      await this._converseStartRecording();
    },
    async _converseStartRecording() {
      if (!navigator.mediaDevices || !window.MediaRecorder) {
        this.toast("Voice input isn't supported in this browser.", "bad");
        return;
      }
      // Don't fight the plain dictation mic.
      if (this.voice.recording || this.voice.busy) { this.toast("Finish the other recording first.", "info"); return; }
      this.converse.error = "";
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mr = new MediaRecorder(stream);
        this.converse.stream = stream;
        this.converse.chunks = [];
        mr.ondataavailable = (e) => { if (e.data && e.data.size) this.converse.chunks.push(e.data); };
        mr.onstop = async () => {
          try { stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
          this.converse.stream = null;
          const blob = new Blob(this.converse.chunks, { type: mr.mimeType || "audio/webm" });
          this.converse.chunks = [];
          await this._converseTranscribeAndSend(blob);
        };
        this.converse.mediaRecorder = mr;
        this.converse.on = true;
        this.converse.phase = "recording";
        mr.start();
      } catch (e) {
        this.converse.phase = "idle";
        this.toast("Microphone unavailable or permission denied.", "bad");
      }
    },
    _converseStopRecording() {
      const mr = this.converse.mediaRecorder;
      if (mr && mr.state !== "inactive") { mr.stop(); }    // → onstop fires
    },
    async _converseTranscribeAndSend(blob) {
      this.converse.phase = "thinking";
      try {
        const ext = (blob.type || "").includes("ogg") ? "ogg" : "webm";
        const fd = new FormData();
        fd.append("audio", blob, "clip." + ext);
        const r = await fetch("/api/voice/transcribe", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || ("Transcription failed (" + r.status + ")"));
        const text = (j.text || "").trim();
        if (!text) {
          // Heard nothing — quietly return to idle so the user can retry.
          this.converse.phase = "idle";
          this.toast("Didn't catch that — try again.", "info");
          return;
        }
        // Put the transcript in the chat composer (visible) and auto-send it.
        this.chat.input = text;
        // Note which agent message ids already exist so we can spot the new reply.
        this._converseBaselineLen = this.chat.history.length;
        await this.sendChatMessage();              // pushes user msg, starts the poll
        // Watch the chat poll for the agent's reply, then speak it.
        this._converseWatchForReply();
      } catch (e) {
        this.converse.phase = "idle";
        this.converse.error = String(e);
        this.toast("Converse error: " + e, "bad");
      }
    },
    _converseWatchForReply() {
      // The existing chat poll updates chat.history + chat.isRunning. We watch
      // for: not running AND a fresh agent message after our baseline.
      if (this.converse.watching) return;
      this.converse.watching = true;
      const started = Date.now();
      const tick = () => {
        if (!this.converse.on || this.converse.phase === "idle") { this.converse.watching = false; return; }
        const h = this.chat.history || [];
        const last = h[h.length - 1];
        const haveReply = last && last.role !== "user" && h.length > (this._converseBaselineLen || 0);
        if (!this.chat.isRunning && haveReply) {
          this.converse.watching = false;
          this._converseSpeak(last);
          return;
        }
        // 90s safety timeout so we never spin forever.
        if (Date.now() - started > 90000) {
          this.converse.watching = false;
          this.converse.phase = "idle";
          this.toast("No reply to speak (timed out).", "info");
          return;
        }
        setTimeout(tick, 800);
      };
      setTimeout(tick, 800);
    },
    async _converseSpeak(msg) {
      const text = (msg && (msg.text || msg.content) || "").toString().trim();
      if (!text) { this.converse.phase = "idle"; return; }
      this.converse.lastSpokenId = msg.id || null;
      this.converse.phase = "speaking";
      try {
        const r = await fetch("/api/voice/speak", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text.slice(0, 6000) }),
        });
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.detail || ("TTS failed (" + r.status + ")"));
        }
        const url = URL.createObjectURL(await r.blob());
        const audio = new Audio(url);
        this.converse.audio = audio;
        const done = () => {
          if (audio.src && audio.src.startsWith("blob:")) { try { URL.revokeObjectURL(audio.src); } catch (_) {} }
          this.converse.audio = null;
          // Loop stays armed — return to idle so the user can speak again.
          if (this.converse.on) this.converse.phase = "idle";
        };
        audio.onended = done;
        audio.onerror = done;
        await audio.play();
      } catch (e) {
        this.converse.phase = "idle";
        this.converse.error = String(e);
        this.toast("Couldn't speak the reply: " + e, "bad");
      }
    },
    // Hard stop: cancels recording/audio and disarms the loop.
    stopConverse() {
      this.converse.on = false;
      this.converse.watching = false;
      const mr = this.converse.mediaRecorder;
      if (mr && mr.state !== "inactive") { try { mr.stop(); } catch (_) {} }
      if (this.converse.stream) { try { this.converse.stream.getTracks().forEach((t) => t.stop()); } catch (_) {} this.converse.stream = null; }
      const a = this.converse.audio;
      this.converse.audio = null;
      if (a) { try { a.pause(); } catch (_) {} if (a.src && a.src.startsWith("blob:")) { try { URL.revokeObjectURL(a.src); } catch (_) {} } }
      this.converse.phase = "idle";
    },
    converseLabel() {
      switch (this.converse.phase) {
        case "recording": return "Listening… tap to send";
        case "thinking":  return "Thinking…";
        case "speaking":  return "Speaking… tap to stop";
        default:          return "Converse";
      }
    },

    // ============================================================
    // (e) TERMINAL — xterm.js over ws://<host>/ws/terminal.
    // ============================================================
    bootTerminal() {
      if (typeof Terminal === "undefined") { this.term.error = "Terminal library unavailable."; return; }
      const host = document.getElementById("terminalHost");
      if (!host) return;
      if (this.term.term) { this.fitTerminal(); return; }   // already booted

      const term = new Terminal({
        cursorBlink: true, fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 13, scrollback: 4000,
        theme: { background: "rgba(0,0,0,0)", foreground: "#cbd5e1", cursor: "#2dd4bf" },
        allowProposedApi: true,
      });
      let fit = null;
      try {
        const FitCtor = (window.FitAddon && window.FitAddon.FitAddon) || window.FitAddon;
        if (FitCtor) { fit = new FitCtor(); term.loadAddon(fit); }
      } catch (_) {}
      term.open(host);
      this.term.term = term;
      this.term.fit = fit;
      this.term.booted = true;
      this.$nextTick(() => this.fitTerminal());
      this.connectTerminalWs();
    },
    connectTerminalWs() {
      const term = this.term.term;
      if (!term) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = proto + "//" + location.host + "/ws/terminal";
      let ws;
      try { ws = new WebSocket(url); } catch (e) { this.term.error = String(e); return; }
      this.term.ws = ws;
      this.term.error = "";
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        this.term.connected = true;
        this.$nextTick(() => this.fitTerminal());   // sends initial resize
      };
      ws.onmessage = (ev) => {
        let data = ev.data;
        if (data instanceof ArrayBuffer) { term.write(new Uint8Array(data)); return; }
        // Server may send framed JSON {type:'output',data} or raw text.
        if (typeof data === "string" && data.startsWith("{")) {
          try { const m = JSON.parse(data); if (m && typeof m.data === "string") { term.write(m.data); return; } } catch (_) {}
        }
        term.write(data);
      };
      ws.onclose = () => { this.term.connected = false; };
      ws.onerror = () => { this.term.error = "WebSocket error — is the terminal backend running?"; };
      if (!this._termDataBound) {
        this._termDataBound = true;
        term.onData((d) => {
          const sock = this.term.ws;
          if (sock && sock.readyState === WebSocket.OPEN) sock.send(d);
        });
      }
    },
    fitTerminal() {
      const term = this.term.term;
      if (!term) return;
      try { if (this.term.fit) this.term.fit.fit(); } catch (_) {}
      const ws = this.term.ws;
      if (ws && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows })); } catch (_) {}
      }
    },
    toggleTerminalFullscreen() {
      this.term.fullscreen = !this.term.fullscreen;
      this.$nextTick(() => this.fitTerminal());
    },
    teardownTerminal() {
      this.term.fullscreen = false;
      const ws = this.term.ws;
      this.term.ws = null;
      this.term.connected = false;
      if (ws) { try { ws.close(); } catch (_) {} }
    },

    // ============================================================
    // (h) PWA — register the service worker.
    // ============================================================
    registerServiceWorker() {
      if (!("serviceWorker" in navigator)) return;
      try {
        // Served from /static so its default scope is /static/ (covers the app
        // shell: app.js, style.css, vendors, fonts, icons).
        navigator.serviceWorker.register("/static/sw.js", { scope: "/static/" }).catch(() => {});
      } catch (_) {}
    },

    // ============================================================
    // STUDY TRACKER (server-backed; roadmap from roadmap_spec.py)
    // ============================================================
    async loadTracker(date) {
      this.tracker.loading = true;
      this.tracker.error = "";
      try {
        const r = await fetch("/api/tracker/state" + (date ? "?date=" + encodeURIComponent(date) : ""));
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.tracker.state = await r.json();
        this.tracker.viewDate = this.tracker.state.isToday ? null : this.tracker.state.viewDate;
        this.loadTrackerMeta();
      } catch (e) {
        this.tracker.error = String(e);
      } finally {
        this.tracker.loading = false;
      }
    },
    // ----- daily date navigation (catch up on missed days) -----
    _trackerBody(obj) {
      // include the viewed date so a tick lands on the day being viewed
      return this.tracker.viewDate ? Object.assign({}, obj, { date: this.tracker.viewDate }) : obj;
    },
    trackerShiftDay(delta) {
      const s = this.tracker.state; if (!s) return;
      const d = new Date(s.viewDate + "T00:00:00"); d.setDate(d.getDate() + delta);
      const iso = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      if (iso < s.planStart || iso > s.today_date) return;
      this.loadTracker(iso === s.today_date ? undefined : iso);
    },
    trackerGoToday() { this.loadTracker(); },
    trackerGoStart() { const s = this.tracker.state; if (s) this.loadTracker(s.planStart); },
    trackerViewLabel() {
      const s = this.tracker.state; if (!s) return "";
      return new Date(s.viewDate + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    },
    // Strategic roadmap layer (exam calendar, alerts, KPIs, phase windows).
    // Cached: fetched once per session — it only changes when the spec changes.
    async loadTrackerMeta() {
      if (this.tracker.meta || this.tracker.metaLoading) return;
      this.tracker.metaLoading = true;
      try {
        const r = await fetch("/api/tracker/meta");
        if (r.ok) this.tracker.meta = await r.json();
      } catch (e) { /* non-fatal — strategic layer is additive */ }
      finally { this.tracker.metaLoading = false; }
    },
    _applyTrackerResult(j) {
      // endpoints return fresh score (+ sometimes today); merge into state
      if (!this.tracker.state) return;
      if (j.score) this.tracker.state.score = j.score;
      if (j.today) this.tracker.state.today = j.today;
      if (j.completed) this.tracker.state.today.studyBlocksCompleted = j.completed;
      if (j.mcqEntries) this.tracker.state.today.mcqEntries = j.mcqEntries;
    },
    async toggleTrackerBlock(block) {
      if (!block || this.tracker.saving) return;
      this.tracker.saving = true;
      try {
        const r = await fetch("/api/tracker/block/toggle", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this._trackerBody({ block_id: block.id })),
        });
        const j = await r.json();
        if (r.ok) {
          block.done = (j.completed || []).includes(block.id);
          this._applyTrackerResult(j);
        }
      } catch (e) { this.tracker.error = String(e); }
      finally { this.tracker.saving = false; }
    },
    async logTrackerMcq() {
      const a = parseInt(this.tracker.mcqAttempted, 10);
      const c = parseInt(this.tracker.mcqCorrect, 10);
      if (!a || a <= 0) return;
      this.tracker.saving = true;
      try {
        const r = await fetch("/api/tracker/mcq", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this._trackerBody({ attempted: a, correct: isNaN(c) ? 0 : c, subject: this.tracker.mcqSubject || "" })),
        });
        const j = await r.json();
        if (r.ok) { this._applyTrackerResult(j); this.tracker.mcqAttempted = ""; this.tracker.mcqCorrect = ""; }
      } catch (e) { this.tracker.error = String(e); }
      finally { this.tracker.saving = false; }
    },
    async logTrackerField(field, value, extra) {
      this.tracker.saving = true;
      try {
        const body = this._trackerBody(Object.assign({ field, value }, extra || {}));
        const r = await fetch("/api/tracker/log", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (r.ok) this._applyTrackerResult(j);
      } catch (e) { this.tracker.error = String(e); }
      finally { this.tracker.saving = false; }
    },
    async setTrackerWater(n) {
      const cur = this.trackerToday() ? this.trackerToday().waterGlasses : 0;
      await this.logTrackerField("water", Math.max(0, cur + n));
    },
    async toggleTrackerHabit(field) {
      const t = this.trackerToday();
      if (!t) return;
      const map = { specs: "specsWorn", sunlight: "sunlightDone", liver: "liverProtocolDone" };
      await this.logTrackerField(field, !t[map[field]]);
    },
    async saveTrackerSleep() {
      if (!this.tracker.sleepTime || !this.tracker.wakeTime) return;
      await this.logTrackerField("sleep", null, { sleepTime: this.tracker.sleepTime, wakeTime: this.tracker.wakeTime });
    },
    async saveTrackerScreen() {
      const p = parseFloat(this.tracker.productive), w = parseFloat(this.tracker.wasted);
      await this.logTrackerField("screen", null, { productive: isNaN(p) ? 0 : p, wasted: isNaN(w) ? 0 : w });
    },
    async commitTracker() {
      try {
        await fetch("/api/tracker/doctor", { method: "POST" });
        await this.loadTracker();
      } catch (e) { this.tracker.error = String(e); }
    },
    trackerToday() { return this.tracker.state ? this.tracker.state.today : null; },
    trackerScorePart(key) {
      const s = this.tracker.state && this.tracker.state.score;
      return s && s.parts ? s.parts[key] : { points: 0, max: 0 };
    },
    trackerScoreRingDash() {
      const s = this.tracker.state && this.tracker.state.score ? this.tracker.state.score.total : 0;
      const c = 2 * Math.PI * 52;
      return { dash: c.toFixed(1), offset: (c * (1 - s / 100)).toFixed(1) };
    },
    trackerScoreColor() {
      const s = this.tracker.state && this.tracker.state.score ? this.tracker.state.score.total : 0;
      if (s >= 80) return "var(--green)";
      if (s >= 50) return "var(--amber)";
      return "var(--rose)";
    },
    async loadTrackerRoadmap(phase) {
      this.tracker.roadmapLoading = true;
      this.loadTrackerMeta();
      try {
        const url = "/api/tracker/roadmap" + (phase ? "?phase=" + encodeURIComponent(phase) : "");
        const r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.tracker.roadmap = await r.json();
        this.tracker.roadmapPhase = this.tracker.roadmap.phase.key;
        // The plan spans the whole year — open it at TODAY, not day 1. No-op
        // when viewing a non-active phase (no .today element exists).
        this.$nextTick(() => setTimeout(() => {
          document.querySelector(".tracker-roadmap .roadmap-day.today")
            ?.scrollIntoView({ block: "center" });
        }, 150));
      } catch (e) { this.tracker.error = String(e); }
      finally { this.tracker.roadmapLoading = false; }
    },
    async loadTrackerStats() {
      this.tracker.statsLoading = true;
      try {
        const r = await fetch("/api/tracker/stats");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.tracker.stats = await r.json();
      } catch (e) { this.tracker.error = String(e); }
      finally { this.tracker.statsLoading = false; }
    },
    trackerHeatColor(score) {
      if (!score) return "var(--bg-3)";
      if (score >= 80) return "var(--green)";
      if (score >= 50) return "var(--amber)";
      if (score >= 25) return "var(--orange)";
      return "var(--rose)";
    },
    _trackerSubjColors: {
      phy: "#fbbf24", chem: "#38bdf8", math: "#c084fc", bio: "#34d399",
      review: "#f472b6", mock: "#fb7185", admin: "#94a3b8",
    },
    trackerSubjectColor(subj) { return this._trackerSubjColors[subj] || "var(--accent)"; },

    // ---------- library helpers ----------
    notesCountTotal() {
      return this.notes.items ? this.notes.items.length : 0;
    },
    notesLastUpdated() {
      if (!this.notes.items || this.notes.items.length === 0) return "";
      // Sort by modified_at descending and return relative time of the first
      const items = [...this.notes.items].sort((a, b) => {
        return new Date(b.modified_at || 0).getTime() - new Date(a.modified_at || 0).getTime();
      });
      return this.formatRelative(items[0].modified_at);
    },

    // ---------- research helpers ----------
    researchInProgress() {
      return (this.researchItems || []).filter((i) => i.status === "researching" || i.status === "queued" || i.status === "running").length;
    },
    researchCompleted() {
      return (this.researchItems || []).filter((i) => i.status === "complete").length;
    },
    researchKeySubmit(ev) {
      if (this.activeTab === "research" && (ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
        this.submitResearch();
        ev.preventDefault();
      }
    },
    globalSearchKey(ev) {
      // Cmd/Ctrl + K toggles the global search palette from anywhere.
      if ((ev.metaKey || ev.ctrlKey) && (ev.key === "k" || ev.key === "K") && !ev.altKey) {
        ev.preventDefault();
        if (this.globalSearchOpen) this.closeGlobalSearch();
        else this.openGlobalSearch();
        return;
      }
      // Windows/Super + Space also toggles the search palette.
      // (On Mac, Cmd+Space is Spotlight — only fire this when the key is literally Space
      // and metaKey is held, and skip if ctrlKey is also held to avoid Ctrl+Space collisions.)
      if (ev.metaKey && !ev.ctrlKey && ev.code === "Space") {
        ev.preventDefault();
        if (this.globalSearchOpen) this.closeGlobalSearch();
        else this.openGlobalSearch();
        return;
      }
      // Escape closes the global search palette if it's open, regardless of focus.
      if (ev.key === "Escape" && this.globalSearchOpen) {
        ev.preventDefault();
        this.closeGlobalSearch();
      }
    },
  };
};

// ============================================================
// Inline SVG icon set (24x24 stroke icons, currentColor)
// ============================================================
function svg(d) {
  return '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
       + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>';
}
function iconHome()     { return svg('<path d="M3 11 12 3l9 8"/><path d="M5 10v10h14V10"/>'); }
function iconBolt()     { return svg('<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>'); }
function iconChat()     { return svg('<path d="M21 12a8 8 0 0 1-11.4 7.2L3 21l1.8-6.6A8 8 0 1 1 21 12z"/>'); }
function iconSun()      { return svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m4.93 19.07 1.41-1.41"/><path d="m17.66 6.34 1.41-1.41"/>'); }
function iconUpload()   { return svg('<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M5 20h14"/>'); }
function iconBook()     { return svg('<path d="M4 4h11a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4V4z"/><path d="M4 16h11"/>'); }
function iconSearch()   { return svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'); }
function iconTarget()   { return svg('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>'); }
function iconRecall()    { return svg('<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v5h-5"/><path d="M12 8v4l3 2"/>'); }
function iconTracker()   { return svg('<path d="M3 3v18h18"/><path d="M7 15l3-3 3 2 5-6"/><circle cx="21" cy="8" r="1.6" fill="currentColor" stroke="none"/>'); }
function iconCalendar() { return svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'); }
function iconLayers()   { return svg('<path d="m12 3 9 5-9 5-9-5 9-5z"/><path d="m3 13 9 5 9-5"/><path d="m3 17 9 5 9-5"/>'); }

// Knowledge-base icons: notebook (open notebook) + obsidian-style
// layered vault. Both follow the same stroke-only convention as the rest.
function iconNotebook()  { return svg('<path d="M4 4h11a4 4 0 0 1 4 4v12H8a4 4 0 0 1-4-4V4z"/><path d="M8 8h7M8 12h7M8 16h5"/><path d="M2 4h2v18H2z" stroke-width="1.5"/>'); }
function iconObsidian()  { return svg('<path d="M12 3 4 7l8 4 8-4-8-4z"/><path d="M4 7v10l8 4 8-4V7"/><path d="M12 11v10"/><path d="M4 7l8 4 8-4"/>'); }
function iconMemory()    { return svg('<path d="M9 3h6a2 2 0 0 1 2 2v1h1a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-1v1a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2v-1H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h1V5a2 2 0 0 1 2-2z"/><path d="M10 9h4M10 13h4" opacity="0.7"/>'); }

// System icons added for the Stats / Knowledge Graph / Terminal tabs.
function iconStats()     { return svg('<path d="M3 3v18h18"/><rect x="7" y="11" width="3" height="6" rx="0.5"/><rect x="12" y="7" width="3" height="10" rx="0.5"/><rect x="17" y="13" width="3" height="4" rx="0.5"/>'); }
function iconGraph()     { return svg('<circle cx="6" cy="6" r="2.4"/><circle cx="18" cy="7" r="2.4"/><circle cx="12" cy="17" r="2.4"/><path d="M7.7 7.6 10.4 15M16.4 8.7 13.3 15.6M8.2 6.5h7.4"/>'); }
function iconTerminal()  { return svg('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3"/><path d="M13 15h4"/>'); }
function iconPulse()     { return svg('<path d="M3 12h4l3 8 4-16 3 8h4"/>'); }
