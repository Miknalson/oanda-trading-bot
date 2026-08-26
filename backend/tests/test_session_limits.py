"""Tests des garde-fous de session — la partie qui protège le compte.

On remplace OANDA par un faux client déterministe : pas de réseau, pas
d'argent, et on peut forcer une série de trades perdants pour vérifier que
la session s'arrête bien au lieu d'éroder tout le solde.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.session import SessionError, SessionManager  # noqa: E402

BALANCE = 250.0


def make_settings(**overrides) -> Settings:
    base = Settings(
        oanda_api_key="fake",
        oanda_account_id="fake",
        oanda_environment="practice",
        scanner_cache_ttl_seconds=30,
        live_trading_confirmed=False,
        max_risk_pct=0.02,
        max_session_loss_pct=0.10,
        max_trades_per_session=20,
        session_poll_seconds=0.0,  # pas d'attente réelle dans les tests
    )
    return replace(base, **overrides)


class FakeClient:
    """Faux OANDA : bougies synthétiques, trades qui se ferment aussitôt."""

    def __init__(self, outcomes: list[str], balance: float = BALANCE) -> None:
        self.outcomes = outcomes  # "win" ou "loss", un par trade
        self.balance = balance
        self.orders: list[dict] = []
        self._next_id = 0
        self._pl: dict[str, float] = {}

    async def get_account_summary(self) -> dict:
        return {"balance": str(self.balance)}

    async def get_candles(self, instrument, granularity="M15", count=100) -> list[dict]:
        # Tendance haussière franche + amplitude constante => ATR > 0.
        candles = []
        price = 1.0
        for i in range(count):
            price += 0.001
            candles.append(
                {"mid": {"o": f"{price:.5f}", "h": f"{price + 0.002:.5f}",
                         "l": f"{price - 0.002:.5f}", "c": f"{price:.5f}"}}
            )
        return candles

    async def create_market_order_with_brackets(
        self, instrument, units, stop_loss_price, take_profit_price
    ) -> dict:
        self._next_id += 1
        trade_id = str(self._next_id)
        self.orders.append({"instrument": instrument, "units": units, "trade_id": trade_id})

        idx = self._next_id - 1
        outcome = self.outcomes[idx] if idx < len(self.outcomes) else "loss"
        risk = abs(units) * abs(float(take_profit_price) - float(stop_loss_price)) / 2.5
        # Le P/L réalisé reflète la distance réellement parcourue.
        stop_distance = abs(stop_loss_price - take_profit_price) / 2.5
        risk_amount = abs(units) * stop_distance
        reward_amount = abs(units) * (abs(take_profit_price - stop_loss_price) - stop_distance)
        self._pl[trade_id] = reward_amount if outcome == "win" else -risk_amount
        return {"orderFillTransaction": {"tradeOpened": {"tradeID": trade_id}}}

    async def get_trade(self, trade_id: str) -> dict:
        return {"state": "CLOSED", "realizedPL": str(self._pl[trade_id])}


async def run_session(outcomes, *, max_loss, objective, settings=None, balance=BALANCE):
    client = FakeClient(outcomes, balance=balance)
    mgr = SessionManager(client, settings or make_settings())
    session = await mgr.start(
        instrument="EUR_USD",
        risk_pct=0.01,
        objective_amount=objective,
        max_loss_amount=max_loss,
        reward_ratio=1.5,
    )
    await mgr._task  # attend la fin de la boucle
    return session, client


def test_max_loss_stops_the_session():
    """Une série ininterrompue de pertes doit arrêter la session."""
    session, client = asyncio.run(
        run_session(["loss"] * 50, max_loss=20.0, objective=20.0)
    )
    assert not session.is_active, "la session doit être terminée"
    assert session.stop_reason in ("max_loss_reached", "max_loss_would_be_exceeded"), session.stop_reason
    assert session.realized_pl >= -20.0, (
        f"perte {session.realized_pl:.2f} a dépassé la perte max de 20.00"
    )
    print(
        f"  perte max: arrêt après {len(session.trades)} trades, "
        f"P/L {session.realized_pl:.2f} (limite -20.00), raison={session.stop_reason}"
    )


def test_objective_stops_the_session():
    session, _ = asyncio.run(run_session(["win"] * 50, max_loss=20.0, objective=20.0))
    assert session.stop_reason == "objective_reached", session.stop_reason
    assert session.realized_pl >= 20.0
    print(
        f"  objectif: arrêt après {len(session.trades)} trades, "
        f"P/L +{session.realized_pl:.2f}"
    )


def test_max_trades_cap():
    """Même sans atteindre objectif ni perte max, on ne boucle pas sans fin."""
    settings = make_settings(max_trades_per_session=5)
    session, _ = asyncio.run(
        run_session(
            # Solde large + limites hautes : ni l'objectif ni la perte max ne
            # peuvent se déclencher, seul le plafond de trades doit arrêter.
            ["win", "loss"] * 50, max_loss=10_000.0, objective=1_000_000.0,
            settings=settings, balance=100_000.0,
        )
    )
    assert session.stop_reason == "max_trades_reached", session.stop_reason
    assert len(session.trades) == 5, len(session.trades)
    print(f"  plafond trades: arrêt à {len(session.trades)} trades comme prévu")


def test_max_loss_is_capped_by_server():
    """Le client ne peut pas demander une perte max supérieure au plafond serveur."""
    async def attempt():
        client = FakeClient([], balance=250.0)
        mgr = SessionManager(client, make_settings())  # plafond = 10% de 250 = 25
        await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=20.0, max_loss_amount=200.0,  # bien trop
            reward_ratio=1.5,
        )

    try:
        asyncio.run(attempt())
    except SessionError as exc:
        assert "plafond de sécurité serveur" in str(exc), str(exc)
        print(f"  plafond serveur: refusé comme attendu -> {exc}")
        return
    raise AssertionError("une perte max de 200 sur un solde de 250 aurait dû être refusée")


def test_max_loss_is_mandatory():
    async def attempt():
        client = FakeClient([], balance=250.0)
        mgr = SessionManager(client, make_settings())
        await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=20.0, max_loss_amount=0.0,
            reward_ratio=1.5,
        )

    try:
        asyncio.run(attempt())
    except SessionError as exc:
        assert "obligatoire" in str(exc), str(exc)
        print(f"  perte max obligatoire: refusé comme attendu -> {exc}")
        return
    raise AssertionError("une session sans perte max aurait dû être refusée")


def test_only_one_session_at_a_time():
    async def attempt():
        client = FakeClient(["loss"] * 100, balance=100_000.0)
        settings = make_settings(session_poll_seconds=0.05)
        mgr = SessionManager(client, settings)
        await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=10_000.0, max_loss_amount=5_000.0, reward_ratio=1.5,
        )
        await mgr.start(  # doit lever
            instrument="GBP_USD", risk_pct=0.01,
            objective_amount=10_000.0, max_loss_amount=5_000.0, reward_ratio=1.5,
        )

    try:
        asyncio.run(attempt())
    except SessionError as exc:
        assert "déjà en cours" in str(exc), str(exc)
        print(f"  session unique: refusé comme attendu -> {exc}")
        return
    raise AssertionError("une deuxième session simultanée aurait dû être refusée")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            print(f"\n{t.__name__}:")
            t()
            print("  ✅ OK")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ ÉCHEC: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passés")
    sys.exit(1 if failed else 0)
