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

Deux pièges de lecture, que ce module refuse de laisser passer :

- **« aucun trade » n'est pas une information.** Ça peut vouloir dire « pas
  assez d'historique », « aucune tendance détectée » ou « chaque entrée
  refusée car le spread était trop large » — trois conclusions opposées. Le
  résultat compte donc les bougies écartées et leur motif (`no_trade_reason`).
- **un petit échantillon ne tranche rien.** Avec 65 trades, un taux de
  réussite 7 points sous le seuil d'équilibre arrive par simple malchance à
  peu près une fois sur sept. Le résultat calcule donc la probabilité de ce
  tirage (`p_value`) et refuse de dire « perdant » quand elle est élevée
  (`verdict` = NON CONCLUANT).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from dataclasses import dataclass, field

from .analysis import ATR_STOP_MULTIPLIER
from .broker import Candle
from .indicators import atr, trend_direction

# Périodes des indicateurs — doivent rester alignées sur indicators.py.
FAST_PERIOD, SLOW_PERIOD, ATR_PERIOD = 20, 50, 14

# Plafond du spread, en fraction de la distance du stop : au-delà le trade est
# refusé. Doit rester aligné sur MAX_SPREAD_RATIO dans config.py, sinon le
# backtest ne mesure pas la stratégie qui tradera réellement.
MAX_SPREAD_RATIO = 0.15

# Bougies nécessaires avant le premier signal possible (amorçage des
# indicateurs). Exposé pour que « pas assez d'historique » puisse se chiffrer.
WARMUP = max(SLOW_PERIOD, ATR_PERIOD) + 1

# Seuil de significativité : au-dessus, on ne conclut pas.
SIGNIFICANCE = 0.05

# Durée d'une bougie en heures, pour estimer le nombre de nuits traversées.
GRANULARITY_HOURS = {
    "M1": 1 / 60, "M5": 5 / 60, "M15": 0.25, "M30": 0.5,
    "H1": 1.0, "H4": 4.0, "D": 24.0,
}


def binomial_tail_p(n: int, k: int, p: float) -> float:
    """P(X <= k) pour X ~ Binomiale(n, p), sans dépendance externe.

    Sert à répondre à la seule question qui permette de conclure d'un
    backtest : « ce résultat pourrait-il être de la malchance ? ». Un verdict
    rendu sans elle est un verdict rendu sur du bruit.

    Le calcul passe par les logarithmes et non par `math.comb`, qui renvoie
    un entier exact : à quelques milliers de trades ce coefficient dépasse
    largement la capacité d'un flottant et le calcul direct lève
    OverflowError. En logarithmes, les termes négligeables s'annulent
    proprement au lieu de déborder.
    """
    if n <= 0:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    k = min(max(k, 0), n)
    if p <= 0.0:
        return 1.0  # X vaut toujours 0, donc X <= k est certain
    if p >= 1.0:
        return 1.0 if k >= n else 0.0

    log_p, log_q = math.log(p), math.log1p(-p)
    log_n_fact = math.lgamma(n + 1)
    total = 0.0
    for i in range(k + 1):
        log_terme = (
            log_n_fact
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * log_p
            + (n - i) * log_q
        )
        total += math.exp(log_terme)
    return min(total, 1.0)


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
    max_spread_ratio: float = MAX_SPREAD_RATIO

    # Pourquoi des bougies n'ont pas donné de trade. Sans ce décompte,
    # « aucun trade » se lit comme « marché calme » alors que la cause est
    # souvent l'inverse exactement : un marché trop cher pour y entrer.
    bars_examined: int = 0
    skipped_no_signal: int = 0
    skipped_spread_too_wide: int = 0
    insufficient_candles: bool = False

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

    def no_trade_reason(self) -> str:
        """Pourquoi aucun trade n'a été ouvert — jamais « on ne sait pas ».

        « Aucun trade » sans motif est la sortie la plus trompeuse d'un
        backtest : elle ressemble à un marché calme, alors qu'un spread trop
        large (marché trop cher) produit exactement la même ligne vide.
        """
        if self.insufficient_candles:
            return (
                f"pas assez d'historique — {self.candles} bougies reçues, il en "
                f"faut plus de {WARMUP + 1} pour amorcer les indicateurs "
                f"(SMA {SLOW_PERIOD}, ATR {ATR_PERIOD})"
            )
        if self.bars_examined == 0:
            return "aucune bougie examinée"

        motifs = []
        if self.skipped_spread_too_wide:
            motifs.append(
                f"{self.skipped_spread_too_wide} entrées refusées car le spread "
                f"({self.spread:.5f}) dépassait {self.max_spread_ratio:.0%} de la "
                f"distance du stop — le marché n'était pas calme, il était trop cher"
            )
        if self.skipped_no_signal:
            motifs.append(
                f"{self.skipped_no_signal} bougies sans signal de tendance"
            )
        if not motifs:
            return f"{self.bars_examined} bougies examinées, cause indéterminée"
        return f"sur {self.bars_examined} bougies examinées : " + " ; ".join(motifs)

    @property
    def p_value(self) -> float:
        """Probabilité d'obtenir un taux de réussite au moins aussi extrême
        que celui mesuré, si la stratégie était en réalité exactement à
        l'équilibre.

        Test unilatéral, dans le sens du résultat observé. Au-dessus de 5 %,
        l'échantillon ne permet pas de conclure : ce tirage-là arriverait par
        pure malchance (ou pure chance) assez souvent pour qu'on n'en tire
        rien.
        """
        n = len(self.closed)
        if n == 0:
            return 1.0
        seuil = self.breakeven_win_rate
        if self.wins <= n * seuil:
            return binomial_tail_p(n, self.wins, seuil)
        return 1.0 - binomial_tail_p(n, self.wins - 1, seuil)

    @property
    def is_conclusive(self) -> bool:
        """L'échantillon suffit-il à trancher ?"""
        return bool(self.closed) and self.p_value < SIGNIFICANCE

    @property
    def verdict(self) -> str:
        if not self.closed:
            return "AUCUN TRADE"
        if not self.is_conclusive:
            return "NON CONCLUANT"
        return "RENTABLE" if self.expectancy > 0 else "PERDANT"

    def trades_needed(self, *, maximum: int = 4000) -> int | None:
        """Combien de trades faudrait-il, au taux observé, pour conclure ?

        Estimation : on suppose le taux de réussite stable et on cherche par
        dichotomie le plus petit échantillon qui passerait sous les 5 %.
        Renvoie None si le résultat est déjà concluant, s'il penche du bon
        côté du seuil, ou si même `maximum` trades ne suffiraient pas.
        """
        n0 = len(self.closed)
        if n0 == 0 or self.is_conclusive:
            return None
        taux, seuil = self.win_rate, self.breakeven_win_rate
        if taux >= seuil:
            return None  # rien à prouver du côté perdant

        def concluant(n: int) -> bool:
            return binomial_tail_p(n, round(taux * n), seuil) < SIGNIFICANCE

        if not concluant(maximum):
            return None
        bas, haut = n0, maximum
        while bas < haut:
            milieu = (bas + haut) // 2
            if concluant(milieu):
                haut = milieu
            else:
                bas = milieu + 1
        return bas

    def summary(self) -> str:
        n = len(self.closed)
        if n == 0:
            return (
                f"{self.instrument} {self.granularity} — {self.candles} bougies\n"
                f"  aucun trade : {self.no_trade_reason()}"
            )
        lignes = [
            f"{self.instrument} {self.granularity} — {self.candles} bougies",
            f"  trades          : {n}",
            f"  taux de réussite: {self.win_rate:.1%}  "
            f"(seuil d'équilibre {self.breakeven_win_rate:.1%})",
            f"  P/L net         : {self.net_pl:+.2f}",
            f"  coût du spread  : {self.total_spread_cost:.2f} "
            f"(payé en pertes plus fréquentes)",
            f"  financement     : {self.total_financing:+.2f} "
            f"(déduit du résultat)",
            f"  par trade       : {self.expectancy:+.3f}",
            f"  pire recul      : {self.max_drawdown:.2f}",
            f"  malchance ?     : {self.p_value:.1%} de probabilité d'un tel "
            f"résultat à l'équilibre",
            f"  verdict         : {self.verdict}",
        ]
        if not self.is_conclusive:
            besoin = self.trades_needed()
            if besoin:
                lignes.append(
                    f"  il faudrait ~{besoin} trades au même taux pour trancher"
                )
            else:
                lignes.append("  échantillon trop petit pour trancher")
        return "\n".join(lignes)


def run_backtest(
    candles: list[Candle],
    *,
    instrument: str = "?",
    granularity: str = "?",
    spread: float = 0.00012,
    reward_ratio: float = 1.5,
    risk_amount: float = 2.50,
    financing_rate_annual: float = 0.02,
    max_spread_ratio: float = MAX_SPREAD_RATIO,
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
        max_spread_ratio=max_spread_ratio,
    )

    bar_hours = GRANULARITY_HOURS.get(granularity, 1.0)

    if len(candles) <= WARMUP + 1:
        result.insufficient_candles = True
        return result

    open_trade: BacktestTrade | None = None

    for i in range(WARMUP, len(candles) - 1):
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
        result.bars_examined += 1
        history = candles[: i + 1]
        closes = [c.close for c in history]
        direction = trend_direction(closes, FAST_PERIOD, SLOW_PERIOD)
        atr_value = atr(history, ATR_PERIOD)
        if direction is None or not atr_value or atr_value <= 0:
            result.skipped_no_signal += 1
            continue

        stop_distance = atr_value * ATR_STOP_MULTIPLIER
        if spread / stop_distance > max_spread_ratio:  # même refus qu'en production
            result.skipped_spread_too_wide += 1
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
