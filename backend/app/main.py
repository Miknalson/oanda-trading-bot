"""API FastAPI — Phase 1 : scanner de volatilité en lecture seule.

Aucune route d'exécution d'ordre n'existe dans cette phase. Voir le
README à la racine du repo pour la feuille de route (Phase 2 :
validation manuelle, Phase 3 : exécution semi-automatique avec
garde-fous).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .oanda_client import OandaClient, OandaError
from .scanner import VolatilityScanner

app = FastAPI(
    title="OANDA Trading Bot — API",
    description="Phase 1 : scanner de volatilité (lecture seule, aucune exécution d'ordre).",
    version="0.1.0",
)

# CORS ouvert pour le développement local de la PWA. À restreindre à ton
# domaine une fois déployé.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_scanner: VolatilityScanner | None = None


def get_scanner() -> VolatilityScanner:
    global _scanner
    if _scanner is None:
        settings = get_settings()
        _scanner = VolatilityScanner(
            client=OandaClient(settings), cache_ttl_seconds=settings.scanner_cache_ttl_seconds
        )
    return _scanner


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.oanda_environment,
        "warning": "LIVE — argent réel" if settings.is_live else "practice — compte démo",
    }


@app.get("/api/scanner/top-volatile")
async def top_volatile(
    limit: int = Query(10, ge=1, le=50),
    granularity: str = Query("M5", pattern="^(M1|M5|M15|M30|H1|H4|D)$"),
    count: int = Query(50, ge=5, le=500),
) -> dict:
    try:
        results = await get_scanner().scan(limit=limit, granularity=granularity, count=count)
    except OandaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "granularity": granularity,
        "count": count,
        "results": [r.__dict__ for r in results],
    }
