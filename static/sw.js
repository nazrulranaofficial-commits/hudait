// This is a minimal Service Worker required for PWA installation
self.addEventListener('install', (e) => {
  console.log('[Service Worker] Install');
});

self.addEventListener('fetch', (e) => {
  // We need a fetch handler, even if it does nothing, to satisfy PWA requirements
  // Logic: Just fetch from network normally
  e.respondWith(fetch(e.request));
});