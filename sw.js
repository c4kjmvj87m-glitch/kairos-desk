// KAIROS: de app werkt ook zonder netwerk met de laatste desk. Desk-data altijd eerst vers van GitHub.
const SHELL = "kairos-shell-v2";
const DATA = "kairos-data-v2";
const FONTS = "kairos-fonts-v1";
const ASSETS = ["./", "./index.html", "./kairos-shim.js", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png", "./icons/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => ![SHELL, DATA, FONTS].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function markCached(res) {
  const h = new Headers(res.headers);
  h.set("X-Kairos-Cache", "1");
  return res.blob().then((b) => new Response(b, { status: 200, statusText: "OK", headers: h }));
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Lettertypes van Google: eerst uit de cache, zodat het uiterlijk ook offline gelijk blijft.
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    e.respondWith(
      caches.open(FONTS).then((c) => c.match(req).then((m) => {
        const net = fetch(req).then((res) => { if (res.ok || res.type === "opaque") c.put(req, res.clone()); return res; }).catch(() => m);
        return m || net;
      }))
    );
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes("/data/")) {
    const key = url.origin + url.pathname;
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) { const copy = res.clone(); caches.open(DATA).then((c) => c.put(key, copy)); }
          return res;
        })
        .catch(() => caches.open(DATA).then((c) => c.match(key)).then((m) => (m ? markCached(m) : Response.error())))
    );
    return;
  }
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
        return res;
      })
      .catch(() => caches.match(req, { ignoreSearch: true }).then((m) => m || caches.match("./index.html")))
  );
});
