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

from app.broker import AccountSummary, Candle, Quote, TradeStatus  # noqa: E402
from app.config import Settings  # noqa: E402
from app.session import SessionError, SessionManager  # noqa: E402

BALANCE = 250.0


def make_settings(**overrides) -> Settings:
    base = Settings(
        broker="oanda",
        saxo_access_token="",
        saxo_environment="sim",
        oanda_api_key="fake",
        oanda_account_id="fake",
        oanda_environment="practice",
        scanner_cache_ttl_seconds=30,
        live_trading_confirmed=False,
        max_risk_pct=0.02,
        max_session_loss_pct=0.10,
        max_trades_per_session=20,
        session_poll_seconds=0.0,  # pas d'attente réelle dans les tests
        max_spread_ratio=0.15,
        vapid_private_key="",
        vapid_public_key="",
        vapid_claim_email="",
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

    async def get_account_summary(self):
        return AccountSummary(balance=self.balance, currency="EUR")

    async def get_quote(self, instrument):
        # Spread volontairement minuscule : ces tests portent sur les limites
        # de session, pas sur le coût du spread (couvert par test_spread.py).
        mid = 1.0 + 0.001 * 60
        spread = 0.000001
        return Quote(bid=mid - spread / 2, ask=mid + spread / 2, tradeable=True)

    async def get_candles(self, instrument, granularity="M15", count=100):
        # Tendance haussière franche + amplitude constante => ATR > 0.
        candles = []
        price = 1.0
        for _ in range(count):
            price += 0.001
            candles.append(
                Candle(open=price, high=price + 0.002, low=price - 0.002, close=price)
            )
        return candles

    async def place_market_order(
        self, instrument, units, stop_loss_price, take_profit_price
    ) -> str:
        self._next_id += 1
        trade_id = str(self._next_id)
        self.orders.append({"instrument": instrument, "units": units, "trade_id": trade_id})

        idx = self._next_id - 1
        outcome = self.outcomes[idx] if idx < len(self.outcomes) else "loss"
        # Le P/L réalisé reflète la distance réellement parcourue.
        stop_distance = abs(stop_loss_price - take_profit_price) / 2.5
        risk_amount = abs(units) * stop_distance
        reward_amount = abs(units) * (abs(take_profit_price - stop_loss_price) - stop_distance)
        self._pl[trade_id] = reward_amount if outcome == "win" else -risk_amount
        return trade_id

    async def get_trade_status(self, trade_id: str) -> TradeStatus:
        return TradeStatus(closed=True, market_pl=self._pl[trade_id], financing=0.0)


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




class RecordingNotifier:
    """Faux notifier : enregistre les envois au lieu de faire du réseau."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, int]] = []
        self.fail = fail
        self.push_enabled = True
        self.vapid_public_key = "fake"
        self.subscriptions: list[dict] = []

    def send_milestone(self, milestone, session_id):
        if self.fail:
            raise RuntimeError("push cassé exprès")
        self.sent.append((milestone.kind, int(milestone.threshold * 100)))
        return 1


async def run_with_notifier(outcomes, *, max_loss, objective, notifier, balance=BALANCE):
    client = FakeClient(outcomes, balance=balance)
    mgr = SessionManager(client, make_settings(), notifier=notifier)
    session = await mgr.start(
        instrument="EUR_USD", risk_pct=0.01,
        objective_amount=objective, max_loss_amount=max_loss, reward_ratio=1.5,
    )
    await mgr._task
    return session


def test_milestones_fire_during_a_winning_session():
    notifier = RecordingNotifier()
    session = asyncio.run(
        run_with_notifier(["win"] * 50, max_loss=20.0, objective=20.0, notifier=notifier)
    )
    kinds = {k for k, _ in notifier.sent}
    percents = [p for _, p in notifier.sent]
    assert kinds == {"gain"}, kinds
    assert 100 in percents, percents
    assert percents == sorted(percents), f"paliers dans le désordre: {percents}"
    assert len(percents) == len(set(percents)), f"doublons: {percents}"
    assert [m.to_dict()["percent"] for m in session.milestones] == percents
    print(f"  session gagnante -> paliers notifiés: {percents}")


def test_milestones_fire_during_a_losing_session():
    notifier = RecordingNotifier()
    asyncio.run(
        run_with_notifier(["loss"] * 50, max_loss=20.0, objective=20.0, notifier=notifier)
    )
    kinds = {k for k, _ in notifier.sent}
    percents = [p for _, p in notifier.sent]
    assert kinds == {"loss"}, kinds
    assert percents == sorted(percents), percents
    assert len(percents) == len(set(percents)), f"doublons: {percents}"
    # Le palier 100% doit arriver même si le garde-fou arrête la session
    # juste sous la limite (sinon aucune notification à l'arrêt).
    assert 100 in percents, f"palier 100% manquant à l'arrêt: {percents}"
    print(f"  session perdante -> paliers notifiés: {percents}")


def test_push_failure_does_not_break_the_session():
    """Si l'envoi de notification plante, le trading continue normalement."""
    notifier = RecordingNotifier(fail=True)
    session = asyncio.run(
        run_with_notifier(["win"] * 50, max_loss=20.0, objective=20.0, notifier=notifier)
    )
    assert session.stop_reason == "objective_reached", session.stop_reason
    assert session.error is None, session.error
    assert len(session.milestones) > 0, "les paliers doivent rester enregistrés"
    print(
        f"  push en échec -> session terminée normalement "
        f"({session.stop_reason}), {len(session.milestones)} paliers conservés"
    )



class WideSpreadClient(FakeClient):
    """Comme FakeClient, mais avec un spread ruineux."""

    async def get_quote(self, instrument):
        mid = 1.0 + 0.001 * 60
        spread = 0.05  # énorme face au stop calculé
        return Quote(bid=mid - spread / 2, ask=mid + spread / 2, tradeable=True)


def test_session_stops_when_spread_too_wide():
    """Un spread ruineux arrête la session sans passer le moindre ordre."""
    async def run():
        client = WideSpreadClient(["win"] * 10)
        mgr = SessionManager(client, make_settings())
        session = await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=20.0, max_loss_amount=20.0, reward_ratio=1.5,
        )
        await mgr._task
        return session, client

    session, client = asyncio.run(run())
    assert session.stop_reason == "spread_too_wide", session.stop_reason
    assert len(client.orders) == 0, (
        f"{len(client.orders)} ordre(s) passé(s) alors que le spread était ruineux"
    )
    assert "Spread trop large" in (session.error or ""), session.error
    print("  spread ruineux -> session arrêtée, AUCUN ordre passé")



def _notified_kinds(notifier):
    return [(k, p) for k, p in notifier.sent]


def test_max_trades_end_is_notified():
    """Un arrêt sur plafond de trades ne doit pas être silencieux."""
    notifier = RecordingNotifier()
    settings = make_settings(max_trades_per_session=4)

    async def run():
        client = FakeClient(["win", "loss"] * 20, balance=100_000.0)
        mgr = SessionManager(client, settings, notifier=notifier)
        session = await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=1_000_000.0, max_loss_amount=10_000.0,
            reward_ratio=1.5,
        )
        await mgr._task
        return session

    session = asyncio.run(run())
    assert session.stop_reason == "max_trades_reached", session.stop_reason
    fins = [m for m in session.milestones if m.kind == "end"]
    assert len(fins) == 1, f"attendu 1 notification de fin, reçu {len(fins)}"
    assert "plafond" in fins[0].title.lower(), fins[0].title
    assert ("end", 100) in _notified_kinds(notifier), notifier.sent
    print(f"  plafond de trades -> notifié : « {fins[0].title} »")


def test_spread_too_wide_end_is_notified():
    notifier = RecordingNotifier()

    async def run():
        client = WideSpreadClient(["win"] * 5)
        mgr = SessionManager(client, make_settings(), notifier=notifier)
        session = await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=20.0, max_loss_amount=20.0, reward_ratio=1.5,
        )
        await mgr._task
        return session

    session = asyncio.run(run())
    assert session.stop_reason == "spread_too_wide", session.stop_reason
    fins = [m for m in session.milestones if m.kind == "end"]
    assert len(fins) == 1, f"attendu 1 notification de fin, reçu {len(fins)}"
    print(f"  spread trop large -> notifié : « {fins[0].title} »")


def test_objective_end_is_not_double_notified():
    """Objectif atteint : le palier 100% suffit, pas de doublon de fin."""
    notifier = RecordingNotifier()
    session = asyncio.run(
        run_with_notifier(["win"] * 50, max_loss=20.0, objective=20.0, notifier=notifier)
    )
    assert session.stop_reason == "objective_reached", session.stop_reason
    fins = [m for m in session.milestones if m.kind == "end"]
    assert fins == [], f"doublon : le palier 100% notifiait déjà la fin ({fins})"
    assert ("gain", 100) in _notified_kinds(notifier), notifier.sent
    print("  objectif atteint -> palier 100% seul, aucun doublon de fin")


def test_max_loss_end_is_not_double_notified():
    notifier = RecordingNotifier()
    session = asyncio.run(
        run_with_notifier(["loss"] * 50, max_loss=20.0, objective=20.0, notifier=notifier)
    )
    fins = [m for m in session.milestones if m.kind == "end"]
    assert fins == [], f"doublon : le palier 100% de perte notifiait déjà ({fins})"
    assert ("loss", 100) in _notified_kinds(notifier), notifier.sent
    print("  perte max atteinte -> palier 100% seul, aucun doublon de fin")


def test_manual_stop_is_notified():
    """Arrêt manuel : confirmation utile si tu as stoppé depuis un autre appareil."""
    notifier = RecordingNotifier()

    async def run():
        client = FakeClient(["win"] * 50, balance=100_000.0)
        mgr = SessionManager(client, make_settings(session_poll_seconds=0.05),
                             notifier=notifier)
        session = await mgr.start(
            instrument="EUR_USD", risk_pct=0.01,
            objective_amount=1_000_000.0, max_loss_amount=10_000.0, reward_ratio=1.5,
        )
        mgr.stop(session.id)
        return session

    session = asyncio.run(run())
    fins = [m for m in session.milestones if m.kind == "end"]
    assert len(fins) == 1, f"attendu 1 notification de fin, reçu {len(fins)}"
    assert session.stop_reason == "stopped_by_user", session.stop_reason
    print(f"  arrêt manuel -> notifié : « {fins[0].title} »")


def test_risque_par_trade_superieur_a_la_perte_max_est_refuse_au_demarrage():
    """Une configuration où aucun trade ne peut tenir doit être refusée.

    Avec 1 % de risque sur un solde de 1000, chaque trade risque 10. Si la
    perte max de la session vaut 10 aussi, le garde-fou d'avant-trade bloque
    la toute première entrée : la session démarrait pour s'arrêter aussitôt,
    zéro trade, sans explication. C'est un problème de réglage, il doit être
    dit au démarrage et nommer les deux issues.
    """
    client = FakeClient([], balance=1000.0)
    mgr = SessionManager(client, make_settings())
    try:
        asyncio.run(mgr.start(
            instrument="EUR_USD", risk_pct=0.02, objective_amount=20.0,
            max_loss_amount=10.0, reward_ratio=1.5,
        ))
        raise AssertionError("la session a démarré alors qu'aucun trade ne peut tenir")
    except SessionError as exc:
        message = str(exc)

    assert "20.00" in message, message          # risque par trade
    assert "10.00" in message, message          # perte max
    assert "Baisse le risque" in message, message
    assert not client.orders, "un ordre a été passé malgré le refus"
    print(f"  refusé au démarrage : « {message[:72]}… »")


def test_un_risque_egal_a_la_perte_max_laisse_passer_un_trade():
    """La perte max est un plafond ATTEIGNABLE, pas une valeur interdite.

    Un trade dont la perte atterrirait exactement sur la limite reste dans ce
    que l'utilisateur a accepté. Avec une comparaison non stricte, ce trade
    était refusé et la session se terminait à zéro trade.
    """
    session, client = asyncio.run(
        run_session(["loss"], max_loss=2.5, objective=20.0, balance=250.0)
    )
    assert len(client.orders) >= 1, (
        f"aucun trade ouvert alors que la perte max vaut exactement le risque "
        f"par trade (statut : {session.stop_reason})"
    )
    print(
        f"  risque = perte max -> {len(client.orders)} trade(s) ouvert(s), "
        f"fin : {session.stop_reason}"
    )


def test_aucune_alerte_de_perte_max_quand_rien_n_a_ete_perdu():
    """Le palier « perte max atteinte » ne doit pas mentir.

    Une session arrêtée avant son premier trade annonçait
    « 🛑 Perte max atteinte — 0.00 sur une limite de -10.00 ». Recevoir cette
    notification sans avoir tradé ni perdu un centime détruit la confiance
    dans toutes les autres alertes.
    """
    from app.milestones import MilestoneTracker

    client = FakeClient([], balance=1000.0)
    mgr = SessionManager(client, make_settings())
    session = asyncio.run(mgr.start(
        instrument="EUR_USD", risk_pct=0.01, objective_amount=20.0,
        max_loss_amount=50.0, reward_ratio=1.5,
    ))
    # Force l'arrêt « avant le trade de trop » avec un P/L intact.
    session.realized_pl = 0.0
    session.tracker = MilestoneTracker(objective_amount=20.0, max_loss_amount=50.0)
    session.milestones.clear()
    mgr._finish(session, "max_loss_would_be_exceeded")

    mensonges = [m for m in session.milestones
                 if m.kind == "loss" and m.threshold >= 1.0]
    assert not mensonges, (
        f"palier de perte max émis avec un P/L de {session.realized_pl} : "
        f"{[m.title for m in mensonges]}"
    )
    # La fin doit tout de même être signalée : silence interdit.
    assert session.milestones, "la fin de session n'a été signalée par rien"
    print(f"  P/L nul -> pas d'alerte de perte, mais fin signalée : "
          f"« {session.milestones[-1].title} »")


def test_le_palier_de_perte_part_toujours_quand_la_perte_est_reelle():
    """Contrepartie du test précédent : le vrai cas doit continuer de marcher.

    Le garde-fou arrête la session à −19,97 sur −20,00, donc le seuil des
    100 % n'est jamais franchi par le P/L lui-même. Le palier doit être émis
    explicitement, sinon la perte max est atteinte en silence.
    """
    session, _ = asyncio.run(
        run_session(["loss"] * 50, max_loss=20.0, objective=20.0)
    )
    assert session.realized_pl < 0, session.realized_pl
    finaux = [m for m in session.milestones
              if m.kind == "loss" and m.threshold >= 1.0]
    assert finaux, (
        f"perte réelle de {session.realized_pl:.2f} sans palier 100 % : "
        f"{[m.title for m in session.milestones]}"
    )
    print(f"  perte réelle {session.realized_pl:+.2f} -> « {finaux[-1].title} »")


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
