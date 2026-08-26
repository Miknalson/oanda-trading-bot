// Abonnement de la PWA aux notifications push.
//
// À appeler depuis un geste utilisateur (clic sur un bouton) : les
// navigateurs refusent une demande de permission déclenchée automatiquement
// au chargement de la page.

const PUSH_API_BASE = window.API_BASE || "http://localhost:8000";

function urlBase64ToUint8Array(base64String) {
  // La clé VAPID est en base64url ; l'API navigateur attend un Uint8Array.
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

export async function enablePushNotifications() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    throw new Error(
      "Ce navigateur ne gère pas les notifications push. Sur iPhone, ajoute " +
        "d'abord l'app à l'écran d'accueil (Partager → Sur l'écran d'accueil)."
    );
  }

  const config = await fetch(`${PUSH_API_BASE}/api/notifications/config`).then((r) => r.json());
  if (!config.push_enabled || !config.vapid_public_key) {
    throw new Error(
      "Le serveur n'a pas de clés VAPID configurées — voir la section " +
        "Notifications du README."
    );
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notifications refusées. Autorise-les dans les réglages du navigateur.");
  }

  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(config.vapid_public_key),
    });
  }

  const res = await fetch(`${PUSH_API_BASE}/api/notifications/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription.toJSON()),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Le serveur a refusé l'abonnement (${res.status}).`);
  }

  return res.json();
}

export async function disablePushNotifications() {
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  if (!subscription) return { removed: false };

  await fetch(`${PUSH_API_BASE}/api/notifications/unsubscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscription.toJSON()),
  });
  await subscription.unsubscribe();
  return { removed: true };
}
