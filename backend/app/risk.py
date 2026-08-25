"""Calcul de la taille de position en fonction du risque accepté.

Principe : quel que soit le trade, la perte maximale (si le stop-loss est
touché) ne doit jamais dépasser `risk_pct` du solde du compte.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionSize:
    units: int
    risk_amount: float
    stop_distance: float


def compute_position_size(
    account_balance: float,
    risk_pct: float,
    stop_distance_price: float,
) -> PositionSize:
    """
    account_balance: solde du compte (dans la devise du compte)
    risk_pct: fraction du solde qu'on accepte de perdre si le stop est touché (ex. 0.01 = 1%)
    stop_distance_price: distance en prix entre l'entrée et le stop-loss

    Approximation : suppose que la devise de cotation de l'instrument
    correspond à la devise du compte (vrai pour la plupart des paires
    majeures type EUR_USD sur un compte en USD). Pour les instruments où
    ce n'est pas le cas, le montant réellement risqué peut différer —
    à affiner avec les taux de conversion OANDA si besoin.
    """
    if stop_distance_price <= 0:
        raise ValueError("stop_distance_price doit être positif")
    if risk_pct <= 0:
        raise ValueError("risk_pct doit être positif")

    risk_amount = account_balance * risk_pct
    units = int(risk_amount / stop_distance_price)
    return PositionSize(units=max(units, 0), risk_amount=risk_amount, stop_distance=stop_distance_price)
