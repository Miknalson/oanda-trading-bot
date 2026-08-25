"""Moteur de suggestion de trade — Phase 3.

Combine tendance (moyennes mobiles), volatilité (ATR) et gestion du
risque pour proposer : direction, stop-loss, take-profit, taille de
position. AUCUNE garantie de résultat — voir le README pour le
disclaimer complet sur les limites de ces heuristiques.
"""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import atr, trend_direction
from .oanda_client import OandaClient, OandaError
from .risk import compute_position_size

# Multiplicateur appliqué à l'ATR pour fixer la distance du stop-loss.
# 1.5x l'ATR est une valeur de départ raisonnable : assez large pour ne
# pas se faire sortir par le bruit normal du marché, assez serré pour
# rester cohérent avec la volatilité réelle de l'instrument.
ATR_STOP_MULTIPLIER = 1.5


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


class TradeAnalyzer:
    def __init__(self, client: OandaClient) -> None:
        self.client = client

    async def suggest(
        self,
        instrument: str,
        risk_pct: float,
        objective_amount: float,
        granularity: str = "M15",
        count: int = 100,
        max_risk_pct: float = 0.02,
    ) -> TradeSuggestion:
        if risk_pct > max_risk_pct:
            raise ValueError(
                f"risk_pct ({risk_pct:.2%}) dépasse le plafond de sécurité ({max_risk_pct:.2%})"
            )
        if objective_amount <= 0:
            raise ValueError("objective_amount doit être positif")

        candles = await self.client.get_candles(instrument, granularity, count)
        if len(candles) < 20:
            raise OandaError(
                f"Pas assez de données ({len(candles)} bougies) pour analyser {instrument}"
            )

        closes = [float(c["mid"]["c"]) for c in candles if c.get("mid")]
        direction = trend_direction(closes)
        atr_value = atr(candles)

        if direction is None or atr_value is None or atr_value <= 0:
            raise OandaError(
                f"Impossible de déterminer une tendance fiable pour {instrument} "
                "avec les données disponibles."
            )

        entry_price = closes[-1]
        stop_distance = atr_value * ATR_STOP_MULTIPLIER

        account = await self.client.get_account_summary()
        balance = float(account.get("balance", 0))
        if balance <= 0:
            raise OandaError("Solde de compte introuvable ou nul.")

        sizing = compute_position_size(balance, risk_pct, stop_distance)
        if sizing.units <= 0:
            raise OandaError(
                "Le montant à risquer est trop faible pour ouvrir une position "
                "(taille calculée = 0 unité). Augmente risk_pct ou ton solde."
            )

        # Distance de take-profit nécessaire pour atteindre l'objectif de
        # gain fixé, compte tenu de la taille de position calculée.
        take_profit_distance = objective_amount / sizing.units
        reward_risk_ratio = take_profit_distance / stop_distance

        if direction == "buy":
            stop_loss_price = entry_price - stop_distance
            take_profit_price = entry_price + take_profit_distance
            units = sizing.units
        else:
            stop_loss_price = entry_price + stop_distance
            take_profit_price = entry_price - take_profit_distance
            units = -sizing.units

        rationale = (
            f"Tendance {'haussière' if direction == 'buy' else 'baissière'} "
            f"(moyenne mobile rapide {'au-dessus' if direction == 'buy' else 'en-dessous'} "
            f"de la lente). Stop-loss à {ATR_STOP_MULTIPLIER}x l'ATR ({atr_value:.5f}). "
            f"Ratio gain/risque de ce trade : {reward_risk_ratio:.2f}."
        )
        if reward_risk_ratio < 1:
            rationale += (
                " ⚠️ Ce ratio est défavorable (tu risques plus que ce que tu vises) — "
                "objectif de gain probablement trop bas par rapport au risque pris."
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
            potential_gain=round(objective_amount, 2),
            reward_risk_ratio=round(reward_risk_ratio, 2),
            rationale=rationale,
        )
