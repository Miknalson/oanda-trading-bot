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
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

from .analysis import ATR_STOP_MULTIPLIER
from .broker import Candle
from .indicators import atr, trend_direction

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

    from .broker_factory import make_broker

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
