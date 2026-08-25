"""API FastAPI — scanner de volatilité + suggestion/exécution de trade.

Phase 3 : le passage d'ordre existe (`POST /api/orders/place`) mais reste
protégé par plusieurs garde-fous — voir la fonction elle-même et
`Settings.orders_allowed`. Rien n'est jamais exécuté sans un `confirm: true`
explicite envoyé par le client (donc par toi, en cliquant "Lancer" dans
l'app).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .analysis import TradeAnalyzer
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
_client: OandaClient | None = None


def get_client() -> OandaClient:
    global _client
    if _client is None:
        _client = OandaClient(get_settings())
    return _client


def get_scanner() -> VolatilityScanner:
    global _scanner
    if _scanner is None:
        settings = get_settings()
        _scanner = VolatilityScanner(
            client=get_client(), cache_ttl_seconds=settings.scanner_cache_ttl_seconds
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


@app.get("/api/analysis/suggest")
async def suggest_trade(
    instrument: str,
    risk_pct: float = Query(0.01, gt=0, le=0.02, description="Fraction du solde risquée (max 2%)"),
    objective_amount: float = Query(..., gt=0, description="Objectif de gain, dans la devise du compte"),
    granularity: str = Query("M15", pattern="^(M1|M5|M15|M30|H1|H4|D)$"),
) -> dict:
    settings = get_settings()
    try:
        suggestion = await TradeAnalyzer(get_client()).suggest(
            instrument=instrument,
            risk_pct=risk_pct,
            objective_amount=objective_amount,
            granularity=granularity,
            max_risk_pct=settings.max_risk_pct,
        )
    except OandaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return suggestion.__dict__


class PlaceOrderRequest(BaseModel):
    instrument: str
    units: int
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    risk_pct: float = Field(gt=0, le=0.02)
    # Doit être explicitement `true` — c'est le "clic de lancement" de la
    # Phase 3. Aucun ordre n'est jamais passé sans cette confirmation.
    confirm: bool = False


@app.post("/api/orders/place")
async def place_order(req: PlaceOrderRequest) -> dict:
    settings = get_settings()

    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm doit être true — aucun ordre n'est passé sans confirmation explicite.",
        )
    if req.risk_pct > settings.max_risk_pct:
        raise HTTPException(
            status_code=400,
            detail=f"risk_pct dépasse le plafond de sécurité serveur ({settings.max_risk_pct:.2%}).",
        )
    if not settings.orders_allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Trading en LIVE non confirmé côté serveur. Défini "
                "LIVE_TRADING_CONFIRMED=true dans backend/.env une fois que tu es "
                "prêt à trader avec de l'argent réel."
            ),
        )

    try:
        result = await get_client().create_market_order_with_brackets(
            instrument=req.instrument,
            units=req.units,
            stop_loss_price=req.stop_loss_price,
            take_profit_price=req.take_profit_price,
        )
    except OandaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"environment": settings.oanda_environment, "oanda_response": result}


@app.get("/api/positions")
async def open_positions() -> dict:
    try:
        trades = await get_client().list_open_trades()
    except OandaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"trades": trades}
