// Offline shell only. Data and images always come from the network: a cached
// approval queue would show the owner decisions that are no longer true.
const SHELL = "aiworker-shell-v1";
const FILES = ["./", "index.html", "app.js", "app.css", "theme.css", "config.js",
  "vendor/supabase-2.117.3.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  // Network first, so a deploy shows up on the next open; the cache is the
  // fallback for opening the app with no signal.
  e.respondWith(fetch(e.request)
    .then((res) => {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put(e.request, copy));
      return res;
    })
    .catch(() => caches.match(e.request)));
});
