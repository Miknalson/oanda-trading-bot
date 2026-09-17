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
import random
from dataclasses import dataclass, field

from .analysis import ATR_STOP_MULTIPLIER
from .broker import BrokerError, Candle
from .filters import CONFIRMATIONS, window_needed
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

# Bougies que les indicateurs regardent réellement : la SMA lente en veut
# SLOW_PERIOD, l'ATR en veut ATR_PERIOD + 1 (il lui faut la clôture
# précédente). Au-delà, rien ne change au calcul.
INDICATOR_WINDOW = max(SLOW_PERIOD, ATR_PERIOD + 1)

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


def bootstrap_p_value(
    resultats: list[float], *, tirages: int = 2000, seed: int = 0
) -> float:
    """Probabilité que l'espérance soit nulle, vu ces résultats.

    Le test binomial suppose que chaque trade rapporte `+ratio x risque` ou
    coûte `-risque` — vrai avec un objectif fixe, faux dès que les gagnants
    courent. Avec des gains de tailles très inégales, le taux de réussite ne
    décide plus de rien : une stratégie à 30 % de réussite peut être très
    rentable si les gagnants sont énormes.

    On rééchantillonne donc les trades avec remise et on regarde à quelle
    fréquence la moyenne change de signe. C'est un bootstrap par centiles,
    unilatéral dans le sens du résultat observé.

    Attention à sa limite : il suppose les trades indépendants et
    interchangeables. Une série de pertes corrélées — un régime de marché
    défavorable qui dure — est sous-estimée par ce test.
    """
    n = len(resultats)
    if n == 0:
        return 1.0
    moyenne = sum(resultats) / n
    rng = random.Random(seed)
    contraires = 0
    for _ in range(tirages):
        echantillon = sum(resultats[rng.randrange(n)] for _ in range(n)) / n
        if (moyenne > 0 and echantillon <= 0) or (moyenne <= 0 and echantillon >= 0):
            contraires += 1
    return contraires / tirages


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
    # Extrême atteint depuis l'entrée, et distance de suivi du stop.
    peak: float = 0.0
    trail_distance: float = 0.0
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
    entry_mode: str = "signal"
    exit_mode: str = "fixed"
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
    # Combien de fois chaque filtre a bloqué une entrée. Sans ce décompte on
    # ne saurait pas lequel fait le travail — ni lequel refuse tout.
    rejected_by: dict[str, int] = field(default_factory=dict)
    filters: tuple[str, ...] = ("trend",)
    # Bougies consommées avant le premier signal possible. Dépend des filtres
    # actifs — un filtre à 200 périodes en exige 200 — donc citer la constante
    # WARMUP dans les messages donnerait un chiffre faux.
    warmup_used: int = WARMUP
    skipped_spread_too_wide: int = 0
    insufficient_candles: bool = False

    @property
    def closed(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.won is not None]

    @property
    def still_open(self) -> int:
        """Trades encore ouverts à la fin de l'historique.

        Ils sont exclus du P/L, et c'est correct : leur résultat n'existe pas
        encore. Mais les taire serait trompeur, surtout en sortie suiveuse où
        une position peut rester ouverte très longtemps — le P/L mesuré
        omettrait alors le trade le plus important de la période.
        """
        return sum(1 for t in self.trades if t.won is None)

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
                f"faut plus de {self.warmup_used + 1} pour amorcer les filtres "
                f"{list(self.filters)}"
            )
        if self.still_open:
            return (
                f"{self.still_open} trade(s) ouvert(s) mais aucun dénoué avant la "
                f"fin de l'historique — fréquent en sortie suiveuse quand le "
                f"marché ne recule jamais assez pour toucher le stop"
            )
        if self.bars_examined == 0:
            return "aucune bougie examinée"

        motifs = []
        for nom, combien in sorted(
            self.rejected_by.items(), key=lambda kv: -kv[1]
        ):
            motifs.append(f"{combien} entrées refusées par le filtre « {nom} »")
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
        """Probabilité d'un résultat au moins aussi extrême, à l'équilibre.

        Deux tests selon le régime de sortie, parce que la question n'est pas
        la même. Avec un objectif fixe, chaque trade rapporte ou coûte un
        montant connu : le taux de réussite décide de tout, et la loi
        binomiale répond exactement. Avec un stop suiveur, les gains sont de
        tailles très inégales et le taux de réussite ne décide plus rien — on
        teste alors directement l'espérance, par bootstrap.

        Appliquer le test binomial à une sortie suiveuse donnerait un chiffre
        qui a l'air d'un résultat et n'en est pas un.
        """
        if self.exit_mode == "trailing":
            return bootstrap_p_value([t.pl for t in self.closed])
        return self._p_value_binomial

    @property
    def _p_value_binomial(self) -> float:
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
        if n0 == 0 or self.is_conclusive or self.exit_mode == "trailing":
            # En sortie suiveuse, projeter à partir du seul taux de réussite
            # n'aurait pas de sens : c'est la taille des gagnants qui décide.
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
            f"  taux de réussite: {self.win_rate:.1%}"
            + (
                "  (seuil sans objet : les gains sont de tailles inégales)"
                if self.exit_mode == "trailing"
                else f"  (seuil d'équilibre {self.breakeven_win_rate:.1%})"
            ),
            f"  P/L net         : {self.net_pl:+.2f}",
            f"  coût du spread  : {self.total_spread_cost:.2f} "
            f"(payé en pertes plus fréquentes)",
            f"  financement     : {self.total_financing:+.2f} "
            f"(déduit du résultat)",
            f"  par trade       : {self.expectancy:+.3f}",
            f"  pire recul      : {self.max_drawdown:.2f}",
            *(
                [f"  ⚠️ encore ouvert : {self.still_open} trade(s) non dénoué(s) "
                 f"en fin d'historique, exclus du P/L"]
                if self.still_open
                else []
            ),
            f"  malchance ?     : {self.p_value:.1%} de probabilité d'un tel "
            f"résultat à l'équilibre"
            + (" (bootstrap)" if self.exit_mode == "trailing" else " (binomial)"),
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
    entry_mode: str = "signal",
    exit_mode: str = "fixed",
    trail_multiplier: float = 2.0,
    filters: tuple[str, ...] = ("trend",),
    seed: int = 0,
) -> BacktestResult:
    """Rejoue la stratégie bougie par bougie, sans regard vers le futur.

    `financing_rate_annual` : coût annuel de détention en fraction du
    notionnel (2 % par défaut, ordre de grandeur courant sur une paire
    majeure). Mets 0 pour isoler l'effet du seul spread.

    `entry_mode` :

    - `"signal"` : le sens du trade vient de `trend_direction` — la stratégie.
    - `"random"` : le sens est tiré à pile ou face, tout le reste identique.

    Le mode aléatoire est l'étalon de mesure, et il manquait. Sans lui, un
    backtest dit seulement « ça perd » ; il ne dit pas si le SIGNAL y est pour
    quelque chose. Comparer les deux sur les MÊMES bougies, avec les mêmes
    stops, le même dimensionnement et les mêmes frais, isole exactement ce
    qu'apporte la logique d'entrée. Si les deux donnent la même chose, le
    signal ne vaut rien — et le régler davantage ne servira à rien.
    """
    if entry_mode not in ("signal", "random"):
        raise ValueError(f"entry_mode inconnu : {entry_mode!r} (signal ou random)")
    if exit_mode not in ("fixed", "trailing"):
        raise ValueError(f"exit_mode inconnu : {exit_mode!r} (fixed ou trailing)")
    if "trend" not in filters:
        raise ValueError(
            "le filtre 'trend' est obligatoire : c'est lui qui donne le SENS "
            "du trade, les autres ne font que le confirmer."
        )

    # La fenêtre doit couvrir le filtre le plus gourmand. L'oublier rendrait un
    # filtre à 200 périodes systématiquement faux avec une fenêtre de 50, et le
    # backtest conclurait « aucun trade » pour une raison sans rapport avec le
    # marché.
    fenetre_requise = max(INDICATOR_WINDOW, window_needed(filters))
    amorcage = fenetre_requise + 1
    tirage = random.Random(seed)
    result = BacktestResult(
        instrument=instrument,
        granularity=granularity,
        candles=len(candles),
        spread=spread,
        reward_ratio=reward_ratio,
        risk_amount=risk_amount,
        financing_rate_annual=financing_rate_annual,
        max_spread_ratio=max_spread_ratio,
        entry_mode=entry_mode,
        exit_mode=exit_mode,
        filters=filters,
        warmup_used=amorcage,
    )

    bar_hours = GRANULARITY_HOURS.get(granularity, 1.0)

    if len(candles) <= amorcage + 1:
        result.insufficient_candles = True
        return result

    open_trade: BacktestTrade | None = None

    for i in range(amorcage, len(candles) - 1):
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

            # En sortie suiveuse il n'y a PAS d'objectif : on ne sort que par
            # le stop, qui remonte derrière le prix.
            if exit_mode == "trailing":
                hit_tp = False

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

                if exit_mode == "trailing":
                    # Le gain n'est plus binaire : il vaut la distance
                    # réellement parcourue jusqu'au stop suiveur. C'est tout
                    # l'intérêt — laisser courir un gagnant au lieu de le
                    # couper à 1,5x, ce qui est la façon classique de tuer une
                    # stratégie de suivi de tendance.
                    sortie = open_trade.exit_price
                    parcours = (
                        sortie - open_trade.entry_price
                        if open_trade.direction == "buy"
                        else open_trade.entry_price - sortie
                    )
                    open_trade.pl = parcours * open_trade.units + open_trade.financing
                    open_trade.won = open_trade.pl > 0
                else:
                    open_trade.pl = (
                        open_trade.pl_if_win if won else open_trade.pl_if_loss
                    ) + open_trade.financing
                open_trade = None
                continue

            if exit_mode == "trailing":
                # Le stop remonte APRÈS le test de déclenchement, jamais avant.
                #
                # L'ordre compte, et l'inverser serait un regard vers le futur
                # déguisé : utiliser le plus haut de la bougie EN COURS pour
                # remonter le stop, puis vérifier si ce nouveau stop a été
                # touché dans cette même bougie, reviendrait à connaître le
                # sommet avant de l'avoir vécu. On teste donc avec le niveau
                # hérité de la bougie précédente, puis on le remonte pour la
                # suivante.
                if open_trade.direction == "buy":
                    open_trade.peak = max(open_trade.peak, high)
                    open_trade.stop_loss = max(
                        open_trade.stop_loss,
                        open_trade.peak - open_trade.trail_distance + spread,
                    )
                else:
                    open_trade.peak = min(open_trade.peak, low)
                    open_trade.stop_loss = min(
                        open_trade.stop_loss,
                        open_trade.peak + open_trade.trail_distance - spread,
                    )
            continue

        # --- Pas de position : chercher un signal sur le passé seulement ---
        result.bars_examined += 1

        # Fenêtre glissante, et non tout l'historique. Les indicateurs ne
        # regardent que leurs dernières bougies (SMA 50, ATR 14) : leur passer
        # l'historique entier à chaque barre donnait exactement le même
        # résultat en temps quadratique — 6000 bougies valaient 36 millions
        # d'opérations par réglage testé, et la pagination venait justement de
        # faire passer l'historique de 1200 à plusieurs milliers.
        # `max(0, ...)` n'est pas décoratif : un indice de départ négatif
        # découperait la fin du tableau, c'est-à-dire des bougies FUTURES.
        # Le regard vers le futur est l'erreur qui rend un backtest
        # flatteur et faux, et elle se glisserait ici sans rien casser.
        debut = max(0, i + 1 - fenetre_requise)
        fenetre = candles[debut : i + 1]
        closes = [c.close for c in fenetre]
        direction = trend_direction(closes, FAST_PERIOD, SLOW_PERIOD)
        atr_value = atr(fenetre, ATR_PERIOD)
        if direction is None or not atr_value or atr_value <= 0:
            result.skipped_no_signal += 1
            continue

        # En mode aléatoire, seule la RÈGLE de direction change : mêmes
        # bougies, même filtre de spread, même dimensionnement, mêmes stops.
        #
        # Les instants d'entrée ne restent pas identiques pour autant, et ils
        # ne le peuvent pas : dès qu'un trade se dénoue différemment, la
        # position se libère à un autre moment et l'entrée suivante se décale.
        # Seule la première entrée est commune. La comparaison reste valide —
        # deux règles de direction jugées sur le même historique avec les
        # mêmes frais — mais ce n'est pas une expérience appariée trade à
        # trade, et il ne faut pas la lire comme telle.
        if entry_mode == "random":
            direction = "buy" if tirage.random() < 0.5 else "sell"

        # Filtres de confirmation : TOUS doivent être d'accord. C'est là que se
        # joue la sélectivité — un signal fréquent devient un signal rare.
        rejete = None
        for nom in filters:
            if nom == "trend":
                continue  # déjà appliqué : c'est lui qui donne la direction
            if not CONFIRMATIONS[nom](fenetre, direction):
                rejete = nom
                break
        if rejete is not None:
            result.rejected_by[rejete] = result.rejected_by.get(rejete, 0) + 1
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

        if exit_mode == "trailing":
            # Objectif repoussé hors d'atteinte : seule la sortie suiveuse
            # décide. Laisser un objectif atteignable annulerait l'expérience.
            target_level = entry + 1e9 if direction == "buy" else entry - 1e9

        open_trade = BacktestTrade(
            direction=direction,
            entry_index=i + 1,
            entry_price=entry,
            stop_loss=stop_level,
            take_profit=target_level,
            units=units,
            peak=entry,
            trail_distance=atr_value * trail_multiplier,
            pl_if_win=tp_distance * units,
            pl_if_loss=-stop_distance * units,
        )
        result.trades.append(open_trade)

    return result


@dataclass
class PooledResult:
    """Résultat agrégé sur plusieurs instruments — une seule mesure.

    Tester dix instruments et retenir le meilleur est la façon la plus
    efficace de se mentir : avec dix cases et aucun avantage réel, on en
    trouve forcément une qui brille. On l'a mesuré — avec huit cases sans
    aucun avantage, 99,6 % des univers en contiennent au moins une positive,
    et quatre en moyenne.

    L'agrégation fait l'inverse. Mettre TOUS les trades de TOUS les
    instruments dans un seul panier multiplie la taille d'échantillon au lieu
    de multiplier les occasions de se tromper. Une seule question, une seule
    réponse : cette famille de stratégies a-t-elle une espérance positive ?

    La ventilation par instrument est conservée, mais pour le diagnostic
    seulement. Ce n'est PAS un menu dans lequel choisir.
    """

    per_instrument: dict[str, BacktestResult] = field(default_factory=dict)

    @property
    def results(self) -> list[BacktestResult]:
        return list(self.per_instrument.values())

    @property
    def trades(self) -> list[BacktestTrade]:
        return [t for r in self.results for t in r.closed]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def net_pl(self) -> float:
        return sum(t.pl for t in self.trades)

    @property
    def expectancy(self) -> float:
        return self.net_pl / len(self.trades) if self.trades else 0.0

    @property
    def breakeven_win_rate(self) -> float:
        """Seuil moyen, pondéré par le nombre de trades de chaque instrument."""
        actifs = [r for r in self.results if r.closed]
        if not actifs:
            return 0.5
        total = sum(len(r.closed) for r in actifs)
        return sum(r.breakeven_win_rate * len(r.closed) for r in actifs) / total

    @property
    def p_value(self) -> float:
        """Significativité de l'AGRÉGAT — la seule qui décide."""
        n = len(self.trades)
        if n == 0:
            return 1.0
        seuil = self.breakeven_win_rate
        if self.wins <= n * seuil:
            return binomial_tail_p(n, self.wins, seuil)
        return 1.0 - binomial_tail_p(n, self.wins - 1, seuil)

    @property
    def verdict(self) -> str:
        if not self.trades:
            return "AUCUN TRADE"
        if self.p_value >= SIGNIFICANCE:
            return "NON CONCLUANT"
        return "RENTABLE" if self.expectancy > 0 else "PERDANT"

    def best_instrument(self) -> tuple[str, BacktestResult] | None:
        """Le meilleur instrument — pour montrer le piège, pas pour le suivre."""
        actifs = [(nom, r) for nom, r in self.per_instrument.items() if r.closed]
        if not actifs:
            return None
        return max(actifs, key=lambda kv: kv[1].expectancy)

    def best_p_value_adjusted(self) -> float | None:
        """Significativité du meilleur instrument, CORRIGÉE du nombre d'essais.

        Regarder N instruments puis n'en retenir qu'un, c'est faire N tirages
        et ne garder que le plus favorable. La probabilité que le meilleur
        paraisse bon par hasard grandit donc avec N, et la p-value brute ne
        veut plus rien dire telle quelle. Correction de Bonferroni :
        volontairement conservatrice, et surtout facile à expliquer.
        """
        meilleur = self.best_instrument()
        if meilleur is None:
            return None
        testes = sum(1 for r in self.results if r.closed)
        return min(1.0, meilleur[1].p_value * testes)

    def summary(self) -> str:
        n = len(self.trades)
        if n == 0:
            # Ne jamais rendre une ligne vide muette : « aucun trade » sur tous
            # les instruments à la fois ressemble à un marché sans opportunité,
            # alors que la cause est presque toujours la même pour tous — un
            # filtre ou un spread qui refuse tout. Le taire ferait chercher du
            # côté de la stratégie un problème de configuration.
            lignes = ["AGRÉGAT — aucun trade sur aucun des "
                      f"{len(self.per_instrument)} instruments.", ""]
            motifs: dict[str, list[str]] = {}
            for nom, r in sorted(self.per_instrument.items()):
                motifs.setdefault(r.no_trade_reason(), []).append(nom)
            for motif, noms in motifs.items():
                lignes.append(f"  {', '.join(noms)} :")
                lignes.append(f"    {motif}")
            if len(motifs) == 1:
                lignes += [
                    "",
                    "  Un motif identique partout désigne la configuration, pas",
                    "  le marché : filtres trop stricts, spread trop large, ou",
                    "  historique trop court.",
                ]
            return "\n".join(lignes)

        lignes = [
            f"AGRÉGAT — {len(self.per_instrument)} instruments, {n} trades",
            f"  taux de réussite: {self.win_rate:.1%}  "
            f"(seuil d'équilibre {self.breakeven_win_rate:.1%})",
            f"  P/L net         : {self.net_pl:+.2f}",
            f"  par trade       : {self.expectancy:+.3f}",
            f"  malchance ?     : {self.p_value:.2%}",
            f"  VERDICT         : {self.verdict}",
        ]

        meilleur = self.best_instrument()
        if meilleur is not None:
            nom, r = meilleur
            ajustee = self.best_p_value_adjusted()
            lignes += [
                "",
                f"Le meilleur instrument est {nom} ({r.expectancy:+.3f}/trade, "
                f"p brute {r.p_value:.1%}).",
                f"Corrigée du nombre d'essais : p = {ajustee:.1%}"
                + ("  -> rien de démontré." if ajustee >= SIGNIFICANCE else "."),
                "Ce chiffre existe pour montrer le piège, pas pour le suivre :",
                "choisir le meilleur d'une liste, c'est garder le tirage le plus",
                "favorable et appeler ça un résultat.",
            ]
        return "\n".join(lignes)


def run_pooled_backtest(
    histories: dict[str, list[Candle]],
    spreads: dict[str, float],
    **kwargs,
) -> PooledResult:
    """Rejoue la MÊME stratégie sur plusieurs instruments, et agrège.

    Le dimensionnement par le risque (`risk_amount` identique partout) rend
    les trades comparables d'un instrument à l'autre : chacun risque le même
    montant, quelle que soit la paire. Sans ça, agréger des P/L exprimés dans
    des échelles différentes n'aurait aucun sens.
    """
    agrege = PooledResult()
    for nom, bougies in histories.items():
        agrege.per_instrument[nom] = run_backtest(
            bougies, instrument=nom, spread=spreads[nom], **kwargs
        )
    return agrege


def split_history(
    candles: list[Candle], *, validation_fraction: float = 0.3
) -> tuple[list[Candle], list[Candle]]:
    """Coupe l'historique en deux : mise au point, puis validation.

    C'est le garde-fou contre la façon la plus efficace de se mentir avec un
    backtest. Essayer dix stratégies sur les mêmes données et garder la
    meilleure garantit d'en trouver une qui paraît rentable — par pur hasard,
    exactement comme dix pièces lancées dix fois donnent forcément une série
    de faces. Cette stratégie-là perdra en réel.

    La coupe est CHRONOLOGIQUE, jamais aléatoire : mélanger les bougies
    laisserait des morceaux du futur dans la période de mise au point.

    Règle d'usage, et elle ne vaut que si on s'y tient : on cherche, on règle
    et on compare autant qu'on veut sur la première période. On n'exécute la
    seconde qu'UNE SEULE FOIS, à la toute fin. Chaque essai supplémentaire
    sur la période de validation la transforme en période de mise au point, et
    le garde-fou disparaît sans prévenir.
    """
    if not 0 < validation_fraction < 1:
        raise ValueError(
            f"validation_fraction doit être entre 0 et 1 (reçu {validation_fraction})"
        )
    coupe = int(len(candles) * (1 - validation_fraction))
    return candles[:coupe], candles[coupe:]


async def fetch_history(
    client, instrument: str, granularity: str, count: int
) -> list[Candle]:
    """Remonte jusqu'à `count` bougies, les plus récentes d'abord.

    Les courtiers plafonnent chaque requête (1200 points chez Saxo, 5000 chez
    OANDA). Pour aller au-delà, on remonte par pages : on demande d'abord la
    fenêtre la plus récente, puis « ce qui précède la plus ancienne bougie
    reçue », et ainsi de suite.

    Le danger de cette boucle est précis. Si le courtier **ignore** le
    paramètre de bornage, il renvoie à chaque tour exactement la même fenêtre.
    Empiler ces réponses fabriquerait un historique fait de doublons : un
    backtest dessus a l'air de fonctionner tout en ne mesurant rien, et c'est
    invisible dans les chiffres qu'il sort. La boucle vérifie donc à chaque
    page que le courtier a réellement reculé, et s'arrête en erreur sinon —
    plutôt qu'un historique court mais vrai devenu long et faux.

    Sans horodatage sur les bougies, il n'y a rien à quoi se borner : on
    renvoie alors la seule première page, en le disant.
    """
    log = logging.getLogger(__name__)
    taille = min(getattr(client, "max_candles_per_request", 500) or 500, count)
    taille = max(taille, 1)

    page = await client.get_candles(instrument, granularity, taille)
    if not page:
        return []

    if not all(c.time for c in page):
        log.info(
            "%s %s : le courtier n'horodate pas ses bougies — pagination "
            "impossible, %d bougies au total.",
            instrument, granularity, len(page),
        )
        return page

    connues: dict[str, Candle] = {c.time: c for c in page}
    # Garde-fou contre une boucle sans fin si un courtier se comporte de façon
    # inattendue : il ne faut jamais plus d'une page par tranche de `taille`.
    pages_max = count // taille + 2

    for _ in range(pages_max):
        if len(connues) >= count:
            break

        plus_recente_avant = page[-1].time
        plus_ancienne = min(connues)
        page = await client.get_candles(
            instrument, granularity, taille, before=plus_ancienne
        )
        if not page:
            break  # historique épuisé : c'est une fin normale

        # Si la bougie la PLUS RÉCENTE de la nouvelle page est celle de la
        # précédente, le courtier n'a pas reculé d'un pouce : le bornage a été
        # ignoré. Ce test ne peut pas se déclencher à tort sur un historique
        # épuisé — dans ce cas la fenêtre renvoyée serait plus ancienne, pas
        # identique.
        if page[-1].time == plus_recente_avant:
            raise BrokerError(
                f"{instrument} {granularity} : le courtier a renvoyé la même "
                f"fenêtre (jusqu'à {page[-1].time}) alors qu'on demandait ce "
                f"qui précède {plus_ancienne}. Le bornage par date n'est pas "
                f"pris en compte. Arrêt : empiler ces réponses fabriquerait un "
                f"historique en doublons, sur lequel un backtest paraît "
                f"fonctionner tout en ne mesurant rien."
            )

        nouvelles = {c.time: c for c in page if c.time and c.time < plus_ancienne}
        if not nouvelles:
            break  # plus rien d'antérieur : début de l'historique disponible
        connues.update(nouvelles)

    ordonnees = [connues[t] for t in sorted(connues)]
    if len(ordonnees) < count:
        log.info(
            "%s %s : %d bougies obtenues sur %d demandées (début de "
            "l'historique disponible).",
            instrument, granularity, len(ordonnees), count,
        )
    return ordonnees[-count:]


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
