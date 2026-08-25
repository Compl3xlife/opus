const CACHE = "opus-phone-v2";
const PRECACHE = [
  "./",
  "./index.html",
  "./callback.html",
  "./manifest.webmanifest",
  "./static/app.css",
  "./static/app.js",
  "./static/opus-client.js",
  "./static/icon.png",
  "./static/icon-192.png",
  "./static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then(async (cache) => {
      for (const url of PRECACHE) {
        try {
          await cache.add(url);
        } catch (_err) {
          /* optional assets such as icons may be generated later */
        }
      }
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== "GET") return;
  if (url.search) {
    event.respondWith(
      fetch(event.request).catch(async () => {
        return (await caches.match("./callback.html")) || (await caches.match("./index.html"));
      })
    );
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("./index.html")))
  );
});
