"""Courtier Saxo Bank (OpenAPI).

Pourquoi Saxo : c'est le seul des courtiers étudiés qui donne accès à son API
sans compte réel ni vérification d'identité. Le portail développeur ouvre un
environnement de simulation — copie du réel, avec 100 000 $ fictifs — et
délivre un jeton immédiatement.

    https://www.developer.saxo/accounts/sim/signup

⚠️ Le jeton du portail dure 24 heures. Suffisant pour un backtest ou une
session de test ; pour un bot qui tourne en continu il faudra enregistrer une
application et implémenter le flux OAuth (clé + secret). Le code lève une
erreur explicite quand le jeton a expiré, plutôt que d'échouer en silence.

Doc : https://www.developer.saxo/openapi/referencedocs
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


class SaxoError(BrokerError):
    """Erreur renvoyée par l'OpenAPI de Saxo."""


class SaxoTokenExpiredError(SaxoError):
    """Le jeton n'est plus valable — il faut en régénérer un.

    Le jeton du portail développeur expire au bout de 24 h. Sans message
    dédié, l'erreur ressemblerait à une panne alors qu'il suffit d'aller en
    chercher un nouveau.
    """


# Saxo exprime les intervalles en MINUTES. On garde les codes du projet
# (hérités d'OANDA) et on traduit ici, pour ne pas imposer le vocabulaire
# d'un courtier au reste du code.
GRANULARITY_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D": 1440,
}


class SaxoClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.saxo_access_token:
            raise SaxoError(
                "SAXO_ACCESS_TOKEN manquant. Crée un compte sur "
                "https://www.developer.saxo/accounts/sim/signup, récupère le "
                "jeton 24 h, et renseigne-le dans backend/.env."
            )
        # Résolus une fois à la première requête : Saxo identifie le compte
        # par des clés opaques, pas par le numéro affiché dans l'interface.
        self._account_key: str | None = None
        self._client_key: str | None = None
        self._uic_cache: dict[str, int] = {}

    @property
    def _base_url(self) -> str:
        if self.settings.saxo_environment == "live":
            return "https://gateway.saxobank.com/openapi"
        return "https://gateway.saxobank.com/sim/openapi"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.saxo_access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method, f"{self._base_url}{path}", headers=self._headers(),
                params=params, json=json,
            )
        if resp.status_code == 401:
            raise SaxoTokenExpiredError(
                "Jeton Saxo refusé (401). Les jetons du portail développeur "
                "expirent après 24 h : régénère-le sur developer.saxo et "
                "remplace SAXO_ACCESS_TOKEN dans backend/.env."
            )
        if resp.status_code not in (200, 201, 202):
            raise SaxoError(self._message_erreur(resp, method, path))
        return resp.json() if resp.content else {}

    @staticmethod
    def _message_erreur(resp, method: str, path: str) -> str:
        """Message lisible plutôt que le corps brut de la réponse.

        Un chemin inexistant renvoie une page d'erreur HTML du serveur web,
        pas une réponse d'API : quarante lignes de balises dans lesquelles
        l'information utile — le chemin est faux — est noyée.
        """
        corps = resp.text or ""
        est_html = corps.lstrip().lower().startswith(("<!doctype", "<html"))

        if resp.status_code == 404 and est_html:
            return (
                f"404 sur {method} {path} : ce chemin n'existe pas côté Saxo. "
                "L'authentification n'est pas en cause — le serveur a répondu "
                "une page d'erreur web, pas une réponse d'API. Vérifie la "
                "version de l'endpoint dans la documentation : Saxo retire "
                "les anciennes (la V1 des graphiques a été remplacée par la V3)."
            )
        if est_html:
            return (
                f"{resp.status_code} sur {method} {path} : réponse HTML au lieu "
                "de JSON, le chemin ou la passerelle est probablement en cause."
            )
        return f"Saxo a renvoyé {resp.status_code} sur {method} {path} : {corps[:400]}"

    async def _ensure_account(self) -> tuple[str, str]:
        """Résout les clés de compte, une seule fois."""
        if self._account_key and self._client_key:
            return self._account_key, self._client_key

        me = await self._request("GET", "/port/v1/accounts/me")
        accounts = me.get("Data", [])
        if not accounts:
            raise SaxoError("Aucun compte accessible avec ce jeton.")
        account = accounts[0]
        self._account_key = account["AccountKey"]
        self._client_key = account["ClientKey"]
        return self._account_key, self._client_key

    async def _resolve_uic(self, instrument: str) -> int:
        """Traduit un nom d'instrument en UIC, l'identifiant interne de Saxo.

        Saxo ne travaille pas avec des noms mais avec des entiers. On accepte
        le format du projet (EUR_USD) et le format Saxo (EURUSD).
        """
        if instrument in self._uic_cache:
            return self._uic_cache[instrument]

        symbol = instrument.replace("_", "")
        data = await self._request(
            "GET", "/ref/v1/instruments",
            params={"Keywords": symbol, "AssetTypes": "FxSpot", "$top": 20},
        )
        for item in data.get("Data", []):
            if item.get("Symbol", "").replace("/", "").upper() == symbol.upper():
                uic = int(item["Identifier"])
                self._uic_cache[instrument] = uic
                return uic
        raise SaxoError(f"Instrument introuvable chez Saxo : {instrument}")

    # ---- Interface Broker ------------------------------------------------

    async def list_instruments(self) -> list[str]:
        data = await self._request(
            "GET", "/ref/v1/instruments",
            params={"AssetTypes": "FxSpot", "$top": 200},
        )
        names = []
        for item in data.get("Data", []):
            symbol = item.get("Symbol", "").replace("/", "")
            if len(symbol) == 6:
                names.append(f"{symbol[:3]}_{symbol[3:]}")
        return names

    async def get_candles(
        self, instrument: str, granularity: str = "M5", count: int = 50
    ) -> list[Candle]:
        minutes = GRANULARITY_MINUTES.get(granularity)
        if minutes is None:
            raise SaxoError(f"Intervalle non supporté : {granularity}")

        uic = await self._resolve_uic(instrument)
        # /chart/v3/charts : la V1 est dépréciée et renvoie 404 (une page
        # d'erreur HTML, pas une réponse d'API — le chemin n'existe plus).
        #
        # Uic, AssetType et Horizon sont les paramètres requis. `Mode` ne
        # s'emploie qu'avec `Time` pour cadrer une fenêtre précise : sans lui,
        # l'API renvoie les bougies les plus récentes, ce qu'on veut.
        data = await self._request(
            "GET", "/chart/v3/charts",
            params={
                "Uic": uic,
                "AssetType": "FxSpot",
                "Horizon": minutes,
                "Count": min(count, 1200),  # plafond de l'API
            },
        )
        brutes = data.get("Data", [])
        candles = []
        for c in brutes:
            # Saxo renvoie OpenBid/OpenAsk sur certains comptes et Open sur
            # d'autres : on prend le médian quand les deux côtés existent.
            if "OpenBid" in c and "OpenAsk" in c:
                candles.append(
                    Candle(
                        open=(c["OpenBid"] + c["OpenAsk"]) / 2,
                        high=(c["HighBid"] + c["HighAsk"]) / 2,
                        low=(c["LowBid"] + c["LowAsk"]) / 2,
                        close=(c["CloseBid"] + c["CloseAsk"]) / 2,
                    )
                )
            elif "Open" in c:
                candles.append(
                    Candle(open=c["Open"], high=c["High"], low=c["Low"], close=c["Close"])
                )

        # Des bougies reçues mais aucune comprise : le format a changé, ou il
        # diffère selon le compte. Se taire ici donnerait « aucun trade » sans
        # la moindre piste — on dit plutôt ce qu'on a réellement reçu.
        if brutes and not candles:
            raise SaxoError(
                f"{len(brutes)} bougies reçues pour {instrument} mais aucune "
                f"exploitable : champs inattendus {sorted(brutes[0].keys())}. "
                "Le format de réponse de Saxo diffère de celui attendu."
            )
        return candles

    async def get_quote(self, instrument: str) -> Quote:
        uic = await self._resolve_uic(instrument)
        _, client_key = await self._ensure_account()
        data = await self._request(
            "GET", "/trade/v1/infoprices",
            params={
                "Uic": uic, "AssetType": "FxSpot", "ClientKey": client_key,
                "Amount": 100000, "FieldGroups": "Quote",
            },
        )
        quote = data.get("Quote", {})
        bid, ask = quote.get("Bid"), quote.get("Ask")
        if bid is None or ask is None:
            raise SaxoError(f"Aucun prix disponible pour {instrument}.")
        # Saxo signale un marché fermé par un état de marché non négociable.
        tradeable = quote.get("MarketState", "Open") not in ("Closed", "Suspended")
        return Quote(bid=float(bid), ask=float(ask), tradeable=tradeable)

    async def get_account_summary(self) -> AccountSummary:
        account_key, client_key = await self._ensure_account()
        data = await self._request(
            "GET", "/port/v1/balances",
            params={"AccountKey": account_key, "ClientKey": client_key},
        )
        return AccountSummary(
            balance=float(data.get("TotalValue", 0)),
            currency=data.get("Currency", ""),
        )

    async def place_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        uic = await self._resolve_uic(instrument)
        account_key, _ = await self._ensure_account()
        buy = units > 0

        # Saxo attache stop-loss et take-profit en ordres « liés » à l'ordre
        # principal, de sens opposé. C'est le courtier qui les porte : une
        # position ne reste jamais sans limite de perte si le bot s'arrête.
        body = {
            "Uic": uic,
            "AssetType": "FxSpot",
            "Amount": abs(units),
            "BuySell": "Buy" if buy else "Sell",
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "AccountKey": account_key,
            "Orders": [
                {
                    "Uic": uic,
                    "AssetType": "FxSpot",
                    "Amount": abs(units),
                    "BuySell": "Sell" if buy else "Buy",
                    "OrderType": "Stop",
                    "OrderPrice": round(stop_loss_price, 5),
                    "OrderDuration": {"DurationType": "GoodTillCancel"},
                    "AccountKey": account_key,
                },
                {
                    "Uic": uic,
                    "AssetType": "FxSpot",
                    "Amount": abs(units),
                    "BuySell": "Sell" if buy else "Buy",
                    "OrderType": "Limit",
                    "OrderPrice": round(take_profit_price, 5),
                    "OrderDuration": {"DurationType": "GoodTillCancel"},
                    "AccountKey": account_key,
                },
            ],
        }
        result = await self._request("POST", "/trade/v2/orders", json=body)
        order_id = result.get("OrderId")
        if not order_id:
            raise SaxoError(f"Ordre envoyé mais aucun OrderId renvoyé : {result}")
        return str(order_id)

    async def get_trade_status(self, trade_id: str) -> TradeStatus:
        """État d'une position.

        Saxo raisonne en positions, pas en « trades » comme OANDA. Tant que
        la position figure parmi les positions ouvertes, le trade est en
        cours ; sinon on va chercher son résultat dans les positions closes.
        """
        _, client_key = await self._ensure_account()

        open_positions = await self._request(
            "GET", "/port/v1/positions/me", params={"FieldGroups": "PositionBase,PositionView"}
        )
        for pos in open_positions.get("Data", []):
            base = pos.get("PositionBase", {})
            if str(base.get("SourceOrderId")) == str(trade_id):
                return TradeStatus(closed=False)

        closed = await self._request(
            "GET", "/port/v1/closedpositions/me",
            params={"FieldGroups": "ClosedPosition", "$top": 100},
        )
        for pos in closed.get("Data", []):
            detail = pos.get("ClosedPosition", {})
            if str(detail.get("OpeningPositionId")) == str(trade_id) or str(
                detail.get("SourceOrderId")
            ) == str(trade_id):
                return TradeStatus(
                    closed=True,
                    market_pl=float(detail.get("ProfitLossOnTrade", 0)),
                    # Saxo ne renvoie pas toujours le financement séparément
                    # sur une position close ; absent, il vaut 0 plutôt qu'une
                    # valeur inventée.
                    financing=float(detail.get("Financing") or 0),
                )

        # Ni ouverte ni retrouvée close : on considère qu'elle est encore en
        # cours plutôt que d'inventer un résultat.
        return TradeStatus(closed=False)

    async def list_open_trades(self) -> list[OpenTrade]:
        data = await self._request(
            "GET", "/port/v1/positions/me",
            params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
        )
        trades = []
        for pos in data.get("Data", []):
            base = pos.get("PositionBase", {})
            view = pos.get("PositionView", {})
            display = pos.get("DisplayAndFormat", {})
            trades.append(
                OpenTrade(
                    trade_id=str(base.get("SourceOrderId") or pos.get("PositionId", "")),
                    instrument=display.get("Symbol", ""),
                    units=float(base.get("Amount", 0)),
                    price=float(base.get("OpenPrice", 0)),
                    unrealized_pl=float(view.get("ProfitLossOnTrade", 0)),
                )
            )
        return trades
