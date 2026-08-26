"""Client HTTP minimal pour l'API REST v20 d'OANDA.

Contient à la fois les appels en lecture seule (instruments, bougies,
solde de compte, positions ouvertes) et le passage d'ordre (Phase 3).
Le passage d'ordre est protégé côté application par `Settings.orders_allowed`
— voir main.py — jamais directement ici.

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

    async def get_account_summary(self) -> dict:
        """Solde, devise et P/L non réalisé du compte."""
        url = (
            f"{self.settings.oanda_base_url}/v3/accounts/"
            f"{self.settings.oanda_account_id}/summary"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json().get("account", {})

    async def list_open_trades(self) -> list[dict]:
        url = (
            f"{self.settings.oanda_base_url}/v3/accounts/"
            f"{self.settings.oanda_account_id}/openTrades"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json().get("trades", [])

    async def create_market_order_with_brackets(
        self,
        instrument: str,
        units: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict:
        """Passe un ordre au marché avec stop-loss ET take-profit attachés.

        C'est OANDA qui gère la fermeture automatique de la position une
        fois l'un des deux prix atteint — ça ne dépend pas de notre serveur
        qui pourrait être arrêté ou injoignable à ce moment-là.

        `units` positif = achat, négatif = vente.
        """
        url = f"{self.settings.oanda_base_url}/v3/accounts/{self.settings.oanda_account_id}/orders"
        body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{stop_loss_price:.5f}"},
                "takeProfitOnFill": {"price": f"{take_profit_price:.5f}"},
            }
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=self._headers(), json=body)
        if resp.status_code not in (200, 201):
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json()

    async def get_trade(self, trade_id: str) -> dict:
        """État d'un trade précis : "OPEN" ou "CLOSED", et son P/L réalisé.

        C'est ce qui permet à une session de savoir quand le stop-loss ou le
        take-profit a été touché, et combien le trade a réellement rapporté
        ou coûté — sans se fier à une estimation locale.
        """
        url = (
            f"{self.settings.oanda_base_url}/v3/accounts/"
            f"{self.settings.oanda_account_id}/trades/{trade_id}"
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json().get("trade", {})

    async def get_pricing(self, instruments: list[str]) -> dict[str, dict]:
        """Prix acheteur/vendeur actuels, et donc le spread réellement payé.

        Les bougies renvoient des prix *médians* : s'en servir pour calculer
        une entrée revient à ignorer le spread, et donc à sous-estimer le
        coût de chaque trade. Pour dimensionner correctement une position il
        faut le vrai prix auquel l'ordre sera exécuté — `ask` à l'achat,
        `bid` à la vente.
        """
        url = (
            f"{self.settings.oanda_base_url}/v3/accounts/"
            f"{self.settings.oanda_account_id}/pricing"
        )
        params = {"instruments": ",".join(instruments)}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")

        prices: dict[str, dict] = {}
        for entry in resp.json().get("prices", []):
            bids, asks = entry.get("bids") or [], entry.get("asks") or []
            if not bids or not asks:
                continue
            bid, ask = float(bids[0]["price"]), float(asks[0]["price"])
            prices[entry["instrument"]] = {
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "tradeable": entry.get("tradeable", True),
            }
        return prices
