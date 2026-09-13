"""Tests de la prise en compte du spread (le coût réel de chaque trade)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import SpreadTooWideError, TradeAnalyzer  # noqa: E402
from app.broker import AccountSummary, Candle, Quote  # noqa: E402

BALANCE = 250.0


class PricedClient:
    """Faux OANDA avec un spread contrôlable et un ATR connu."""

    def __init__(self, spread: float, atr_target: float = 0.0006, mid: float = 1.1000,
                 balance: float = BALANCE) -> None:
        self.spread = spread
        self.atr_target = atr_target
        self.mid = mid
        self.balance = balance

    async def get_account_summary(self):
        return AccountSummary(balance=self.balance, currency="EUR")

    async def get_candles(self, instrument, granularity="M15", count=100):
        # Hausse régulière (tendance "buy") + amplitude constante => ATR connu.
        candles, price = [], self.mid - 0.01
        half = self.atr_target / 2
        for _ in range(count):
            price += 0.0002
            candles.append(
                Candle(open=price, high=price + half, low=price - half, close=price)
            )
        # Dernière clôture au niveau du mid visé.
        last = candles[-1]
        candles[-1] = Candle(last.open, last.high, last.low, self.mid)
        return candles

    async def get_quote(self, instrument):
        return Quote(
            bid=self.mid - self.spread / 2,
            ask=self.mid + self.spread / 2,
            tradeable=True,
        )


def suggest(spread, *, atr_target=0.0006, max_spread_ratio=0.15, ratio=1.5):
    client = PricedClient(spread, atr_target=atr_target)
    return asyncio.run(
        TradeAnalyzer(client).suggest(
            instrument="EUR_USD", risk_pct=0.01, reward_ratio=ratio,
            max_spread_ratio=max_spread_ratio,
        )
    )


def test_entry_uses_ask_not_mid():
    """À l'achat, l'entrée doit être le prix ask, pas le médian."""
    spread = 0.00012
    s = suggest(spread)
    assert s.direction == "buy", s.direction
    expected_ask = 1.1000 + spread / 2
    assert abs(s.entry_price - expected_ask) < 1e-9, (
        f"entrée {s.entry_price} != ask {expected_ask} (le mid serait 1.1000)"
    )
    print(f"  entrée au ask {s.entry_price:.5f} (mid 1.10000, spread {spread})")


def test_spread_cost_is_reported():
    s = suggest(0.00012)
    risk = BALANCE * 0.01
    # Coût attendu = (spread / distance_stop) x montant risqué
    stop_distance = abs(s.entry_price - s.stop_loss_price)
    expected = (0.00012 / stop_distance) * risk
    assert abs(s.spread_cost - expected) < 0.02, (
        f"coût annoncé {s.spread_cost} != attendu {expected:.2f}"
    )
    assert s.spread_pct_of_risk > 0, "le % du risque doit être renseigné"
    print(
        f"  coût du spread {s.spread_cost:.2f}€ sur {risk:.2f}€ risqués "
        f"= {s.spread_pct_of_risk:.0%} du risque"
    )


def test_losing_needs_less_movement_than_winning():
    """Le spread rend la perte plus proche que le gain — c'est ça, son coût."""
    s = suggest(0.00012)
    assert s.move_to_lose < s.move_to_win, (s.move_to_lose, s.move_to_win)
    # Perdre demande le stop MOINS le spread ; gagner l'objectif PLUS le spread.
    stop_distance = abs(s.entry_price - s.stop_loss_price)
    tp_distance = abs(s.take_profit_price - s.entry_price)
    assert abs(s.move_to_lose - (stop_distance - 0.00012)) < 1e-9
    assert abs(s.move_to_win - (tp_distance + 0.00012)) < 1e-9
    print(
        f"  mouvement pour perdre {s.move_to_lose:.5f} < pour gagner {s.move_to_win:.5f}"
    )


def test_wide_spread_is_refused():
    """Un spread représentant plus que le plafond du stop doit être refusé."""
    # ATR minuscule (type M1) => stop serré => spread énorme en proportion.
    try:
        suggest(0.00012, atr_target=0.00006)  # stop = 1.5 x 0.00006 = 0.00009
    except SpreadTooWideError as exc:
        assert "Spread trop large" in str(exc)
        print(f"  refusé comme attendu -> {str(exc)[:90]}...")
        return
    raise AssertionError("un spread supérieur au stop aurait dû être refusé")


def test_reasonable_spread_is_accepted():
    """Un stop large (type H1/H4) rend le spread négligeable : accepté."""
    s = suggest(0.00012, atr_target=0.0024)  # stop = 0.0036
    assert s.spread_pct_of_risk < 0.05, s.spread_pct_of_risk
    print(f"  stop large -> spread à {s.spread_pct_of_risk:.1%} du risque, accepté")


def test_risk_amount_stays_exact_despite_spread():
    """La perte si le stop est touché reste exactement le montant risqué."""
    s = suggest(0.00012)
    stop_distance = abs(s.entry_price - s.stop_loss_price)
    real_loss = stop_distance * abs(s.suggested_units)
    assert abs(real_loss - s.risk_amount) < 0.05, (
        f"perte réelle {real_loss:.2f} != risque annoncé {s.risk_amount:.2f}"
    )
    print(f"  perte réelle au stop {real_loss:.2f}€ = risque annoncé {s.risk_amount:.2f}€")


def test_rationale_warns_about_fees():
    s = suggest(0.00012)
    assert "Spread" in s.rationale, s.rationale
    assert "frais" in s.rationale.lower(), s.rationale
    print(f"  explication: ...{s.rationale[-120:]}")


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


def test_un_petit_compte_est_refuse_avant_d_exploser_le_plafond_de_risque():
    """Le piège des petits comptes : le minimum du courtier dicte le risque.

    Avec 100 € de capital et un minimum de 10 000 unités, une seule perte vaut
    12 € — 12 % du compte, six fois le plafond de sécurité. Le pourcentage de
    risque demandé n'y peut rien : c'est la taille minimale qui décide. Mieux
    vaut refuser en l'expliquant que laisser le courtier rejeter l'ordre sans
    raison lisible, ou pire, ouvrir une position hors de toute limite.
    """
    import asyncio

    from app.analysis import TradeAnalyzer
    from app.broker import BrokerError

    client = PricedClient(spread=0.00012, balance=100.0)

    try:
        asyncio.run(TradeAnalyzer(client).suggest(
            instrument="EUR_USD", risk_pct=0.01, objective_amount=20.0,
            reward_ratio=1.5, min_trade_units=10_000,
        ))
        raise AssertionError("un compte de 100 € a été accepté avec un minimum de 10 000")
    except BrokerError as exc:
        message = str(exc)

    assert "minimum du courtier" in message, message
    assert "10000" in message or "10 000" in message, message
    print(f"  100 € / minimum 10 000 -> refusé : « {message[:70]}… »")


def test_le_garde_fou_est_desactive_par_defaut():
    """Une contrainte non vérifiée ne doit pas bloquer de trades valides.

    La taille minimale réelle de Saxo n'a pas pu être confirmée. Livrer 1000
    par défaut refuserait des trades légitimes chez un courtier plus permissif
    — un faux positif coûte plus cher ici qu'un faux négatif, puisque sans le
    réglage c'est le courtier lui-même qui refusera l'ordre.
    """
    import asyncio

    from app.analysis import TradeAnalyzer
    from app.config import Settings

    assert Settings.min_trade_units == 0, (
        f"défaut à {Settings.min_trade_units} : contrainte non vérifiée imposée"
    )

    client = PricedClient(spread=0.00012, balance=100.0)
    suggestion = asyncio.run(TradeAnalyzer(client).suggest(
        instrument="EUR_USD", risk_pct=0.01, objective_amount=20.0,
        reward_ratio=1.5,
    ))
    assert suggestion.suggested_units != 0
    print(f"  défaut désactivé -> {abs(suggestion.suggested_units)} unités acceptées")
