"""Moteur de suggestion de trade — Phase 3.

Combine tendance (moyennes mobiles), volatilité (ATR) et gestion du
risque pour proposer : direction, stop-loss, take-profit, taille de
position. AUCUNE garantie de résultat — voir le README pour le
disclaimer complet sur les limites de ces heuristiques.
"""
from __future__ import annotations

from dataclasses import dataclass

from .broker import Broker, BrokerError
from .indicators import atr, trend_direction
from .risk import compute_position_size

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
        min_trade_units: int = 0,
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

        # Les courtiers imposent une taille minimale (souvent 1 000 unités sur
        # le Forex, parfois 10 000). En dessous, l'ordre est refusé.
        #
        # Le piège est que la seule façon de respecter ce minimum avec un petit
        # compte est de risquer BEAUCOUP plus que le pourcentage demandé : avec
        # 100 € et un minimum de 10 000 unités, une seule perte vaut 12 € — soit
        # 12 % du compte, six fois le plafond de sécurité. Mieux vaut refuser en
        # expliquant que laisser le courtier rejeter l'ordre sans raison lisible,
        # ou pire, ouvrir une position hors de toute limite de risque.
        # Désactivé par défaut (0) : la taille minimale réelle de Saxo n'a pas
        # pu être vérifiée depuis l'environnement où ce code a été écrit, et
        # livrer une contrainte devinée qui REFUSE des trades valides serait
        # pire que le problème qu'elle prétend éviter. À activer via
        # MIN_TRADE_UNITS une fois la valeur de ton courtier connue.
        if min_trade_units > 0 and sizing.units < min_trade_units:
            solde_requis = min_trade_units * stop_distance / risk_pct
            raise BrokerError(
                f"Taille calculée : {sizing.units} unités, sous le minimum du "
                f"courtier ({min_trade_units}). Ton compte est trop petit pour "
                f"risquer {risk_pct:.1%} par trade sur cet instrument : "
                f"respecter le minimum reviendrait à risquer "
                f"{min_trade_units * stop_distance:.2f} de ta devise par trade. "
                f"Il faudrait environ {solde_requis:.0f} de solde à ce niveau de "
                f"risque, ou un instrument dont le stop est plus large."
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
