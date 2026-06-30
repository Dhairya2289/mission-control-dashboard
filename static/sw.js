/* Mission Control — service worker (PWA app shell cache).
 *
 * Served from /static/sw.js, so its scope is /static/ — it intercepts the app
 * shell (app.js, style.css, vendored libs, fonts, icons). The root document and
 * /api, /ws are NOT in scope, so they always hit the network normally (exactly
 * the desired behaviour: live data is never served stale).
 *
 * Strategy:
 *   - Never touch /api/* or /ws/*  (defensive — also out of scope anyway):
 *     pure network passthrough.
 *   - Static GET assets under scope: cache-first, then network, and refresh the
 *     cache in the background (stale-while-revalidate-ish).
 *   - The shell list is best-effort pre-cached on install.
 */
const CACHE = "mc-shell-v7";   // bumped for Nebula theme + deepened pure-dark pass

const SHELL = [
  "/static/style.css",
  "/static/app.js",
  "/static/aurora.js",
  "/static/fonts/fonts.css",
  "/static/vendor/alpine.min.js",
  "/static/vendor/apexcharts.min.js",
  "/static/vendor/marked.min.js",
  "/static/vendor/d3.min.js",
  "/static/vendor/plot.umd.min.js",
  "/static/vendor/mermaid.min.js",
  "/static/vendor/cytoscape.min.js",
  "/static/vendor/xterm.js",
  "/static/vendor/xterm.css",
  "/static/vendor/xterm-addon-fit.js",
  "/static/vendor/markmap-view.js",
  "/static/vendor/markmap-lib.js",
  "/static/vendor/markmap-toolbar.js",
  "/static/vendor/markmap-toolbar.css",
  "/static/manifest.webmanifest",
  "/static/favicon.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // Best-effort: a single 404 must not abort the whole install.
      Promise.allSettled(SHELL.map((url) => cache.add(url)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                 // never cache mutations

  const url = new URL(req.url);

  // Live endpoints — always go to the network, never cache.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) {
    return; // default browser handling (network)
  }

  // Only manage same-origin static assets under our scope.
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith("/static/")) return;

  // Cache-first with background refresh.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === "basic") {
            cache.put(req, res.clone());
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
