"""Scanner de volatilité — Phase 1 (lecture seule).

Pour chaque instrument tradable du compte, on récupère les dernières
bougies et on calcule un score de volatilité simple : l'amplitude
(high - low) sur la période, exprimée en % du prix bas.

C'est un point de départ ; on pourra affiner avec un vrai ATR (Average
True Range) ou un calcul de volatilité annualisée plus tard.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .broker import Broker, BrokerError


@dataclass
class VolatilityResult:
    instrument: str
    volatility_pct: float
    last_price: float
    high: float
    low: float


class VolatilityScanner:
    def __init__(self, client: Broker, cache_ttl_seconds: int = 30) -> None:
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: list[VolatilityResult] | None = None
        self._cache_timestamp: float = 0.0

    async def _compute_volatility(
        self, instrument: str, granularity: str, count: int
    ) -> VolatilityResult | None:
        try:
            candles = await self.client.get_candles(instrument, granularity, count)
        except BrokerError:
            # Un instrument indisponible ne doit pas faire planter tout le scan.
            return None

        if not candles:
            return None

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        if not highs or not lows:
            return None

        high, low = max(highs), min(lows)
        last_price = candles[-1].close
        if low <= 0:
            return None

        volatility_pct = (high - low) / low * 100
        return VolatilityResult(
            instrument=instrument,
            volatility_pct=round(volatility_pct, 4),
            last_price=last_price,
            high=high,
            low=low,
        )

    async def scan(
        self,
        limit: int = 10,
        granularity: str = "M5",
        count: int = 50,
        instruments: list[str] | None = None,
        force_refresh: bool = False,
    ) -> list[VolatilityResult]:
        now = time.time()
        if (
            not force_refresh
            and self._cache is not None
            and (now - self._cache_timestamp) < self.cache_ttl_seconds
        ):
            return self._cache[:limit]

        if instruments is None:
            instruments = await self.client.list_instruments()

        tasks = [self._compute_volatility(name, granularity, count) for name in instruments]
        results = await asyncio.gather(*tasks)
        ranked = sorted(
            (r for r in results if r is not None),
            key=lambda r: r.volatility_pct,
            reverse=True,
        )

        self._cache = ranked
        self._cache_timestamp = now
        return ranked[:limit]
