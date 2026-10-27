// Bump this whenever the production shell/API wiring changes so installed
// PWAs do not keep running an older JavaScript bundle after deployment.
const CACHE = "lead-gennie-shell-v3";
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon-192.svg", "/icons/icon-512.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(Promise.all([
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("lead-gennie-shell-") && key !== CACHE).map((key) => caches.delete(key)))),
    self.clients.claim(),
  ]));
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET" || new URL(event.request.url).origin !== self.location.origin) return;
  const url = new URL(event.request.url);
  // API responses can contain personal contact data and source images. Always
  // fetch them directly and never store them in the PWA cache.
  if (/^\/(?:api|documents|leads|exports)(?:\/|$)/.test(url.pathname)) return;
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      if (cached) return cached;
      const response = await fetch(event.request);
      if (response.ok) await cache.put(event.request, response.clone());
      return response;
    }));
    return;
  }
  event.respondWith(fetch(event.request).then(async (response) => {
    if (response.ok && SHELL.includes(url.pathname)) {
      const cache = await caches.open(CACHE);
      await cache.put(event.request, response.clone());
    }
    return response;
  }).catch(() => caches.match(event.request).then((cached) => cached || caches.match("/"))));
});
