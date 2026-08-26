// Service worker de la PWA : installation + réception des notifications push.
//
// C'est ce fichier qui permet de recevoir un palier (25/50/75/100%) même
// quand l'app est fermée ou l'écran verrouillé — le navigateur réveille le
// service worker pour afficher la notification.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = { title: "Session de trading", body: event.data ? event.data.text() : "" };
  }

  const title = data.title || "Session de trading";
  const options = {
    body: data.body || "",
    // Un tag par type+palier : une nouvelle notification du même palier
    // remplace l'ancienne au lieu d'empiler des doublons.
    tag: `${data.kind || "session"}-${data.percent || 0}`,
    data: { session_id: data.session_id, kind: data.kind, percent: data.percent },
    // Les paliers de perte et la fin de session méritent d'insister.
    requireInteraction: data.kind === "loss" || data.percent === 100,
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Ramène l'onglet existant au premier plan plutôt que d'en ouvrir un autre.
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("./index.html");
    })
  );
});
