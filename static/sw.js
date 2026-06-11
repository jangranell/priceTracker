// PriceTracker Service Worker — handles incoming push notifications

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data.json(); } catch { data = { title: "PriceTracker", body: event.data.text() }; }

  const options = {
    body:    data.body  || "New price drops detected.",
    icon:    "/static/icon.png",
    badge:   "/static/icon.png",
    tag:     data.tag   || "PriceTracker",
    renotify: true,
    requireInteraction: true,
    data:    { url: data.url || "/" },
    actions: [
      { action: "open", title: "See deals →" },
      { action: "dismiss", title: "Dismiss" },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(data.title || "🏷️ PriceTracker", options)
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  if (event.action === "dismiss") return;

  const target = event.notification.data?.url || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      // Focus an existing PriceTracker tab if open
      for (const client of list) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return clients.openWindow(target);
    })
  );
});

self.addEventListener("pushsubscriptionchange", (event) => {
  // Re-subscribe automatically if the subscription expires
  event.waitUntil(
    self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: event.oldSubscription?.options?.applicationServerKey,
    }).then((sub) =>
      fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sub.toJSON()),
      })
    )
  );
});
