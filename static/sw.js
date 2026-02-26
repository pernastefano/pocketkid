const CACHE_NAME = 'pocketkid-v4';
const URLS_TO_CACHE = [
  '/static/css/styles.css',
  '/static/js/app.js',
  '/static/manifest.webmanifest',
  '/static/icons/logo.svg',
  '/static/icons/logo-192.png',
  '/static/icons/logo-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(URLS_TO_CACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then((networkResponse) => networkResponse)
        .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/login')))
    );
    return;
  }

  const isStaticAsset = event.request.url.includes('/static/');
  if (!isStaticAsset) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(event.request)
        .then((response) => {
          if (!response || response.status !== 200 || response.type !== 'basic') {
            return response;
          }

          const responseClone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseClone));
          return response;
        })
        .catch(() => caches.match('/login'));
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/dashboard';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if ('focus' in client) {
          client.postMessage({ type: 'PUSH_EVENT', payload: event.notification.data || {} });
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
      return undefined;
    })
  );
});

self.addEventListener('push', (event) => {
  let payload = { title: 'PocketKid', body: 'New event', url: '/dashboard' };
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (_) {
      payload = { title: 'PocketKid', body: event.data.text(), url: '/dashboard' };
    }
  }

  const showNotification = () => {
    return self.registration.showNotification(payload.title || 'PocketKid', {
      body: payload.body || '',
      icon: '/static/icons/logo-192.png',
      badge: '/static/icons/logo-192.png',
      tag: `push-${Date.now()}`,
      data: { url: payload.url || '/dashboard' },
      requireInteraction: false,
      vibrate: [200, 100, 200]
    });
  };

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      // Always show notification for reliability across all platforms
      // Send message to all open windows for real-time UI updates
      for (const client of windowClients) {
        client.postMessage({ type: 'PUSH_EVENT', payload });
      }
      
      return showNotification();
    })
  );
});
