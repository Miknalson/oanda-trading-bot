"""Client HTTP minimal pour l'API REST v20 d'OANDA.

Phase 1 : uniquement des appels en LECTURE SEULE (liste des instruments,
bougies de prix). Aucune fonction de passage d'ordre n'est implémentée ici
volontairement — voir le README pour la feuille de route par phases.

Doc officielle : https://developer.oanda.com/rest-live-v20/introduction/
"""
from __future__ import annotations

import httpx

from .config import Settings, get_settings


class OandaError(RuntimeError):
    """Erreur renvoyée par l'API OANDA (statut HTTP non-2xx)."""


class OandaClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.oanda_api_key:
            raise OandaError(
                "OANDA_API_KEY manquant. Copie backend/.env.example en backend/.env "
                "et renseigne ta clé API (compte démo recommandé pour commencer)."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.oanda_api_key}",
            "Content-Type": "application/json",
        }

    async def list_tradable_instruments(self) -> list[dict]:
        """Retourne les instruments disponibles sur le compte configuré."""
        url = (
            f"{self.settings.oanda_base_url}/v3/accounts/"
            f"{self.settings.oanda_account_id}/instruments"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json().get("instruments", [])

    async def get_candles(
        self, instrument: str, granularity: str = "M5", count: int = 50
    ) -> list[dict]:
        """Récupère les N dernières bougies pour un instrument.

        granularity: ex. "M1", "M5", "M15", "H1", "H4", "D".
        """
        url = f"{self.settings.oanda_base_url}/v3/instruments/{instrument}/candles"
        params = {"granularity": granularity, "count": count, "price": "M"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json().get("candles", [])
