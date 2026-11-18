// Service worker for one team's pay page (scope /p/<token>/). Keeps the app shell for offline starts;
// the API is never cached, so amounts and statuses always come from the server.
"use strict";
const VERSION = "tf-web-12";
const SHELL = ["./", "app.js", "app.css", "icon.svg", "icon-192.png", "icon-512.png", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  // network first, so a deploy shows up at once; the cached copy only when offline
  e.respondWith(fetch(e.request)
    .then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(e.request, copy)); }
      return res;
    })
    .catch(() => caches.match(e.request, { ignoreSearch: true })));
});
