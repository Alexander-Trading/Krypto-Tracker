/* Service Worker des Krypto-Trackers
 *
 * Zweck: Die Python-Laufzeit (pyodide/, ~12 MB) dauerhaft auf dem Geraet
 * speichern. Ohne Service Worker fragt Safari nach Ablauf der Cache-Zeit
 * von GitHub Pages (10 Minuten) bei jedem Oeffnen neu nach - auf dem iPhone
 * kostet das spuerbar Zeit.
 *
 * Bewusst NUR pyodide/ und pyodide-pkgs/ (aendern sich nie, solange die
 * Pyodide-Version gleich bleibt). index.html und py/*.py gehen immer direkt
 * ans Netz, damit Updates sofort ankommen (siehe APP_BUILD in index.html).
 *
 * Neue Pyodide-Version eingespielt? CACHE-Namen hochzaehlen - dann wird der
 * alte Speicher beim naechsten Start verworfen.
 */
const CACHE = "kt-runtime-v1";
const CACHED_PREFIXES = ["pyodide/", "pyodide-pkgs/"];

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("kt-runtime-") && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const scope = new URL(self.registration.scope);
  const rel = url.pathname.startsWith(scope.pathname)
    ? url.pathname.slice(scope.pathname.length) : url.pathname;
  if (!CACHED_PREFIXES.some(p => rel.startsWith(p))) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const hit = await cache.match(req, {ignoreSearch: true});
    if (hit) return hit;
    const resp = await fetch(req);
    if (resp.ok && resp.status === 200) cache.put(req, resp.clone()).catch(() => {});
    return resp;
  })());
});
