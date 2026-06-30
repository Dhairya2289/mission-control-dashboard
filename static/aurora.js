/* ============================================================================
   AURORA atmosphere glue — v2.0
   ----------------------------------------------------------------------------
   Independent of Alpine state and the API. Adds the "alive" layer:
     · scroll progress bar
     · parallax orbs (uses the CSS `translate` property so the keyframe drift
       on `transform` is preserved — they compose, not clobber)
     · count-up on the Overview signature numbers ([data-countup])
     · keyboard layer: ? opens the shortcut overlay, single-key tab nav
   Every motion effect is gated behind prefers-reduced-motion.
   ============================================================================ */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* ── Scroll progress + chart-field parallax ──────────────────────────────
     The .aurora-bg orbs are gone. We now drive the scroll progress bar and a
     gentle vertical parallax on the chart-field layers. Each layer also carries
     its own CSS keyframe drift; we write the independent `translate` property so
     the scroll offset COMPOSES with `transform` instead of clobbering it. */
  var progressEl = document.getElementById("scrollProgress");
  var cfGraticule = document.querySelector(".cf-graticule");
  var cfContour = document.querySelector(".cf-contour");
  var ticking = false;

  function onScrollFrame() {
    ticking = false;
    var doc = document.documentElement;
    var max = doc.scrollHeight - window.innerHeight;
    var y = window.scrollY || doc.scrollTop || 0;
    if (progressEl) {
      var pct = max > 0 ? (y / max) * 100 : 0;
      progressEl.style.width = pct.toFixed(2) + "%";
    }
    if (!reduceMotion.matches) {
      // far layer drifts slowest; near layer a touch faster (depth cue)
      if (cfGraticule) cfGraticule.style.translate = "0px " + (y * 0.04).toFixed(1) + "px";
      if (cfContour)   cfContour.style.translate   = "0px " + (y * 0.075).toFixed(1) + "px";
    }
  }
  function requestScroll() {
    if (!ticking) { ticking = true; requestAnimationFrame(onScrollFrame); }
  }
  window.addEventListener("scroll", requestScroll, { passive: true });
  window.addEventListener("resize", requestScroll, { passive: true });
  onScrollFrame();

  /* ── Constellation field — parallax star chart on <canvas#constellation> ──
     Points drift slowly; near points connect with hairlines; the whole field
     parallaxes a few px toward the pointer. Pure transform/opacity feel, but
     drawn on canvas so the proximity links are cheap. Fully skipped under
     reduced-motion (the CSS leaves the canvas blank, which is fine). */
  (function constellation() {
    var canvas = document.getElementById("constellation");
    if (!canvas || reduceMotion.matches) return;
    var ctx = canvas.getContext("2d");
    if (!ctx) return;

    var DPR = Math.min(window.devicePixelRatio || 1, 2);
    var W = 0, H = 0, points = [];
    var pointer = { x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 };
    var LINK_DIST = 132;          // px at which two stars connect
    var running = true;

    function accent() {
      var v = getComputedStyle(document.documentElement).getPropertyValue("--accent-rgb").trim();
      return v || "45,212,191";
    }
    var rgb = accent();
    // recolor exactly once per theme flip (watch data-theme) instead of polling
    // the computed style every frame.
    if ("MutationObserver" in window) {
      new MutationObserver(function () { rgb = accent(); })
        .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    function resize() {
      W = canvas.clientWidth; H = canvas.clientHeight;
      canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      // density scales with area, capped so dense pages stay light
      var target = Math.min(72, Math.round((W * H) / 26000));
      points = [];
      for (var i = 0; i < target; i++) {
        points.push({
          x: rand(i * 7.3) * W,
          y: rand(i * 3.1 + 11) * H,
          // slow deterministic drift vectors (no Math.random at runtime needed,
          // but Math.random is fine here — this file isn't a workflow script)
          vx: (Math.random() - 0.5) * 0.12,
          vy: (Math.random() - 0.5) * 0.12,
          z: 0.4 + Math.random() * 0.6,        // depth → parallax + size
          r: 0.6 + Math.random() * 1.3
        });
      }
    }
    // tiny deterministic pseudo-random for initial placement spread
    function rand(s) { var x = Math.sin(s * 99.13) * 43758.5453; return x - Math.floor(x); }

    function onMove(e) {
      pointer.tx = e.clientX / window.innerWidth;
      pointer.ty = e.clientY / window.innerHeight;
    }
    window.addEventListener("pointermove", onMove, { passive: true });

    function frame() {
      if (!running) return;
      // ease pointer
      pointer.x += (pointer.tx - pointer.x) * 0.05;
      pointer.y += (pointer.ty - pointer.y) * 0.05;
      var px = (pointer.x - 0.5), py = (pointer.y - 0.5);

      ctx.clearRect(0, 0, W, H);
      var i, j, p, q;
      // advance + draw stars
      for (i = 0; i < points.length; i++) {
        p = points[i];
        p.x += p.vx; p.y += p.vy;
        if (p.x < -20) p.x = W + 20; else if (p.x > W + 20) p.x = -20;
        if (p.y < -20) p.y = H + 20; else if (p.y > H + 20) p.y = -20;
        // parallax offset toward pointer, scaled by depth
        p.ox = p.x + px * 26 * p.z;
        p.oy = p.y + py * 26 * p.z;
      }
      // links (proximity) — drawn first, under the dots
      for (i = 0; i < points.length; i++) {
        p = points[i];
        for (j = i + 1; j < points.length; j++) {
          q = points[j];
          var dx = p.ox - q.ox, dy = p.oy - q.oy;
          var d2 = dx * dx + dy * dy;
          if (d2 < LINK_DIST * LINK_DIST) {
            var a = (1 - Math.sqrt(d2) / LINK_DIST) * 0.22;
            ctx.strokeStyle = "rgba(" + rgb + "," + a.toFixed(3) + ")";
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(p.ox, p.oy); ctx.lineTo(q.ox, q.oy); ctx.stroke();
          }
        }
      }
      // dots
      for (i = 0; i < points.length; i++) {
        p = points[i];
        ctx.fillStyle = "rgba(" + rgb + "," + (0.18 + p.z * 0.30).toFixed(3) + ")";
        ctx.beginPath(); ctx.arc(p.ox, p.oy, p.r * p.z + 0.3, 0, 6.2832); ctx.fill();
      }
      requestAnimationFrame(frame);
    }

    resize();
    window.addEventListener("resize", function () { DPR = Math.min(window.devicePixelRatio || 1, 2); resize(); }, { passive: true });
    // pause when tab hidden (battery + CPU)
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { running = false; }
      else if (!running && !reduceMotion.matches) { running = true; requestAnimationFrame(frame); }
    });
    // stop entirely if the user flips reduced-motion on at runtime
    reduceMotion.addEventListener && reduceMotion.addEventListener("change", function (e) {
      if (e.matches) { running = false; ctx.clearRect(0, 0, W, H); }
    });
    requestAnimationFrame(frame);
  })();

  /* ── Count-up on the Overview signature numbers ──────────────────────── */
  function animateCount(el, target) {
    if (reduceMotion.matches) { return; } // leave Alpine's final value untouched
    var dur = 1100, start = null, fmt = (target >= 1000);
    function frame(ts) {
      if (start === null) start = ts;
      var t = Math.min((ts - start) / dur, 1);
      var eased = 1 - Math.pow(1 - t, 4);
      var val = Math.round(target * eased);
      el.textContent = fmt ? val.toLocaleString() : String(val);
      if (t < 1) requestAnimationFrame(frame);
      else el.textContent = fmt ? target.toLocaleString() : String(target);
    }
    requestAnimationFrame(frame);
  }
  function tryCountUp(el, tries) {
    if (el.dataset.counted) return;
    var raw = (el.textContent || "").trim().replace(/,/g, "");
    if (/^\d{1,9}$/.test(raw)) {            // clean integer Alpine has rendered
      el.dataset.counted = "1";
      var target = parseInt(raw, 10);
      if (target > 0) animateCount(el, target);
      return;
    }
    // value not ready yet (e.g. "—" before /api/overview resolves) — retry briefly
    if (tries > 0) setTimeout(function () { tryCountUp(el, tries - 1); }, 160);
  }

  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { tryCountUp(e.target, 16); io.unobserve(e.target); }
      });
    }, { threshold: 0.4 });
    document.querySelectorAll("[data-countup]").forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll("[data-countup]").forEach(function (el) { tryCountUp(el, 16); });
  }

  /* ── Keyboard layer ──────────────────────────────────────────────────── */
  var helpBackdrop = document.getElementById("kbdHelpBackdrop");
  var helpFab = document.getElementById("kbdHelpFab");

  var helpLastFocus = null;
  function helpOpen() {
    if (!helpBackdrop) return;
    helpLastFocus = document.activeElement;
    helpBackdrop.classList.add("is-open");
    var modal = helpBackdrop.querySelector(".kbd-help-modal");
    if (modal) { modal.setAttribute("tabindex", "-1"); modal.focus(); }
  }
  function helpClose() {
    if (!helpBackdrop) return;
    helpBackdrop.classList.remove("is-open");
    if (helpLastFocus && helpLastFocus.focus) { helpLastFocus.focus(); helpLastFocus = null; }
  }
  function helpToggle() { if (helpBackdrop) { helpBackdrop.classList.contains("is-open") ? helpClose() : helpOpen(); } }
  if (helpFab) helpFab.addEventListener("click", helpOpen);
  if (helpBackdrop) helpBackdrop.addEventListener("click", function (e) { if (e.target === helpBackdrop) helpClose(); });

  // Single-key tab navigation. Reads the Alpine component lazily and calls its
  // existing goTo() — no state shape or API touched.
  var KEY_TABS = {
    g: "overview", b: "briefing", a: "agents", c: "chat", u: "upload",
    q: "practice-quiz", f: "practice-flashcards", r: "research", p: "planner-focus"
  };
  function alpineData() {
    try {
      if (!window.Alpine || !Alpine.$data) return null;
      var root = document.querySelector("[x-data]");
      return root ? Alpine.$data(root) : null;
    } catch (e) { return null; }
  }
  function isTyping(t) {
    if (!t) return false;
    var tag = (t.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable;
  }

  document.addEventListener("keydown", function (e) {
    // ? opens help (Shift+/)
    if (e.key === "?" && !isTyping(e.target)) { e.preventDefault(); helpToggle(); return; }
    if (e.key === "Escape" && helpBackdrop && helpBackdrop.classList.contains("is-open")) {
      e.preventDefault(); helpClose(); return;
    }
    // Focus trap: while the keyboard-help modal is open, Tab loops within it.
    // Without this, Tab escapes the modal and walks the page behind, which
    // means screen-reader users (and anyone keyboard-only) can't tell the
    // modal is modal.
    if (e.key === "Tab" && helpBackdrop && helpBackdrop.classList.contains("is-open")) {
      var modal = helpBackdrop.querySelector(".kbd-help-modal");
      if (!modal) return;
      var focusables = modal.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (!focusables.length) { e.preventDefault(); modal.focus(); return; }
      var first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); return; }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); return; }
    }
    if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;

    var data = null;
    // don't hijack keys while the command palette is open
    var d0 = alpineData();
    if (d0 && d0.globalSearchOpen) return;

    // 1-4 grade the current recall card (active-recall engine)
    if (/^[1-4]$/.test(e.key) && d0 && d0.activeTab === "recall" && typeof d0.gradeRecall === "function") {
      try {
        if (d0.recall && d0.recall.queue && d0.recall.queue[d0.recall.idx]) {
          e.preventDefault(); d0.gradeRecall(parseInt(e.key, 10)); return;
        }
      } catch (_) { /* ignore */ }
    }

    var tab = KEY_TABS[e.key.toLowerCase()];
    if (tab) {
      data = d0 || alpineData();
      if (data && typeof data.goTo === "function") { e.preventDefault(); data.goTo(tab); }
    }
  });

  /* ── Physical layer: pointer aurora · 3D tilt · magnetic buttons ─────────
     All effects are skipped on touch / coarse pointers and under reduced
     motion, matching the CSS guards. They write the typed custom properties
     registered in style.css (@property) so the values interpolate. */
  var finePointer = window.matchMedia("(hover: hover) and (pointer: fine)");

  function physicalEnabled() {
    return finePointer.matches && !reduceMotion.matches;
  }

  // Pointer-tracking aurora glow — hero + sidebar only (per design contract).
  function bindAuroraGlow(el) {
    if (!el || el.dataset.glowBound) return;
    el.dataset.glowBound = "1";
    el.addEventListener("pointermove", function (e) {
      if (!physicalEnabled()) return;
      var r = el.getBoundingClientRect();
      var x = ((e.clientX - r.left) / r.width) * 100;
      var y = ((e.clientY - r.top) / r.height) * 100;
      el.style.setProperty("--aurora-x", x.toFixed(1) + "%");
      el.style.setProperty("--aurora-y", y.toFixed(1) + "%");
      el.classList.add("aurora-track");
    }, { passive: true });
    el.addEventListener("pointerleave", function () {
      el.classList.remove("aurora-track");
    });
  }
  // Note: the legacy .sidebar and .ov-hero glow targets were retired in the Volt
  // redesign (sidebar folded into the top bar; hero became the lime .ov-stat-hero
  // focal card, a light surface where a dark-bloom glow doesn't apply). The
  // bindAuroraGlow helper is retained for future opt-in surfaces but is currently
  // unbound.

  // 3D tilt for [data-tilt] cards + cursor-tracked specular highlight.
  var TILT_MAX = 6; // degrees
  function bindTilt(el) {
    el.classList.add("tilt-spec"); // enable the specular ::after overlay
    el.addEventListener("pointerenter", function () { el.classList.add("tilt-lit"); });
    el.addEventListener("pointermove", function (e) {
      if (!physicalEnabled()) return;
      var r = el.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width - 0.5;   // -0.5..0.5
      var py = (e.clientY - r.top) / r.height - 0.5;
      el.style.setProperty("--tilt-y", (px * TILT_MAX * 2).toFixed(2) + "deg");
      el.style.setProperty("--tilt-x", (-py * TILT_MAX * 2).toFixed(2) + "deg");
      // specular "light source" follows the cursor (percent within the card)
      el.style.setProperty("--mx", ((px + 0.5) * 100).toFixed(1) + "%");
      el.style.setProperty("--my", ((py + 0.5) * 100).toFixed(1) + "%");
    }, { passive: true });
    el.addEventListener("pointerleave", function () {
      el.style.setProperty("--tilt-x", "0deg");
      el.style.setProperty("--tilt-y", "0deg");
      el.classList.remove("tilt-lit");
    });
  }

  // Magnetic pull for [data-magnetic] buttons.
  var MAG_STRENGTH = 0.28, MAG_MAX = 10; // px
  function bindMagnetic(el) {
    el.addEventListener("pointermove", function (e) {
      if (!physicalEnabled()) return;
      var r = el.getBoundingClientRect();
      var dx = (e.clientX - (r.left + r.width / 2)) * MAG_STRENGTH;
      var dy = (e.clientY - (r.top + r.height / 2)) * MAG_STRENGTH;
      dx = Math.max(-MAG_MAX, Math.min(MAG_MAX, dx));
      dy = Math.max(-MAG_MAX, Math.min(MAG_MAX, dy));
      el.style.setProperty("--mag-x", dx.toFixed(1) + "px");
      el.style.setProperty("--mag-y", dy.toFixed(1) + "px");
    }, { passive: true });
    el.addEventListener("pointerleave", function () {
      // spring-like settle on release
      el.classList.add("mag-settle");
      el.style.setProperty("--mag-x", "0px");
      el.style.setProperty("--mag-y", "0px");
      setTimeout(function () { el.classList.remove("mag-settle"); }, 500);
    });
  }

  // Initial wiring + a light observer so dynamically rendered cards/buttons
  // (Alpine x-for / page mounts) also get wired without re-scanning constantly.
  function wirePhysical(root) {
    var scope = root || document;
    // Auto-tag premium Overview cards as tilt targets without per-card markup.
    scope.querySelectorAll(".quick-card:not([data-tilt])").forEach(function (el) {
      el.setAttribute("data-tilt", "");
    });
    // Auto-tag primary action buttons as magnetic (no per-button markup).
    scope.querySelectorAll(".btn-primary:not([data-magnetic]), .primary-btn:not([data-magnetic]), button.btn.primary:not([data-magnetic])").forEach(function (el) {
      el.setAttribute("data-magnetic", "");
    });
    scope.querySelectorAll("[data-tilt]:not([data-tilt-bound])").forEach(function (el) {
      el.setAttribute("data-tilt-bound", "1"); bindTilt(el);
    });
    scope.querySelectorAll("[data-magnetic]:not([data-mag-bound])").forEach(function (el) {
      el.setAttribute("data-mag-bound", "1"); bindMagnetic(el);
    });
  }
  wirePhysical(document);

  // Kinetic typography: warp [data-kinetic] headings on cursor proximity.
  // Subtle by design — ≤8% skew + small letter-spacing shift (directive spec).
  function bindKinetic(el) {
    if (el.dataset.kineticBound) return;
    el.dataset.kineticBound = "1";
    var RADIUS = 220; // px of influence
    function onMove(e) {
      if (!physicalEnabled()) return;
      var r = el.getBoundingClientRect();
      var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      var dx = e.clientX - cx, dy = e.clientY - cy;
      var dist = Math.sqrt(dx * dx + dy * dy);
      var influence = Math.max(0, 1 - dist / RADIUS); // 0..1
      var skew = (dx / Math.max(1, r.width)) * 6 * influence;   // ≤ ~6deg
      var spacing = (0.04 * influence);                          // em
      el.style.setProperty("--kin-skew", skew.toFixed(2) + "deg");
      el.style.setProperty("--kin-space", spacing.toFixed(3) + "em");
    }
    function reset() {
      el.style.setProperty("--kin-skew", "0deg");
      el.style.setProperty("--kin-space", "0em");
    }
    window.addEventListener("pointermove", onMove, { passive: true });
    el.addEventListener("pointerleave", reset);
  }
  function wireKinetic(root) {
    (root || document).querySelectorAll("[data-kinetic]").forEach(bindKinetic);
  }
  wireKinetic(document);

  // Split-flap: roll the visible glyph when a [data-flap] value changes.
  function animateFlaps(root) {
    if (reduceMotion.matches) return;
    (root || document).querySelectorAll("[data-flap]").forEach(function (host) {
      var next = host.getAttribute("data-flap-value");
      if (host.dataset.flapPrev === next) return;
      host.dataset.flapPrev = next;
      // x-html has just (re)rendered the digit cells; animate them in.
      host.querySelectorAll(".flap-digit").forEach(function (d, i) {
        d.classList.remove("flipping");
        // stagger digits slightly for a board-flip feel
        setTimeout(function () { d.classList.add("flipping"); }, i * 40);
      });
    });
  }

  if ("MutationObserver" in window) {
    var moThrottle = false;
    var mo = new MutationObserver(function () {
      if (moThrottle) return;
      moThrottle = true;
      requestAnimationFrame(function () {
        moThrottle = false;
        wirePhysical(document);
        wireKinetic(document);
        animateFlaps(document);
      });
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }
})();
