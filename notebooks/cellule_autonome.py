"""Backtest autonome — généré par scripts/build_standalone.py.

Ne pas modifier à la main : relancer le script après toute évolution
du code, sinon cette cellule mesurerait autre chose que le vrai bot."""

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx


# --- Configuration minimale (remplace config.py, qui lit un fichier .env) ---
@dataclass(frozen=True)
class Settings:
    saxo_access_token: str = ""
    saxo_environment: str = "sim"
    max_risk_pct: float = 0.02
    max_spread_ratio: float = 0.15


def get_settings() -> Settings:
    return Settings(
        saxo_access_token=os.environ.get("SAXO_ACCESS_TOKEN", ""),
        saxo_environment=os.environ.get("SAXO_ENVIRONMENT", "sim"),
    )


# ====================================================================
# broker.py
# ====================================================================

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

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class BrokerError(RuntimeError):
    """Erreur renvoyée par le courtier (réseau, refus, réponse inattendue)."""


@dataclass(frozen=True)
class Candle:
    """Une bougie, en prix médians."""

    open: float
    high: float
    low: float
    close: float


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

    async def get_candles(
        self, instrument: str, granularity: str, count: int
    ) -> list[Candle]:
        """Les `count` dernières bougies, de la plus ancienne à la plus récente."""
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

# ====================================================================
# indicators.py
# ====================================================================

"""Indicateurs techniques simples, calculés à partir des bougies OANDA.

Aucun de ces indicateurs ne prédit le marché avec certitude — ce sont des
heuristiques classiques de suivi de tendance / mesure de volatilité, pas
des garanties de gain. Voir README pour le disclaimer complet.
"""


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles, period: int = 14) -> float | None:
    """Average True Range — mesure de volatilité, utilisée pour dimensionner
    le stop-loss proportionnellement au mouvement récent du marché.

    Prend des `Candle` (voir broker.py) : aucun format de courtier ici.
    """
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    if len(closes) < period + 1:
        return None

    true_ranges = [
        true_range(highs[i], lows[i], closes[i - 1]) for i in range(1, len(closes))
    ]
    return sum(true_ranges[-period:]) / period


def trend_direction(closes: list[float], fast_period: int = 20, slow_period: int = 50) -> str | None:
    """Retourne "buy", "sell", ou None si pas assez de données.

    Heuristique de suivi de tendance classique : moyenne mobile rapide
    au-dessus de la lente => tendance haussière, et inversement.
    """
    fast = sma(closes, fast_period)
    slow = sma(closes, slow_period) if len(closes) >= slow_period else sma(closes, len(closes))
    if fast is None or slow is None:
        return None
    if fast == slow:
        return None
    return "buy" if fast > slow else "sell"

# ====================================================================
# analysis.py
# ====================================================================

"""Moteur de suggestion de trade — Phase 3.

Combine tendance (moyennes mobiles), volatilité (ATR) et gestion du
risque pour proposer : direction, stop-loss, take-profit, taille de
position. AUCUNE garantie de résultat — voir le README pour le
disclaimer complet sur les limites de ces heuristiques.
"""

from dataclasses import dataclass


# Multiplicateur appliqué à l'ATR pour fixer la distance du stop-loss.
# 1.5x l'ATR est une valeur de départ raisonnable : assez large pour ne
# pas se faire sortir par le bruit normal du marché, assez serré pour
# rester cohérent avec la volatilité réelle de l'instrument.
ATR_STOP_MULTIPLIER = 1.5


class SpreadTooWideError(RuntimeError):
    """Le spread mange une part excessive du risque : trade refusé.

    Le spread est fixe alors que la distance du stop-loss suit la volatilité :
    sur un intervalle court le stop est serré, donc le spread représente une
    fraction énorme du risque et l'espérance de gain devient négative. Mieux
    vaut ne pas trader que trader à perte structurelle.
    """


@dataclass
class TradeSuggestion:
    instrument: str
    direction: str  # "buy" ou "sell"
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    suggested_units: int
    risk_amount: float
    risk_pct: float
    potential_gain: float
    reward_risk_ratio: float
    rationale: str
    # Coût réel du trade : le spread payé à l'ouverture.
    spread: float = 0.0
    spread_cost: float = 0.0
    spread_pct_of_risk: float = 0.0
    # Ce que le marché doit réellement parcourir, spread inclus. Perdre
    # demande moins de mouvement que gagner — c'est ça, le coût.
    move_to_win: float = 0.0
    move_to_lose: float = 0.0


class TradeAnalyzer:
    def __init__(self, client: Broker) -> None:
        self.client = client

    async def suggest(
        self,
        instrument: str,
        risk_pct: float,
        objective_amount: float | None = None,
        granularity: str = "M15",
        count: int = 100,
        max_risk_pct: float = 0.02,
        reward_ratio: float | None = None,
        max_spread_ratio: float = 0.15,
    ) -> TradeSuggestion:
        """Construit une proposition de trade.

        Deux façons de fixer le take-profit :
        - `reward_ratio` (mode session) : le take-profit vise `ratio x` le
          montant risqué sur CE trade. C'est ce qu'utilise une session, qui
          atteint son objectif en cumulant plusieurs petits trades.
        - `objective_amount` (trade isolé) : le take-profit est placé pour
          rapporter ce montant en une seule fois.
        """
        if risk_pct > max_risk_pct:
            raise ValueError(
                f"risk_pct ({risk_pct:.2%}) dépasse le plafond de sécurité ({max_risk_pct:.2%})"
            )
        if reward_ratio is None and (objective_amount is None or objective_amount <= 0):
            raise ValueError("Fournis soit reward_ratio, soit un objective_amount positif")
        if reward_ratio is not None and reward_ratio <= 0:
            raise ValueError("reward_ratio doit être positif")

        candles = await self.client.get_candles(instrument, granularity, count)
        if len(candles) < 20:
            raise BrokerError(
                f"Pas assez de données ({len(candles)} bougies) pour analyser {instrument}"
            )

        closes = [c.close for c in candles]
        direction = trend_direction(closes)
        atr_value = atr(candles)

        if direction is None or atr_value is None or atr_value <= 0:
            raise BrokerError(
                f"Impossible de déterminer une tendance fiable pour {instrument} "
                "avec les données disponibles."
            )

        stop_distance = atr_value * ATR_STOP_MULTIPLIER

        # Prix réels d'exécution : on achète au `ask`, on vend au `bid`.
        # Utiliser le prix médian des bougies masquerait le spread.
        quote = await self.client.get_quote(instrument)
        if not quote.tradeable:
            raise BrokerError(
                f"{instrument} n'est pas négociable actuellement (marché fermé ?)."
            )

        spread = quote.spread
        spread_ratio = spread / stop_distance if stop_distance > 0 else float("inf")
        if spread_ratio > max_spread_ratio:
            raise SpreadTooWideError(
                f"Spread trop large sur {instrument} : {spread:.5f} pour un stop de "
                f"{stop_distance:.5f}, soit {spread_ratio:.0%} du risque "
                f"(plafond {max_spread_ratio:.0%}). Passe à un intervalle plus long "
                f"(H1/H4) ou choisis un instrument moins cher."
            )

        entry_price = quote.ask if direction == "buy" else quote.bid

        account = await self.client.get_account_summary()
        balance = account.balance
        if balance <= 0:
            raise BrokerError("Solde de compte introuvable ou nul.")

        sizing = compute_position_size(balance, risk_pct, stop_distance)
        if sizing.units <= 0:
            raise BrokerError(
                "Le montant à risquer est trop faible pour ouvrir une position "
                "(taille calculée = 0 unité). Augmente risk_pct ou ton solde."
            )

        if reward_ratio is not None:
            # Mode session : chaque trade vise `ratio x` ce qu'il risque.
            take_profit_distance = stop_distance * reward_ratio
        else:
            # Trade isolé : le take-profit doit rapporter tout l'objectif.
            take_profit_distance = objective_amount / sizing.units
        reward_risk_ratio = take_profit_distance / stop_distance
        expected_gain = take_profit_distance * sizing.units

        # Stop et objectif placés depuis le prix d'exécution réel : la perte
        # en euros si le stop est touché vaut donc exactement `risk_amount`.
        if direction == "buy":
            stop_loss_price = entry_price - stop_distance
            take_profit_price = entry_price + take_profit_distance
            units = sizing.units
        else:
            stop_loss_price = entry_price + stop_distance
            take_profit_price = entry_price - take_profit_distance
            units = -sizing.units

        # Le spread se paie en mouvement de marché : pour sortir en gain il
        # faut parcourir la distance de l'objectif PLUS le spread, alors
        # qu'une perte survient après le spread EN MOINS.
        spread_cost = spread * sizing.units
        move_to_win = take_profit_distance + spread
        move_to_lose = max(stop_distance - spread, 0.0)

        rationale = (
            f"Tendance {'haussière' if direction == 'buy' else 'baissière'} "
            f"(moyenne mobile rapide {'au-dessus' if direction == 'buy' else 'en-dessous'} "
            f"de la lente). Stop-loss à {ATR_STOP_MULTIPLIER}x l'ATR ({atr_value:.5f}). "
            f"Ratio gain/risque de ce trade : {reward_risk_ratio:.2f}."
        )
        rationale += (
            f" Spread {spread:.5f} = {spread_ratio:.0%} du risque "
            f"({spread_cost:.2f} de frais sur ce trade)."
        )
        if reward_risk_ratio < 1:
            rationale += (
                " ⚠️ Ce ratio est défavorable (tu risques plus que ce que tu vises) — "
                "objectif de gain probablement trop bas par rapport au risque pris."
            )
        if spread_ratio > 0.08:
            rationale += (
                " ⚠️ Les frais pèsent lourd ici : un intervalle plus long "
                "(H1/H4) réduirait fortement leur poids."
            )

        return TradeSuggestion(
            instrument=instrument,
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=round(stop_loss_price, 5),
            take_profit_price=round(take_profit_price, 5),
            suggested_units=units,
            risk_amount=round(sizing.risk_amount, 2),
            risk_pct=risk_pct,
            potential_gain=round(expected_gain, 2),
            reward_risk_ratio=round(reward_risk_ratio, 2),
            rationale=rationale,
            spread=round(spread, 5),
            spread_cost=round(spread_cost, 2),
            spread_pct_of_risk=round(spread_ratio, 4),
            move_to_win=round(move_to_win, 5),
            move_to_lose=round(move_to_lose, 5),
        )

# ====================================================================
# backtest.py
# ====================================================================

"""Backtest de la stratégie sur données historiques.

Objectif : mesurer le taux de réussite RÉEL au lieu de le supposer.

Deux règles de conception non négociables, sans lesquelles un backtest
raconte n'importe quoi :

1. **Même code que le live.** Les signaux viennent de `indicators.py`, les
   mêmes fonctions qu'utilise `analysis.py` en production. Backtester une
   logique différente de celle qui tradera ne prouve rien.

2. **Aucun regard vers le futur.** À la bougie `i`, seules les bougies
   `0..i` sont visibles, et l'entrée se fait à l'ouverture de `i+1` — on ne
   peut pas décider d'entrer à un prix qu'on n'a pas encore vu. C'est la
   première façon dont un backtest se ment à lui-même.

Comment le spread est facturé (le point le plus facile à rater) :

On achète au `ask` et on revend au `bid`. Le stop et l'objectif se
déclenchent donc sur un prix décalé d'un spread par rapport à l'entrée. En
raisonnant sur les prix médians, cela revient à :

- le stop se déclenche **plus tôt** (à `entrée - distance + spread`) ;
- l'objectif se déclenche **plus tard** (à `entrée + cible + spread`).

Le gain et la perte en euros restent exacts (`+ratio x risque` ou `-risque`) :
le coût du spread ne se déduit pas du résultat de chaque trade, il se paie en
**perdant plus souvent**. C'est pour ça que le seuil d'équilibre reste
`1/(1+ratio)` et que c'est le taux de réussite mesuré qui, lui, baisse.

Le **financement** est le second frais, distinct du spread : OANDA facture des
intérêts sur toute position gardée après 17h à New York. Contrairement au
spread, il se déduit bien du résultat, et il grandit avec la durée de
détention — donc il pèse d'autant plus que l'intervalle est long. Sur H4, un
trade traverse souvent plusieurs nuits.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field


# Périodes des indicateurs — doivent rester alignées sur indicators.py.
FAST_PERIOD, SLOW_PERIOD, ATR_PERIOD = 20, 50, 14

# Durée d'une bougie en heures, pour estimer le nombre de nuits traversées.
GRANULARITY_HOURS = {
    "M1": 1 / 60, "M5": 5 / 60, "M15": 0.25, "M30": 0.5,
    "H1": 1.0, "H4": 4.0, "D": 24.0,
}


@dataclass
class BacktestTrade:
    direction: str
    entry_index: int
    entry_price: float
    stop_loss: float
    take_profit: float
    units: float
    pl_if_win: float = 0.0
    pl_if_loss: float = 0.0
    financing: float = 0.0
    exit_index: int | None = None
    exit_price: float | None = None
    won: bool | None = None
    pl: float = 0.0
    spread_cost: float = 0.0


@dataclass
class BacktestResult:
    instrument: str
    granularity: str
    candles: int
    trades: list[BacktestTrade] = field(default_factory=list)
    spread: float = 0.0
    reward_ratio: float = 1.5
    risk_amount: float = 0.0
    financing_rate_annual: float = 0.0

    @property
    def closed(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.won is not None]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.closed if t.won)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.closed) if self.closed else 0.0

    @property
    def net_pl(self) -> float:
        return sum(t.pl for t in self.closed)

    @property
    def total_spread_cost(self) -> float:
        return sum(t.spread_cost for t in self.closed)

    @property
    def total_financing(self) -> float:
        """Intérêts de détention, comptés en négatif (un coût)."""
        return sum(t.financing for t in self.closed)

    @property
    def expectancy(self) -> float:
        """Gain net moyen par trade — le chiffre qui décide de tout."""
        return self.net_pl / len(self.closed) if self.closed else 0.0

    @property
    def breakeven_win_rate(self) -> float:
        """Taux de réussite minimum pour ne pas perdre d'argent.

        Chaque trade rapporte exactement `+ratio x risque` ou coûte
        `-risque`, donc le seuil vaudrait `1/(1+ratio)`. Le spread n'y
        apparaît pas : son coût se lit dans le taux de réussite MESURÉ,
        qu'il tire vers le bas (voir l'en-tête du module).

        Le financement, lui, se déduit du résultat, donc il relève bien le
        seuil : il faut gagner un peu plus souvent pour l'absorber.
        """
        base = 1 / (self.reward_ratio + 1)
        if not self.closed or self.risk_amount <= 0:
            return base
        cost_per_trade = -self.total_financing / len(self.closed)
        return (1 + cost_per_trade / self.risk_amount) / (self.reward_ratio + 1)

    @property
    def max_drawdown(self) -> float:
        """Pire recul depuis un sommet — ce que tu aurais encaissé en route."""
        equity, peak, worst = 0.0, 0.0, 0.0
        for t in self.closed:
            equity += t.pl
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return worst

    def summary(self) -> str:
        n = len(self.closed)
        if n == 0:
            return f"{self.instrument} {self.granularity}: aucun trade sur la période."
        verdict = "RENTABLE" if self.expectancy > 0 else "PERDANT"
        return (
            f"{self.instrument} {self.granularity} — {self.candles} bougies\n"
            f"  trades          : {n}\n"
            f"  taux de réussite: {self.win_rate:.1%}  "
            f"(seuil d'équilibre {self.breakeven_win_rate:.1%})\n"
            f"  P/L net         : {self.net_pl:+.2f}\n"
            f"  coût du spread  : {self.total_spread_cost:.2f} "
            f"(payé en pertes plus fréquentes)\n"
            f"  financement     : {self.total_financing:+.2f} "
            f"(déduit du résultat)\n"
            f"  par trade       : {self.expectancy:+.3f}\n"
            f"  pire recul      : {self.max_drawdown:.2f}\n"
            f"  verdict         : {verdict}"
        )


def run_backtest(
    candles: list[Candle],
    *,
    instrument: str = "?",
    granularity: str = "?",
    spread: float = 0.00012,
    reward_ratio: float = 1.5,
    risk_amount: float = 2.50,
    financing_rate_annual: float = 0.02,
) -> BacktestResult:
    """Rejoue la stratégie bougie par bougie, sans regard vers le futur.

    `financing_rate_annual` : coût annuel de détention en fraction du
    notionnel (2 % par défaut, ordre de grandeur courant sur une paire
    majeure). Mets 0 pour isoler l'effet du seul spread.
    """
    result = BacktestResult(
        instrument=instrument,
        granularity=granularity,
        candles=len(candles),
        spread=spread,
        reward_ratio=reward_ratio,
        risk_amount=risk_amount,
        financing_rate_annual=financing_rate_annual,
    )

    bar_hours = GRANULARITY_HOURS.get(granularity, 1.0)

    warmup = max(SLOW_PERIOD, ATR_PERIOD) + 1
    if len(candles) <= warmup + 1:
        return result

    open_trade: BacktestTrade | None = None

    for i in range(warmup, len(candles) - 1):
        # --- Position ouverte : le stop ou l'objectif est-il touché ? ---
        if open_trade is not None:
            bar = candles[i]
            high, low = bar.high, bar.low

            if open_trade.direction == "buy":
                hit_tp = high >= open_trade.take_profit
                hit_sl = low <= open_trade.stop_loss
            else:
                hit_tp = low <= open_trade.take_profit
                hit_sl = high >= open_trade.stop_loss

            if hit_sl or hit_tp:
                # Quand une même bougie touche les deux, on ne sait pas
                # lequel est arrivé en premier : on retient la PERTE.
                # Supposer le gain gonflerait artificiellement le résultat.
                won = hit_tp and not hit_sl
                open_trade.exit_index = i
                open_trade.exit_price = (
                    open_trade.take_profit if won else open_trade.stop_loss
                )
                open_trade.won = won
                # Coût implicite du spread, pour information : il n'est pas
                # déduit ici, il est déjà payé via des niveaux décalés qui
                # font perdre plus souvent (voir l'en-tête du module).
                open_trade.spread_cost = spread * abs(open_trade.units)

                # Financement : proportionnel au temps de détention. Il se
                # déduit vraiment du résultat, contrairement au spread.
                hours_held = (i - open_trade.entry_index) * bar_hours
                notional = abs(open_trade.units) * open_trade.entry_price
                open_trade.financing = -(
                    notional * financing_rate_annual * hours_held / (365 * 24)
                )

                open_trade.pl = (
                    open_trade.pl_if_win if won else open_trade.pl_if_loss
                ) + open_trade.financing
                open_trade = None
            continue

        # --- Pas de position : chercher un signal sur le passé seulement ---
        history = candles[: i + 1]
        closes = [c.close for c in history]
        direction = trend_direction(closes, FAST_PERIOD, SLOW_PERIOD)
        atr_value = atr(history, ATR_PERIOD)
        if direction is None or not atr_value or atr_value <= 0:
            continue

        stop_distance = atr_value * ATR_STOP_MULTIPLIER
        if spread / stop_distance > 0.15:  # même refus qu'en production
            continue

        # Entrée à l'ouverture de la bougie SUIVANTE (prix médian).
        entry = candles[i + 1].open
        units = risk_amount / stop_distance
        tp_distance = stop_distance * reward_ratio

        # Niveaux de déclenchement exprimés en prix médian, décalés du
        # spread : le stop tombe plus près, l'objectif plus loin.
        if direction == "buy":
            stop_level = entry - stop_distance + spread
            target_level = entry + tp_distance + spread
        else:
            stop_level = entry + stop_distance - spread
            target_level = entry - tp_distance - spread

        open_trade = BacktestTrade(
            direction=direction,
            entry_index=i + 1,
            entry_price=entry,
            stop_loss=stop_level,
            take_profit=target_level,
            units=units,
            pl_if_win=tp_distance * units,
            pl_if_loss=-stop_distance * units,
        )
        result.trades.append(open_trade)

    return result


async def fetch_history(
    client, instrument: str, granularity: str, count: int
) -> list[Candle]:
    """Récupère jusqu'à `count` bougies, les plus récentes.

    Une seule requête, volontairement. Les clients n'exposent pas encore de
    paramètre « antérieur à telle date » : redemander en boucle renverrait
    exactement la même fenêtre, et empiler ces réponses fabriquerait un
    historique fait de doublons. Un backtest sur des données dupliquées a
    l'air de fonctionner tout en ne mesurant rien — mieux vaut un historique
    court et vrai.

    Pour remonter plus loin, il faudra une pagination par date dans chaque
    client, puis lever la limite ici.
    """
    candles = await client.get_candles(instrument, granularity, count)
    if len(candles) < count:
        logging.getLogger(__name__).info(
            "%s %s : %d bougies obtenues sur %d demandées (plafond du courtier).",
            instrument, granularity, len(candles), count,
        )
    return candles


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Backtest de la stratégie")
    parser.add_argument("--instrument", default="EUR_USD")
    parser.add_argument(
        "--granularity", default="H1",
        help="M15, H1, H4... (M1/M5 sont refusés par le filtre de spread)",
    )
    parser.add_argument("--count", type=int, default=5000, help="Nombre de bougies")
    parser.add_argument("--spread", type=float, default=0.00012)
    parser.add_argument("--ratio", type=float, default=1.5)
    parser.add_argument("--risk", type=float, default=2.50)
    parser.add_argument(
        "--financing", type=float, default=0.02,
        help="Coût annuel de détention, en fraction du notionnel (0 pour l'ignorer)",
    )
    args = parser.parse_args()


    client = make_broker()
    candles = await fetch_history(client, args.instrument, args.granularity, args.count)
    result = run_backtest(
        candles,
        instrument=args.instrument,
        granularity=args.granularity,
        spread=args.spread,
        reward_ratio=args.ratio,
        risk_amount=args.risk,
        financing_rate_annual=args.financing,
    )
    print(result.summary())


if __name__ == "__main__":
    asyncio.run(_main())

# ====================================================================
# saxo_client.py
# ====================================================================

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

import httpx



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
            raise SaxoError(f"Saxo a renvoyé {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

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
        data = await self._request(
            "GET", "/chart/v1/charts",
            params={
                "Uic": uic, "AssetType": "FxSpot", "Horizon": minutes,
                "Count": min(count, 1200),  # plafond de l'API
                "Mode": "UpTo",
            },
        )
        candles = []
        for c in data.get("Data", []):
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