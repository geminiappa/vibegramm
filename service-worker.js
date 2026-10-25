/* Vibegram notification worker. System notifications keep the sender avatar; this worker handles notification clicks. */
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const username = String(event.notification.data?.username || '').toLowerCase();
  event.waitUntil((async () => {
    const clientsList = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of clientsList) {
      if ('focus' in client) {
        await client.focus();
        if (username) client.postMessage({ type: 'VIBEGRAM_OPEN_CHAT', username });
        return;
      }
    }
    await self.clients.openWindow('/');
  })());
});

self.addEventListener('push', event => {
  // Reserved for future Web Push integration. Current delivery is initiated by the
  // already-connected Vibegram client, with the sender avatar in notification.icon.
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {}
  const title = String(payload.title || 'Vibegram').slice(0, 100);
  const options = {
    body: String(payload.body || 'Новое сообщение').slice(0, 240),
    icon: payload.icon || '/facuicon.jpeg?v=1',
    badge: '/facuicon.jpeg?v=1',
    data: { username: String(payload.username || '').toLowerCase() }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
