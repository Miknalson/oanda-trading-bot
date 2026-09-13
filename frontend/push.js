// Abonnement de la PWA aux notifications push.
//
// Pas de `export` ici : index.html charge ce fichier en script classique.
// Un `export` dans un script non-module lève « Unexpected token 'export' »,
// et c'est TOUT le fichier qui ne s'exécute pas — le bouton d'activation
// restait donc muet sans aucune erreur visible à l'écran.
//
// À appeler depuis un geste utilisateur (clic sur un bouton) : les
// navigateurs refusent une demande de permission déclenchée automatiquement
// au chargement de la page.

// Même origine par défaut : le backend sert cette page, donc les chemins
// relatifs suffisent et il n'y a aucun CORS à configurer. `window.API_BASE`
// reste utile pour viser un backend distant depuis un fichier local.
const PUSH_API_BASE = window.API_BASE || "";

function urlBase64ToUint8Array(base64String) {
  // La clé VAPID est en base64url ; l'API navigateur attend un Uint8Array.
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePushNotifications() {
  // Distinguer les deux causes : elles appellent des actions opposées.
  // Une connexion non sécurisée se règle côté serveur (HTTPS), pas en
  // installant l'app sur l'écran d'accueil.
  if (!window.isSecureContext) {
    throw new Error(
      `Connexion non sécurisée (${location.protocol}//${location.hostname}). ` +
        "Les notifications exigent HTTPS, ou localhost sur la machine même. " +
        "Un tunnel (Cloudflare Tunnel, ngrok) ou un hébergeur donne une URL " +
        "HTTPS sans rien changer au code."
    );
  }
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

async function disablePushNotifications() {
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

// Exposé explicitement : ces fonctions sont appelées depuis app.js, qui est
// lui aussi un script classique.
window.enablePushNotifications = enablePushNotifications;
window.disablePushNotifications = disablePushNotifications;
