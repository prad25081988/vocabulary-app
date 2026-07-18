const CACHE_NAME = 'vocab-app-v1';
const urlsToCache = [
    '/',
    '/index.html',
    '/manifest.json'
];

// Install service worker and cache files
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(urlsToCache);
        })
    );
});

// Fetch from cache if available
self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request).then(response => {
            if (response) return response;
            return fetch(event.request);
        })
    );
});