"""API FastAPI — scanner, suggestion, trade isolé et sessions.

Deux façons d'exécuter :
- `POST /api/orders/place` : un seul trade, un seul ordre, puis plus rien.
- `POST /api/sessions/start` : une session qui enchaîne plusieurs petits
  trades vers un objectif cumulé, avec une PERTE MAX OBLIGATOIRE qui
  l'arrête net — voir `session.py`.

Dans les deux cas rien n'est exécuté sans confirmation explicite du client,
et plusieurs plafonds serveur (risque par trade, perte max de session,
nombre de trades) ne peuvent pas être contournés depuis l'app.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .analysis import SpreadTooWideError, TradeAnalyzer
from .broker import Broker, BrokerError
from .broker_factory import make_broker
from .config import get_settings
from .scanner import VolatilityScanner
from .session import SessionError, SessionManager

app = FastAPI(
    title="OANDA Trading Bot — API",
    description="Scanner de volatilité, suggestion de trade et sessions à perte max plafonnée.",
    version="0.1.0",
)

# CORS ouvert pour le développement local de la PWA. À restreindre à ton
# domaine une fois déployé.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_scanner: VolatilityScanner | None = None
_client: Broker | None = None
_sessions: SessionManager | None = None


def get_client() -> Broker:
    global _client
    if _client is None:
        _client = make_broker(get_settings())
    return _client


def get_session_manager() -> SessionManager:
    global _sessions
    if _sessions is None:
        _sessions = SessionManager(get_client(), get_settings())
    return _sessions


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
        "broker": settings.broker,
        "environment": settings.saxo_environment
        if settings.broker == "saxo"
        else settings.oanda_environment,
        "warning": "LIVE — argent réel" if settings.is_live else "simulation — argent fictif",
    }


@app.get("/api/scanner/top-volatile")
async def top_volatile(
    limit: int = Query(10, ge=1, le=50),
    granularity: str = Query("M5", pattern="^(M1|M5|M15|M30|H1|H4|D)$"),
    count: int = Query(50, ge=5, le=500),
) -> dict:
    try:
        results = await get_scanner().scan(limit=limit, granularity=granularity, count=count)
    except BrokerError as exc:
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
            max_spread_ratio=settings.max_spread_ratio,
        )
    except SpreadTooWideError as exc:
        # 409 : rien d'invalide dans la requête, c'est l'état du marché qui
        # rend ce trade non rentable en l'état.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BrokerError as exc:
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
        result = await get_client().place_market_order(
            instrument=req.instrument,
            units=req.units,
            stop_loss_price=req.stop_loss_price,
            take_profit_price=req.take_profit_price,
        )
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"broker": settings.broker, "trade_id": result}


@app.get("/api/positions")
async def open_positions() -> dict:
    try:
        trades = await get_client().list_open_trades()
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"trades": [t.__dict__ for t in trades]}


class StartSessionRequest(BaseModel):
    """Paramètres d'une session.

    `max_loss_amount` n'a volontairement PAS de valeur par défaut : une
    session ne peut pas démarrer sans que tu aies dit explicitement combien
    tu acceptes de perdre au total. Le serveur la borne ensuite à
    `MAX_SESSION_LOSS_PCT` du solde réel.
    """

    instrument: str
    risk_pct: float = Field(gt=0, le=0.02, description="Fraction du solde risquée par trade")
    objective_amount: float = Field(gt=0, description="Gain cumulé visé, en devise du compte")
    max_loss_amount: float = Field(gt=0, description="Perte cumulée maximale — OBLIGATOIRE")
    reward_ratio: float = Field(default=1.5, gt=0, le=5)
    granularity: str = Field(default="M15", pattern="^(M1|M5|M15|M30|H1|H4|D)$")
    # Confirmation explicite, comme pour un ordre isolé.
    confirm: bool = False


@app.post("/api/sessions/start")
async def start_session(req: StartSessionRequest) -> dict:
    settings = get_settings()

    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm doit être true — aucune session ne démarre sans confirmation explicite.",
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
        session = await get_session_manager().start(
            instrument=req.instrument,
            risk_pct=req.risk_pct,
            objective_amount=req.objective_amount,
            max_loss_amount=req.max_loss_amount,
            reward_ratio=req.reward_ratio,
            granularity=req.granularity,
        )
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return session.to_dict()


@app.get("/api/sessions/active")
async def active_session() -> dict:
    session = get_session_manager().active_session
    return {"session": session.to_dict() if session else None}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    session = get_session_manager().sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session inconnue : {session_id}")
    return session.to_dict()


@app.post("/api/sessions/{session_id}/stop")
async def stop_session(session_id: str) -> dict:
    """Arrête la session : plus aucun nouveau trade n'est ouvert.

    Un trade déjà ouvert n'est PAS fermé de force — il garde son stop-loss et
    son take-profit chez OANDA et se fermera tout seul à l'un des deux prix.
    """
    try:
        session = get_session_manager().stop(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.to_dict()


@app.get("/api/limits")
async def limits() -> dict:
    """Les plafonds de sécurité appliqués côté serveur, pour affichage."""
    settings = get_settings()
    return {
        "max_risk_pct": settings.max_risk_pct,
        "max_session_loss_pct": settings.max_session_loss_pct,
        "max_trades_per_session": settings.max_trades_per_session,
        "max_spread_ratio": settings.max_spread_ratio,
        "orders_allowed": settings.orders_allowed,
        "broker": settings.broker,
        "is_live": settings.is_live,
    }


class PushSubscriptionRequest(BaseModel):
    """Abonnement Web Push tel que produit par le navigateur.

    Format renvoyé par `PushManager.subscribe()` côté PWA :
    `{endpoint, keys: {p256dh, auth}}`.
    """

    endpoint: str
    keys: dict


@app.get("/api/notifications/config")
async def notifications_config() -> dict:
    """Clé publique VAPID à utiliser par la PWA pour s'abonner."""
    notifier = get_session_manager().notifier
    return {
        "push_enabled": notifier.push_enabled,
        "vapid_public_key": notifier.vapid_public_key or None,
        "subscribers": len(notifier.subscriptions),
        "thresholds_percent": [25, 50, 75, 100],
    }


@app.post("/api/notifications/subscribe")
async def subscribe_push(req: PushSubscriptionRequest) -> dict:
    notifier = get_session_manager().notifier
    if not notifier.push_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Web Push non configuré. Génère une paire de clés avec "
                "`python -c \"from app.notifier import generate_vapid_keys; "
                "generate_vapid_keys()\"` puis renseigne VAPID_PRIVATE_KEY, "
                "VAPID_PUBLIC_KEY et VAPID_CLAIM_EMAIL dans backend/.env."
            ),
        )
    try:
        created = notifier.subscribe(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"subscribed": True, "new": created, "subscribers": len(notifier.subscriptions)}


@app.post("/api/notifications/unsubscribe")
async def unsubscribe_push(req: PushSubscriptionRequest) -> dict:
    notifier = get_session_manager().notifier
    removed = notifier.unsubscribe(req.endpoint)
    return {"removed": removed, "subscribers": len(notifier.subscriptions)}


@app.get("/api/sessions/{session_id}/milestones")
async def session_milestones(session_id: str) -> dict:
    """Paliers franchis par la session — pour affichage dans l'app."""
    session = get_session_manager().sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session inconnue : {session_id}")
    return {
        "session_id": session_id,
        "realized_pl": round(session.realized_pl, 2),
        "progress_gain_pct": round(
            max(0.0, session.realized_pl) / session.objective_amount * 100, 1
        )
        if session.objective_amount > 0
        else 0.0,
        "progress_loss_pct": round(
            max(0.0, -session.realized_pl) / session.max_loss_amount * 100, 1
        )
        if session.max_loss_amount > 0
        else 0.0,
        "milestones": [m.to_dict() for m in session.milestones],
    }
