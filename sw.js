const CACHE = 'vn-port-watch-v2'
const ASSETS = ['./', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)))
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ),
  )
  self.clients.claim()
})

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  // network-first for HTML + data JSON (dashboard must never serve stale ops data)
  if (e.request.mode === 'navigate' || url.pathname.endsWith('.json')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)))
    return
  }
  // cache-first for static assets
  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)))
})
