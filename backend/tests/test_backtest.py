"""Tests du backtester lui-même.

Un backtest qui se trompe donne une fausse confiance et coûte de l'argent
réel. Ces tests vérifient la mécanique avant de croire le moindre chiffre
qu'elle produira.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import run_backtest  # noqa: E402


def candle(o, h, l, c):
    return {"mid": {"o": f"{o:.6f}", "h": f"{h:.6f}", "l": f"{l:.6f}", "c": f"{c:.6f}"}}


def random_walk(n, seed=42, start=1.10, step=0.0005):
    """Marche aléatoire SANS tendance : aucun avantage à exploiter."""
    rng = random.Random(seed)
    candles, price = [], start
    for _ in range(n):
        o = price
        c = price + rng.gauss(0, step)
        h = max(o, c) + abs(rng.gauss(0, step / 2))
        l = min(o, c) - abs(rng.gauss(0, step / 2))
        candles.append(candle(o, h, l, c))
        price = c
    return candles


def steady_uptrend(n, start=1.10, step=0.0004, noise=0.00005):
    """Hausse régulière : une stratégie suiveuse de tendance doit y gagner."""
    candles, price = [], start
    for _ in range(n):
        o = price
        c = price + step
        candles.append(candle(o, c + noise, o - noise, c))
        price = c
    return candles


def test_random_walk_gives_no_edge():
    """Sur une marche aléatoire, le taux de réussite doit tendre vers 1/(1+ratio).

    C'est LE test qui détecte un regard vers le futur. Sans avantage réel,
    un ratio de 1.5 impose environ 40% de réussite (il faut parcourir 1.5x
    plus de distance pour gagner). Un backtester qui triche afficherait
    nettement plus.
    """
    result = run_backtest(
        random_walk(6000), spread=0.0, reward_ratio=1.5, risk_amount=2.5
    )
    assert len(result.closed) >= 30, f"trop peu de trades: {len(result.closed)}"
    assert 0.28 <= result.win_rate <= 0.48, (
        f"taux {result.win_rate:.1%} hors du domaine attendu (~40%) — "
        "signe probable d'un regard vers le futur"
    )
    print(
        f"  marche aléatoire: {result.win_rate:.1%} de réussite sur "
        f"{len(result.closed)} trades (théorie ~40%)"
    )


def test_no_lookahead():
    """Les décisions passées ne doivent pas changer quand on ajoute du futur."""
    candles = random_walk(2000, seed=7)
    short = run_backtest(candles[:1200], spread=0.0)
    long = run_backtest(candles, spread=0.0)

    # Les trades ouverts bien avant la coupure doivent être identiques.
    cutoff = 1100
    a = [(t.entry_index, round(t.entry_price, 6)) for t in short.trades if t.entry_index < cutoff]
    b = [(t.entry_index, round(t.entry_price, 6)) for t in long.trades if t.entry_index < cutoff]
    assert a == b, (
        "les entrées passées changent selon les bougies futures disponibles "
        "=> le backtester regarde le futur"
    )
    print(f"  {len(a)} entrées identiques avec et sans données futures")


def test_ambiguous_bar_counts_as_loss():
    """Une bougie touchant stop ET objectif doit compter comme une perte."""
    # Tendance haussière pour armer un achat, puis une bougie très large.
    candles = steady_uptrend(80)
    candles.append(candle(1.1330, 1.2000, 1.0500, 1.1330))  # englobe tout
    candles.append(candle(1.1330, 1.1340, 1.1320, 1.1330))

    result = run_backtest(candles, spread=0.0, reward_ratio=1.5)
    ambiguous = [t for t in result.closed if t.exit_index == len(candles) - 2]
    assert ambiguous, "la bougie large aurait dû clôturer un trade"
    assert all(t.won is False for t in ambiguous), (
        "une bougie ambiguë comptée comme gain gonflerait artificiellement le résultat"
    )
    print(f"  bougie englobant stop+objectif -> comptée en perte ({len(ambiguous)} trade)")


def test_uptrend_is_profitable_for_trend_following():
    """Validation de bon sens : une hausse franche doit être gagnante."""
    result = run_backtest(steady_uptrend(1500), spread=0.0, reward_ratio=1.5)
    assert len(result.closed) >= 5, f"trop peu de trades: {len(result.closed)}"
    assert result.win_rate > 0.8, f"taux {result.win_rate:.1%} sur une hausse régulière"
    assert result.net_pl > 0, result.net_pl
    print(
        f"  hausse régulière: {result.win_rate:.0%} de réussite, "
        f"P/L {result.net_pl:+.2f}"
    )


def test_spread_lowers_win_rate_and_pl():
    """Le spread doit faire BAISSER le taux de réussite et le P/L.

    Sur une marche aléatoire sans frais, la stratégie tourne autour du
    seuil d'équilibre. Ajouter le spread doit la faire passer en dessous —
    sinon le coût n'est pas réellement facturé.
    """
    candles = random_walk(6000, seed=11)
    free = run_backtest(candles, spread=0.0, reward_ratio=1.5)
    costly = run_backtest(candles, spread=0.00015, reward_ratio=1.5)

    assert costly.total_spread_cost > 0, "aucun frais comptabilisé"
    assert costly.win_rate < free.win_rate, (
        f"le spread n'a pas dégradé la réussite ({free.win_rate:.1%} -> "
        f"{costly.win_rate:.1%}) : le coût n'est pas facturé"
    )
    assert costly.net_pl < free.net_pl, (
        f"le spread n'a pas dégradé le P/L ({free.net_pl:+.2f} -> {costly.net_pl:+.2f})"
    )
    print(
        f"  sans frais {free.win_rate:.1%} / {free.net_pl:+.2f}  ->  "
        f"avec frais {costly.win_rate:.1%} / {costly.net_pl:+.2f}"
    )


def test_pl_and_win_rate_are_consistent():
    """Le P/L doit correspondre exactement au taux de réussite mesuré.

    C'est le test qui a rattrapé le bug initial : un P/L positif avec un
    taux sous le seuil est arithmétiquement impossible.
    """
    r = run_backtest(random_walk(6000, seed=21), spread=0.00012,
                     reward_ratio=1.5, risk_amount=2.5)
    n, w = len(r.closed), r.wins
    attendu = w * (2.5 * 1.5) - (n - w) * 2.5
    assert abs(r.net_pl - attendu) < 0.01, (
        f"P/L {r.net_pl:+.2f} incohérent avec {w}/{n} gagnants (attendu {attendu:+.2f})"
    )
    # Et le signe doit suivre la position par rapport au seuil.
    if r.win_rate < r.breakeven_win_rate:
        assert r.net_pl < 0, (
            f"taux {r.win_rate:.1%} sous le seuil {r.breakeven_win_rate:.1%} "
            f"mais P/L {r.net_pl:+.2f} positif — impossible"
        )
    print(
        f"  {w}/{n} gagnants ({r.win_rate:.1%}) -> P/L {r.net_pl:+.2f}, "
        f"cohérent avec le seuil de {r.breakeven_win_rate:.1%}"
    )


def test_wide_spread_blocks_trades():
    """Le même refus qu'en production s'applique dans le backtest."""
    candles = random_walk(3000, seed=3)
    blocked = run_backtest(candles, spread=0.05)  # ruineux face au stop
    assert len(blocked.trades) == 0, (
        f"{len(blocked.trades)} trades ouverts malgré un spread ruineux"
    )
    print("  spread ruineux -> aucun trade ouvert, comme en production")


def test_max_drawdown_is_negative_or_zero():
    result = run_backtest(random_walk(4000, seed=5), spread=0.00012)
    assert result.max_drawdown <= 0, result.max_drawdown
    print(f"  pire recul mesuré: {result.max_drawdown:.2f}")


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
