// Service worker minimal — permet l'installation en PWA.
// Pas de cache agressif pour l'instant : les données de marché doivent
// toujours être fraîches.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
