"""Indicateurs techniques simples, calculés à partir des bougies OANDA.

Aucun de ces indicateurs ne prédit le marché avec certitude — ce sont des
heuristiques classiques de suivi de tendance / mesure de volatilité, pas
des garanties de gain. Voir README pour le disclaimer complet.
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Average True Range — mesure de volatilité, utilisée pour dimensionner
    le stop-loss proportionnellement au mouvement récent du marché."""
    closes = [float(c["mid"]["c"]) for c in candles if c.get("mid")]
    highs = [float(c["mid"]["h"]) for c in candles if c.get("mid")]
    lows = [float(c["mid"]["l"]) for c in candles if c.get("mid")]
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
