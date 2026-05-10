const CACHE_NAME = 'zarzis-irrigation-v8.4-offline';
const API_CACHE = 'zarzis-api-cache-v8.4';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => ![CACHE_NAME, API_CACHE].includes(key))
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  if (url.pathname.startsWith('/api/')) {
    if (req.method !== 'GET') {
      event.respondWith(fetch(req));
      return;
    }
    event.respondWith(
      fetch(req)
        .then(response => {
          const copy = response.clone();
          caches.open(API_CACHE).then(cache => cache.put(req, copy));
          return response;
        })
        .catch(() => caches.match(req).then(cached => cached || new Response(
          JSON.stringify({success:false, offline:true, error:'API indisponible hors-ligne'}),
          {status: 503, headers: {'Content-Type': 'application/json'}}
        )))
    );
    return;
  }

  event.respondWith(
    fetch(req)
      .then(response => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(req, copy));
        return response;
      })
      .catch(() => caches.match(req).then(cached => cached || caches.match('./')))
  );
});
