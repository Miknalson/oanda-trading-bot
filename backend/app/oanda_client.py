"""Courtier OANDA (API REST v20).

⚠️ Disponibilité : l'entité européenne d'OANDA (OANDA TMS Brokers, qui sert
les clients français depuis 2023) ne propose PAS cette API — son offre passe
par MetaTrader 5. Ce client reste utilisable avec les entités qui exposent
v20 (OANDA Corporation, Global Markets, Australia, Asia Pacific).

Doc : https://developer.oanda.com/rest-live-v20/introduction/
"""
from __future__ import annotations

import httpx

from .broker import (
    AccountSummary,
    BrokerError,
    Candle,
    OpenTrade,
    Quote,
    TradeStatus,
)
from .config import Settings, get_settings


class OandaError(BrokerError):
    """Conservé pour compatibilité — alias de BrokerError."""


class OandaClient:
    # Plafond documenté de /v3/instruments/{}/candles : 5000 par requête.
    max_candles_per_request = 5000

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.oanda_api_key:
            raise OandaError(
                "OANDA_API_KEY manquant. Copie backend/.env.example en backend/.env "
                "et renseigne ta clé API."
            )

    @property
    def _base_url(self) -> str:
        if self.settings.oanda_environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.oanda_api_key}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self._base_url}{path}", headers=self._headers(), params=params
            )
        if resp.status_code != 200:
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")
        return resp.json()

    @property
    def _account_path(self) -> str:
        return f"/v3/accounts/{self.settings.oanda_account_id}"

    # ---- Interface Broker ------------------------------------------------

    async def list_instruments(self) -> list[str]:
        data = await self._get(f"{self._account_path}/instruments")
        return [i["name"] for i in data.get("instruments", [])]

    async def get_candles(
        self, instrument: str, granularity: str = "M5", count: int = 50,
        before: str | None = None,
    ) -> list[Candle]:
        params: dict = {
            "granularity": granularity,
            "count": min(count, self.max_candles_per_request),
            "price": "M",
        }
        # v20 borne la fenêtre par la fin avec `to` : combiné à `count`, il
        # renvoie les `count` bougies qui précèdent cet instant.
        if before:
            params["to"] = before

        data = await self._get(f"/v3/instruments/{instrument}/candles", params)
        return [
            Candle(
                open=float(c["mid"]["o"]),
                high=float(c["mid"]["h"]),
                low=float(c["mid"]["l"]),
                close=float(c["mid"]["c"]),
                time=str(c.get("time", "")),
            )
            for c in data.get("candles", [])
            if c.get("mid")
        ]

    async def get_quote(self, instrument: str) -> Quote:
        data = await self._get(
            f"{self._account_path}/pricing", {"instruments": instrument}
        )
        for entry in data.get("prices", []):
            bids, asks = entry.get("bids") or [], entry.get("asks") or []
            if not bids or not asks:
                continue
            return Quote(
                bid=float(bids[0]["price"]),
                ask=float(asks[0]["price"]),
                tradeable=entry.get("tradeable", True),
            )
        raise OandaError(f"Aucun prix disponible pour {instrument}.")

    async def get_account_summary(self) -> AccountSummary:
        account = (await self._get(f"{self._account_path}/summary")).get("account", {})
        return AccountSummary(
            balance=float(account.get("balance", 0)),
            currency=account.get("currency", ""),
        )

    async def place_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
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
            resp = await client.post(
                f"{self._base_url}{self._account_path}/orders",
                headers=self._headers(),
                json=body,
            )
        if resp.status_code not in (200, 201):
            raise OandaError(f"OANDA a renvoyé {resp.status_code}: {resp.text}")

        result = resp.json()
        trade_id = (
            result.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID")
        )
        if not trade_id:
            raise OandaError(
                f"Ordre passé mais aucun tradeID renvoyé par OANDA : {result}"
            )
        return trade_id

    async def get_trade_status(self, trade_id: str) -> TradeStatus:
        trade = (await self._get(f"{self._account_path}/trades/{trade_id}")).get(
            "trade", {}
        )
        return TradeStatus(
            closed=trade.get("state") == "CLOSED",
            market_pl=float(trade.get("realizedPL", 0)),
            financing=float(trade.get("financing", 0)),
        )

    async def list_open_trades(self) -> list[OpenTrade]:
        trades = (await self._get(f"{self._account_path}/openTrades")).get("trades", [])
        return [
            OpenTrade(
                trade_id=t["id"],
                instrument=t["instrument"],
                units=float(t.get("currentUnits", 0)),
                price=float(t.get("price", 0)),
                unrealized_pl=float(t.get("unrealizedPL", 0)),
            )
            for t in trades
        ]
