const CACHE_NAME = 'vibegram-pwa-v1';
const APP_SHELL = ['/', '/manifest.webmanifest', '/facuicon.jpeg', '/pwa-192.png', '/pwa-512.png'];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(APP_SHELL).catch(() => {});
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith('vibegram-pwa-') && key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response && response.ok) {
          const cache = await caches.open(CACHE_NAME);
          cache.put('/', response.clone()).catch(() => {});
        }
        return response;
      } catch (_) {
        return (await caches.match('/')) || new Response('Нет подключения к интернету. Подключись к сети и обнови Vibegram.', {
          status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      }
    })());
    return;
  }
  if (APP_SHELL.includes(url.pathname) || url.pathname.startsWith('/uploads/')) {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      const network = fetch(request).then(response => {
        if (response && response.ok) caches.open(CACHE_NAME).then(cache => cache.put(request, response.clone()));
        return response;
      }).catch(() => cached);
      return cached || network;
    })());
  }
});

self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {}
  const username = String(payload.username || '').toLowerCase();
  const title = String(payload.title || 'Vibegram').slice(0, 100);
  const options = {
    body: String(payload.body || 'Новое сообщение').slice(0, 240),
    icon: payload.icon || '/facuicon.jpeg',
    badge: payload.badge || '/pwa-192.png',
    tag: payload.tag || ('vibegram-' + username),
    renotify: false,
    data: {
      username,
      url: payload.url || ('/?openChat=' + encodeURIComponent(username)),
      payload: { title, body: String(payload.body || 'Новое сообщение').slice(0, 240), username }
    }
  };

  event.waitUntil((async () => {
    const openClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const visibleClient = openClients.find(client => client.visibilityState === 'visible');
    if (visibleClient) {
      // Avoid duplicate OS notifications: the foreground page already shows a local notification
      // when the incoming conversation is not the currently open chat.
      visibleClient.postMessage({ type: 'VIBEGRAM_PUSH_RECEIVED', username, payload: options.data.payload });
      return;
    }
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const username = String(event.notification.data?.username || '').toLowerCase();
  const requestedUrl = String(event.notification.data?.url || ('/?openChat=' + encodeURIComponent(username)));
  event.waitUntil((async () => {
    const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clientsList) {
      if ('focus' in client) {
        await client.focus();
        if (username) client.postMessage({ type: 'VIBEGRAM_OPEN_CHAT', username });
        return;
      }
    }
    await self.clients.openWindow(requestedUrl);
  })());
});
