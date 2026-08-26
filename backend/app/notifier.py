"""Envoi des notifications de palier vers le téléphone (Web Push).

Deux canaux, complémentaires :

1. **Historique en mémoire** — toujours actif. Chaque palier est conservé
   dans la session et lisible via l'API. Suffit quand l'app est ouverte :
   elle interroge l'API et affiche le palier.
2. **Web Push** — optionnel, à configurer. C'est le seul canal qui atteint
   le téléphone quand l'app est fermée ou l'écran verrouillé. Nécessite une
   paire de clés VAPID et un abonnement enregistré depuis la PWA.

Sans clés VAPID configurées, le push est simplement désactivé : les paliers
continuent d'être enregistrés, rien ne casse.

Générer une paire de clés VAPID (une fois) :

    python -c "from app.notifier import generate_vapid_keys; generate_vapid_keys()"
"""
from __future__ import annotations

import json
import logging

from .milestones import Milestone

logger = logging.getLogger(__name__)

try:  # pywebpush est optionnel — le backend tourne sans.
    from pywebpush import WebPushException, webpush

    WEBPUSH_AVAILABLE = True
except ImportError:  # pragma: no cover - dépend de l'environnement
    WEBPUSH_AVAILABLE = False


def generate_vapid_keys() -> tuple[str, str]:
    """Affiche une paire de clés VAPID à coller dans backend/.env."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    private_b64, public_b64 = b64(private_raw), b64(public_raw)
    print("Ajoute ces lignes à backend/.env :\n")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print("VAPID_CLAIM_EMAIL=mailto:ton@email.com")
    return private_b64, public_b64


class Notifier:
    """Diffuse les paliers aux abonnements Web Push enregistrés."""

    def __init__(
        self,
        vapid_private_key: str = "",
        vapid_public_key: str = "",
        vapid_claim_email: str = "",
    ) -> None:
        self.vapid_private_key = vapid_private_key
        self.vapid_public_key = vapid_public_key
        self.vapid_claim_email = vapid_claim_email
        # En mémoire : un redémarrage du serveur oblige à se réabonner
        # depuis la PWA. Suffisant pour un usage personnel.
        self.subscriptions: list[dict] = []

    @property
    def push_enabled(self) -> bool:
        return bool(
            WEBPUSH_AVAILABLE and self.vapid_private_key and self.vapid_claim_email
        )

    def subscribe(self, subscription: dict) -> bool:
        """Enregistre un abonnement PWA. Renvoie False si déjà connu."""
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("Abonnement invalide : 'endpoint' manquant.")
        if any(s.get("endpoint") == endpoint for s in self.subscriptions):
            return False
        self.subscriptions.append(subscription)
        return True

    def unsubscribe(self, endpoint: str) -> bool:
        before = len(self.subscriptions)
        self.subscriptions = [s for s in self.subscriptions if s.get("endpoint") != endpoint]
        return len(self.subscriptions) < before

    def send_milestone(self, milestone: Milestone, session_id: str) -> int:
        """Envoie le palier à tous les abonnés. Renvoie le nombre d'envois réussis."""
        if not self.push_enabled or not self.subscriptions:
            logger.info(
                "Palier %s %d%% (session %s) — push non envoyé (%s)",
                milestone.kind,
                int(milestone.threshold * 100),
                session_id,
                "push désactivé" if not self.push_enabled else "aucun abonné",
            )
            return 0

        payload = json.dumps(
            {
                "title": milestone.title,
                "body": milestone.body,
                "kind": milestone.kind,
                "percent": int(milestone.threshold * 100),
                "session_id": session_id,
            }
        )

        sent, dead = 0, []
        for sub in self.subscriptions:
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims={"sub": self.vapid_claim_email},
                )
                sent += 1
            except WebPushException as exc:
                # 404/410 = abonnement expiré côté navigateur, on le retire.
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    dead.append(sub.get("endpoint"))
                logger.warning("Échec du push vers %s : %s", sub.get("endpoint"), exc)

        for endpoint in dead:
            self.unsubscribe(endpoint)

        return sent
