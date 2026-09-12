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
from app.broker import Candle  # noqa: E402


def candle(o, h, l, c):
    return Candle(open=o, high=h, low=l, close=c)


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
    # Résultat = marché (±montants exacts) + financement (un coût déduit).
    attendu = w * (2.5 * 1.5) - (n - w) * 2.5 + r.total_financing
    assert abs(r.net_pl - attendu) < 0.01, (
        f"P/L {r.net_pl:+.2f} incohérent avec {w}/{n} gagnants "
        f"et {r.total_financing:+.2f} de financement (attendu {attendu:+.2f})"
    )
    # Et le signe doit suivre la position par rapport au seuil.
    if r.win_rate < r.breakeven_win_rate:
        assert r.net_pl < 0, (
            f"taux {r.win_rate:.1%} sous le seuil {r.breakeven_win_rate:.1%} "
            f"mais P/L {r.net_pl:+.2f} positif — impossible"
        )
    print(
        f"  {w}/{n} gagnants ({r.win_rate:.1%}) -> P/L {r.net_pl:+.2f} "
        f"(dont {r.total_financing:+.2f} de financement), "
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



def test_financing_is_charged_and_hurts():
    """Le financement doit réduire le P/L et relever le seuil d'équilibre."""
    candles = random_walk(5000, seed=31)
    free = run_backtest(candles, spread=0.00012, granularity="H4",
                        financing_rate_annual=0.0)
    costly = run_backtest(candles, spread=0.00012, granularity="H4",
                          financing_rate_annual=0.02)

    assert free.total_financing == 0.0, free.total_financing
    assert costly.total_financing < 0, "le financement doit être un coût (négatif)"
    assert costly.net_pl < free.net_pl, (
        f"le financement n'a pas dégradé le P/L "
        f"({free.net_pl:+.2f} -> {costly.net_pl:+.2f})"
    )
    assert costly.breakeven_win_rate > free.breakeven_win_rate, (
        "le seuil d'équilibre doit monter avec le financement"
    )
    print(
        f"  sans financement {free.net_pl:+.2f} (seuil {free.breakeven_win_rate:.1%}) -> "
        f"avec {costly.net_pl:+.2f} (seuil {costly.breakeven_win_rate:.1%}, "
        f"{costly.total_financing:+.2f} d'intérêts)"
    )


def test_financing_grows_with_timeframe():
    """Plus l'intervalle est long, plus la détention coûte cher."""
    candles = random_walk(5000, seed=33)
    couts = {}
    for g in ("M15", "H1", "H4"):
        r = run_backtest(candles, spread=0.00012, granularity=g,
                         financing_rate_annual=0.02)
        if r.closed:
            couts[g] = -r.total_financing / len(r.closed)

    assert set(couts) == {"M15", "H1", "H4"}, couts
    assert couts["M15"] < couts["H1"] < couts["H4"], (
        f"le coût par trade devrait croître avec l'intervalle : {couts}"
    )
    print("  coût de détention par trade: " + ", ".join(
        f"{g} {c:.4f}" for g, c in couts.items()
    ))


def test_financing_is_zero_when_rate_is_zero():
    r = run_backtest(random_walk(3000, seed=35), spread=0.00012,
                     granularity="H4", financing_rate_annual=0.0)
    assert all(t.financing == 0.0 for t in r.closed)
    print("  taux nul -> aucun coût de détention, comme attendu")



def test_fetch_history_never_duplicates():
    """Une seule requête : jamais d'historique fabriqué par répétition.

    Les clients ne savent pas encore demander « plus ancien que telle date ».
    Boucler renverrait la même fenêtre ; empiler ces réponses produirait un
    backtest qui tourne sans rien mesurer.
    """
    import asyncio
    from app.backtest import fetch_history

    class ClientQuiRepete:
        """Renvoie toujours la même fenêtre, comme un vrai courtier sans pagination."""

        def __init__(self, taille_max=1200):
            self.taille_max = taille_max
            self.appels = 0

        async def get_candles(self, instrument, granularity, count):
            self.appels += 1
            n = min(count, self.taille_max)
            return [candle(1.0 + i, 1.0 + i, 1.0 + i, 1.0 + i) for i in range(n)]

    client = ClientQuiRepete()
    bougies = asyncio.run(fetch_history(client, "EUR_USD", "H1", 5000))

    assert client.appels == 1, f"{client.appels} requêtes au lieu d'une seule"
    assert len(bougies) == 1200, len(bougies)
    closes = [b.close for b in bougies]
    assert len(closes) == len(set(closes)), "des bougies dupliquées se sont glissées dedans"
    print(f"  {len(bougies)} bougies uniques en 1 requête, aucun doublon")


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
