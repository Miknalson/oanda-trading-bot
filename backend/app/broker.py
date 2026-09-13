"""Interface neutre de courtier.

Le projet a d'abord été écrit directement contre l'API d'OANDA : le format
JSON d'OANDA (`{"mid": {"o", "h", "l", "c"}}`, `realizedPL`, `financing`...)
se retrouvait jusque dans la logique de stratégie. Quand il a fallu changer
de courtier — l'entité européenne d'OANDA ne propose pas l'API REST — ce
couplage a transformé un changement de fournisseur en refonte.

Ce module fixe le contrat : des types neutres et un protocole que chaque
courtier implémente. La stratégie, la gestion du risque, les sessions et le
backtest ne connaissent que ça. Ajouter un courtier = écrire une classe,
sans toucher au reste.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """Erreur renvoyée par le courtier (réseau, refus, réponse inattendue)."""


@dataclass(frozen=True)
class Candle:
    """Une bougie, en prix médians.

    `time` est l'horodatage ISO 8601 du début de la bougie, tel que le
    courtier le renvoie. Il est facultatif — une bougie reste exploitable
    sans — mais c'est lui qui rend la pagination possible : pour demander
    « ce qui précède », il faut savoir où l'on s'est arrêté. Sans date, un
    historique ne peut pas être remonté au-delà d'une requête.
    """

    open: float
    high: float
    low: float
    close: float
    time: str = ""


@dataclass(frozen=True)
class Quote:
    """Prix acheteur/vendeur courants d'un instrument."""

    bid: float
    ask: float
    tradeable: bool = True

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class AccountSummary:
    balance: float
    currency: str = ""


@dataclass(frozen=True)
class TradeStatus:
    """État d'un trade, et son résultat une fois clôturé.

    `market_pl` et `financing` restent distincts : le premier dépend de la
    justesse du trade, le second du temps passé en position. Les confondre
    masquerait le coût de détention.
    """

    closed: bool
    market_pl: float = 0.0
    financing: float = 0.0

    @property
    def net_pl(self) -> float:
        return self.market_pl + self.financing


@dataclass(frozen=True)
class OpenTrade:
    trade_id: str
    instrument: str
    units: float
    price: float
    unrealized_pl: float = 0.0


@runtime_checkable
class Broker(Protocol):
    """Ce qu'un courtier doit savoir faire pour que le bot fonctionne."""

    async def list_instruments(self) -> list[str]:
        """Instruments négociables sur le compte."""
        ...

    # Plafond de bougies par requête, propre à chaque courtier. Sert à
    # dimensionner les pages quand on remonte l'historique.
    max_candles_per_request: int

    async def get_candles(
        self, instrument: str, granularity: str, count: int, before: str | None = None
    ) -> list[Candle]:
        """Les `count` dernières bougies, de la plus ancienne à la plus récente.

        `before` : horodatage ISO 8601. Quand il est fourni, seules des
        bougies STRICTEMENT antérieures doivent être renvoyées — c'est ce qui
        permet de remonter l'historique page par page. Un courtier qui
        l'ignore renverrait la même fenêtre indéfiniment ; `fetch_history` le
        détecte et refuse de continuer plutôt que d'empiler des doublons.
        """
        ...

    async def get_quote(self, instrument: str) -> Quote:
        """Prix acheteur/vendeur courants — indispensable pour chiffrer le spread."""
        ...

    async def get_account_summary(self) -> AccountSummary:
        ...

    async def place_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> str:
        """Ordre au marché avec stop-loss ET take-profit attachés.

        Les deux protections doivent être portées par le courtier, pas par
        notre serveur : une position ne doit jamais rester sans limite de
        perte si le bot s'arrête. Renvoie l'identifiant du trade ouvert.
        """
        ...

    async def get_trade_status(self, trade_id: str) -> TradeStatus:
        ...

    async def list_open_trades(self) -> list[OpenTrade]:
        ...
