/* Energy Analytics — cache for instant loading.
   Opens from cache straight away, then quietly fetches the latest copy from
   GitHub in the background. Your edits show up from the next visit.
   If you ever change the list of files below, raise the version number. */
const CACHE = 'energy-analytics-v3';
const FILES = ['./', './index.html', './dashboards.json', './manifest.webmanifest', './icon.svg', './icon-192.png', './icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  const url = new URL(req.url);
  if (url.pathname.endsWith('/dashboards.json')) {
    const key = new Request(url.origin + url.pathname);
    if (url.searchParams.has('fresh')) {
      // Background check from the page: always ask GitHub, save the answer
      e.respondWith(caches.open(CACHE).then(cache =>
        fetch(key, { cache: 'no-cache' }).then(res => { if (res && res.ok) cache.put(key, res.clone()); return res; })
      ));
    } else {
      // First draw: saved copy at once; network only if nothing is saved yet
      e.respondWith(caches.open(CACHE).then(cache =>
        cache.match(key).then(c => c || fetch(key, { cache: 'no-cache' }).then(res => { if (res && res.ok) cache.put(key, res.clone()); return res; }))
      ));
    }
    return;
  }
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(req, { ignoreSearch: true }).then(cached => {
        const fresh = fetch(req, { cache: 'no-cache' })
          .then(res => { if (res && res.ok) cache.put(req, res.clone()); return res; })
          .catch(() => cached);
        return cached || fresh;
      })
    )
  );
});
