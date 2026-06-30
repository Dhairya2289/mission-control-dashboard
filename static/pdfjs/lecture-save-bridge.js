/* Lecture Notes save bridge — injected into PDF.js viewer.
 *
 * Strategy:
 *   1. Wait for `webviewerloaded`, then grab `window.PDFViewerApplication`.
 *   2. Once the document is open, listen to the event bus for annotation
 *      changes and debounce-trigger a save.
 *   3. On `visibilitychange` (tab hidden) trigger an immediate save.
 *   4. Expose `window.LectureSave` with saveNow() so the parent (or a
 *      toolbar button) can force a save.
 *
 * Saves grab the current annotated PDF bytes via
 *   `PDFViewerApplication.pdfDocument.saveDocument()`
 * which returns a Uint8Array with all annotations embedded. We base64
 * the bytes and post them to the parent (Alpine) for the dashboard API
 * to PUT back to disk.
 */
(function () {
  "use strict";

  const DEBOUNCE_MS = 1200;
  const PARENT_ORIGIN = window.location.origin;
  let debounceTimer = null;
  let saving = false;
  let pendingPdf = null;
  let docReady = false;
  let docFilename = "";

  function getParams() {
    const p = new URLSearchParams(window.location.search);
    return {
      file: p.get("file") || "",
      pdfId: p.get("pdfId") || "",
    };
  }

  function arrayBufferToBase64(buf) {
    const bytes = new Uint8Array(buf);
    // chunked to avoid call-stack limits on big PDFs
    const CHUNK = 0x8000;
    let bin = "";
    for (let i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(
        null, bytes.subarray(i, Math.min(i + CHUNK, bytes.length))
      );
    }
    return btoa(bin);
  }

  async function captureAnnotatedBytes() {
    const app = window.PDFViewerApplication;
    if (!app || !app.pdfDocument) {
      throw new Error("PDF.js viewer not ready");
    }
    if (typeof app.pdfDocument.saveDocument !== "function") {
      throw new Error("PDF.js viewer is too old to expose saveDocument()");
    }
    const data = await app.pdfDocument.saveDocument();
    return data; // Uint8Array
  }

  function postToParent(type, payload) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(
          { source: "lecture-save-bridge", type, ...payload },
          PARENT_ORIGIN
        );
      }
    } catch (e) { /* parent may be cross-origin; ignore */ }
  }

  async function flushSave(reason) {
    if (saving) return;
    if (!docReady) return;
    saving = true;
    try {
      const bytes = await captureAnnotatedBytes();
      const b64 = arrayBufferToBase64(bytes);
      const { pdfId } = getParams();
      postToParent("save:start", { reason, size: bytes.byteLength });
      if (window.parent && window.parent !== window) {
        // Trigger the parent Alpine handler. The parent window has
        // `Alpine` data on `document.body.__x` (Alpine 3.x).
        const parentApp = window.parent;
        const component = parentApp.document?.body
          ? parentApp.Alpine?.$data(parentApp.document.body)
          : null;
        if (component && typeof component._saveLectureFromViewer === "function") {
          await component._saveLectureFromViewer(pdfId, b64);
        } else {
          // Fallback: emit a CustomEvent the parent can listen to.
          postToParent("save:bytes", { pdfId, base64: b64 });
        }
      }
      postToParent("save:done", { reason, size: bytes.byteLength });
    } catch (e) {
      postToParent("save:error", { reason, message: String(e) });
    } finally {
      saving = false;
    }
  }

  function debouncedSave(reason) {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => flushSave(reason || "debounce"), DEBOUNCE_MS);
  }

  function wireEvents() {
    const app = window.PDFViewerApplication;
    if (!app) return false;
    if (app.eventBus) {
      // Annotation editor changes (highlights, free-text, ink, etc.)
      const events = [
        "annotationeditorlayerrendered",
        "annotationeditoruimanager",
        "annotationeditormodechanged",
        "annotationeditorparamschanged",
      ];
      for (const ev of events) {
        try {
          app.eventBus._on(ev, () => debouncedSave("annotation:" + ev), {
            once: false,
          });
        } catch (e) { /* not all events exist on every build */ }
      }
    }
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        // Immediate, not debounced — flush on tab switch
        if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
        flushSave("visibility-hidden");
      }
    });
    window.addEventListener("pagehide", () => {
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
      flushSave("pagehide");
    });
    return true;
  }

  function onDocLoaded() {
    docReady = true;
    const app = window.PDFViewerApplication;
    if (app && app.documentInfo) {
      docFilename = app.documentInfo.Title || docFilename;
    }
  }

  function onWebViewerLoaded() {
    if (!wireEvents()) {
      // try again on next tick — viewer.mjs assigns PDFViewerApplication
      // synchronously, so this should always succeed on first try.
      setTimeout(onWebViewerLoaded, 50);
      return;
    }
    const app = window.PDFViewerApplication;
    if (app.eventBus) {
      try {
        app.eventBus._on("documentloaded", onDocLoaded, { once: true });
      } catch (e) { /* eventBus may be missing on early frames */ }
    }
    // Public API for parent
    window.LectureSave = {
      saveNow: () => flushSave("manual"),
      debouncedSave: (reason) => debouncedSave(reason || "manual-debounce"),
      isReady: () => docReady,
    };
    // Tell parent the bridge is alive
    postToParent("bridge:ready", { debounceMs: DEBOUNCE_MS });
  }

  if (document.readyState === "complete" || document.readyState === "interactive") {
    onWebViewerLoaded();
  } else {
    document.addEventListener("DOMContentLoaded", onWebViewerLoaded, { once: true });
  }
})();
