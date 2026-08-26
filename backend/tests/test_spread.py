"""Tests de la prise en compte du spread (le coût réel de chaque trade)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis import SpreadTooWideError, TradeAnalyzer  # noqa: E402

BALANCE = 250.0


class PricedClient:
    """Faux OANDA avec un spread contrôlable et un ATR connu."""

    def __init__(self, spread: float, atr_target: float = 0.0006, mid: float = 1.1000) -> None:
        self.spread = spread
        self.atr_target = atr_target
        self.mid = mid

    async def get_account_summary(self):
        return {"balance": str(BALANCE)}

    async def get_candles(self, instrument, granularity="M15", count=100):
        # Hausse régulière (tendance "buy") + amplitude constante => ATR connu.
        candles, price = [], self.mid - 0.01
        half = self.atr_target / 2
        for _ in range(count):
            price += 0.0002
            candles.append(
                {"mid": {"o": f"{price:.5f}", "h": f"{price + half:.5f}",
                         "l": f"{price - half:.5f}", "c": f"{price:.5f}"}}
            )
        # Dernière clôture au niveau du mid visé.
        candles[-1]["mid"]["c"] = f"{self.mid:.5f}"
        return candles

    async def get_pricing(self, instruments):
        return {
            instruments[0]: {
                "bid": self.mid - self.spread / 2,
                "ask": self.mid + self.spread / 2,
                "spread": self.spread,
                "tradeable": True,
            }
        }


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
