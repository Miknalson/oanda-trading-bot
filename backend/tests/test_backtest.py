"""Tests du backtester lui-même.

Un backtest qui se trompe donne une fausse confiance et coûte de l'argent
réel. Ces tests vérifient la mécanique avant de croire le moindre chiffre
qu'elle produira.
"""
from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import fetch_history, run_backtest  # noqa: E402
from app.broker import BrokerError, Candle  # noqa: E402


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



class CourtierHistorique:
    """Courtier factice avec un vrai historique daté et borné.

    Reproduit le comportement attendu : `before` ne renvoie que des bougies
    strictement antérieures, et chaque réponse est plafonnée.
    """

    def __init__(self, bougies: int = 10_000, taille_max: int = 1200):
        self.max_candles_per_request = taille_max
        self.appels = 0
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.histoire = [
            Candle(
                open=1.10 + i * 1e-5, high=1.10 + i * 1e-5,
                low=1.10 + i * 1e-5, close=1.10 + i * 1e-5,
                time=(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            for i in range(bougies)
        ]

    async def get_candles(self, instrument, granularity, count, before=None):
        self.appels += 1
        dispo = self.histoire
        if before:
            dispo = [c for c in dispo if c.time < before]
        return dispo[-min(count, self.max_candles_per_request):]


def test_fetch_history_pagine_et_ne_duplique_rien():
    """La pagination doit dépasser le plafond d'une requête, sans doublon."""
    client = CourtierHistorique(bougies=10_000, taille_max=1200)
    bougies = asyncio.run(fetch_history(client, "EUR_USD", "H1", 3000))

    assert len(bougies) == 3000, len(bougies)
    dates = [b.time for b in bougies]
    assert len(set(dates)) == 3000, "des bougies dupliquées se sont glissées dedans"
    assert dates == sorted(dates), "les bougies ne sont pas dans l'ordre chronologique"
    # Les 3000 doivent être les PLUS RÉCENTES de l'historique.
    assert dates[-1] == client.histoire[-1].time
    assert client.appels == 3, f"{client.appels} requêtes pour 3 pages de 1200"
    print(f"  {len(bougies)} bougies uniques et ordonnées en {client.appels} requêtes")


def test_fetch_history_refuse_un_historique_en_doublons():
    """Un courtier qui ignore le bornage doit faire ÉCHOUER la récupération.

    C'est la raison d'être du garde-fou : empiler des fenêtres identiques
    fabriquerait un historique de doublons. Un backtest dessus tourne, sort
    des chiffres crédibles, et ne mesure rien. Mieux vaut une erreur franche.
    """
    class CourtierQuiIgnoreLeBornage(CourtierHistorique):
        async def get_candles(self, instrument, granularity, count, before=None):
            self.appels += 1  # `before` jeté à la poubelle, comme une API v1
            return self.histoire[-min(count, self.max_candles_per_request):]

    client = CourtierQuiIgnoreLeBornage()
    with pytest.raises(BrokerError) as erreur:
        asyncio.run(fetch_history(client, "EUR_USD", "H1", 5000))

    message = str(erreur.value)
    assert "même fenêtre" in message, message
    assert "doublons" in message, message
    print(f"  bornage ignoré -> erreur franche : « {message[:60]}... »")


def test_fetch_history_s_arrete_au_debut_de_l_historique():
    """Demander plus que ce qui existe doit rendre tout, sans boucler sans fin."""
    client = CourtierHistorique(bougies=2_500, taille_max=1200)
    bougies = asyncio.run(fetch_history(client, "EUR_USD", "H1", 99_000))

    assert len(bougies) == 2_500, len(bougies)
    assert len({b.time for b in bougies}) == 2_500
    print(f"  historique de 2500 bougies entièrement remonté en {client.appels} requêtes")


def test_fetch_history_sans_horodatage_ne_pagine_pas():
    """Sans date, il n'y a rien à quoi se borner : une seule page, et on le dit.

    Boucler à l'aveugle redemanderait la même fenêtre — exactement le
    scénario que le garde-fou précédent interdit.
    """
    class CourtierSansDates:
        max_candles_per_request = 1200

        def __init__(self):
            self.appels = 0

        async def get_candles(self, instrument, granularity, count, before=None):
            self.appels += 1
            n = min(count, self.max_candles_per_request)
            return [candle(1.0 + i, 1.0 + i, 1.0 + i, 1.0 + i) for i in range(n)]

    client = CourtierSansDates()
    bougies = asyncio.run(fetch_history(client, "EUR_USD", "H1", 5000))

    assert client.appels == 1, f"{client.appels} requêtes alors qu'aucune date"
    assert len(bougies) == 1200, len(bougies)
    closes = [b.close for b in bougies]
    assert len(closes) == len(set(closes)), "des doublons malgré tout"
    print(f"  pas d'horodatage -> {len(bougies)} bougies en 1 requête, aucun doublon")


def test_le_bornage_demande_est_bien_anterieur():
    """Chaque page doit être demandée avant la plus ancienne déjà connue."""
    class CourtierEspion(CourtierHistorique):
        def __init__(self):
            super().__init__()
            self.bornes = []

        async def get_candles(self, instrument, granularity, count, before=None):
            self.bornes.append(before)
            return await super().get_candles(instrument, granularity, count, before)

    client = CourtierEspion()
    asyncio.run(fetch_history(client, "EUR_USD", "H1", 3000))

    assert client.bornes[0] is None, "la première page ne doit pas être bornée"
    bornes = client.bornes[1:]
    assert bornes == sorted(bornes, reverse=True), (
        f"les bornes ne reculent pas dans le temps : {bornes}"
    )
    print(f"  bornes demandées, du plus récent au plus ancien : {bornes}")


def test_la_fenetre_glissante_ne_change_aucun_resultat():
    """L'optimisation doit être exactement neutre sur les résultats.

    `run_backtest` ne passe plus tout l'historique aux indicateurs mais une
    fenêtre des dernières bougies — la SMA lente et l'ATR ne regardent rien
    de plus loin. Une optimisation qui déplace ne serait-ce qu'un trade
    invaliderait tous les chiffres mesurés jusqu'ici, donc on le vérifie au
    lieu de le supposer.
    """
    from app import backtest as module
    from app.indicators import atr as atr_reel
    from app.indicators import trend_direction as tendance_reelle

    candles = random_walk(1500, seed=21)

    rapide = run_backtest(candles, spread=0.00008, instrument="T", granularity="H1")

    # Rejoue en forçant les indicateurs à voir TOUT l'historique, via une
    # fenêtre assez large pour qu'elle n'en retire rien.
    ancienne = module.INDICATOR_WINDOW
    module.INDICATOR_WINDOW = len(candles)
    try:
        complet = run_backtest(candles, spread=0.00008, instrument="T", granularity="H1")
    finally:
        module.INDICATOR_WINDOW = ancienne

    assert len(rapide.trades) == len(complet.trades), (
        f"{len(rapide.trades)} trades avec fenêtre, {len(complet.trades)} sans"
    )
    assert rapide.wins == complet.wins
    assert abs(rapide.net_pl - complet.net_pl) < 1e-9
    for a, b in zip(rapide.trades, complet.trades):
        assert a.entry_index == b.entry_index
        assert a.direction == b.direction
        assert abs(a.stop_loss - b.stop_loss) < 1e-12
        assert abs(a.take_profit - b.take_profit) < 1e-12

    # Un départ de fenêtre négatif découperait la FIN du tableau, donc des
    # bougies futures. L'invariant qui l'empêche doit tenir.
    assert module.WARMUP + 1 > module.INDICATOR_WINDOW - 1, (
        "la première barre examinée n'a pas assez de passé pour sa fenêtre"
    )

    # Et la fenêtre doit bien couvrir ce dont les indicateurs ont besoin.
    assert module.INDICATOR_WINDOW >= module.SLOW_PERIOD
    assert module.INDICATOR_WINDOW >= module.ATR_PERIOD + 1
    assert atr_reel(candles[:module.INDICATOR_WINDOW], module.ATR_PERIOD) is not None
    assert tendance_reelle(
        [c.close for c in candles[:module.INDICATOR_WINDOW]],
        module.FAST_PERIOD, module.SLOW_PERIOD,
    ) is not None

    print(
        f"  {len(rapide.trades)} trades identiques au trade près, "
        f"fenêtre de {ancienne} bougies contre {len(candles)}"
    )


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


def test_no_trade_names_its_cause_spread():
    """« Aucun trade » doit dire POURQUOI, sinon on le lit à l'envers.

    Un spread trop large et un marché sans tendance produisent la même ligne
    vide, alors que ce sont deux conclusions opposées : « trop cher pour
    entrer » contre « rien à jouer ».
    """
    result = run_backtest(random_walk(3000, seed=3), spread=0.05)
    assert not result.closed
    motif = result.no_trade_reason()
    assert "spread" in motif and "trop cher" in motif, motif
    assert result.skipped_spread_too_wide > 0
    assert result.verdict == "AUCUN TRADE"
    print(f"  {motif}")


def test_no_trade_names_its_cause_insufficient_history():
    result = run_backtest(random_walk(20), spread=0.00012)
    assert result.insufficient_candles
    motif = result.no_trade_reason()
    assert "historique" in motif, motif
    print(f"  {motif}")


def test_skip_counts_add_up():
    """Chaque bougie examinée sans position finit dans exactement un compteur."""
    result = run_backtest(random_walk(2000, seed=11), spread=0.00012)
    entrees = len(result.trades)
    total = result.skipped_no_signal + result.skipped_spread_too_wide + entrees
    assert total == result.bars_examined, (
        f"{total} bougies classées pour {result.bars_examined} examinées"
    )
    print(
        f"  {result.bars_examined} examinées = {entrees} entrées "
        f"+ {result.skipped_no_signal} sans signal "
        f"+ {result.skipped_spread_too_wide} spread trop large"
    )


def test_small_sample_is_not_a_verdict():
    """Un petit échantillon sous le seuil ne doit PAS être déclaré perdant.

    C'est le piège qui a failli nous faire abandonner la stratégie : 65 trades
    à 33,8 % face à un seuil de 41 % semblent accablants, alors qu'un tirage
    aussi mauvais arrive par malchance environ une fois sur sept.
    """
    from app.backtest import BacktestResult, BacktestTrade

    r = BacktestResult(instrument="EUR_USD", granularity="H4", candles=1200,
                       reward_ratio=1.5, risk_amount=2.5)
    for i in range(65):
        t = BacktestTrade("buy", i, 1.10, 1.09, 1.12, 1000.0)
        t.won = i < 22
        t.pl = 3.75 if t.won else -2.50
        r.trades.append(t)

    assert r.net_pl < 0, "ce cas doit bien être perdant en euros"
    assert r.p_value > 0.05, f"p = {r.p_value:.1%}"
    assert not r.is_conclusive
    assert r.verdict == "NON CONCLUANT", r.verdict
    besoin = r.trades_needed()
    assert besoin and besoin > 65, besoin
    print(
        f"  65 trades à {r.win_rate:.1%} (seuil {r.breakeven_win_rate:.1%}) "
        f"-> p = {r.p_value:.1%}, non concluant ; ~{besoin} trades suffiraient"
    )


def test_large_sample_does_conclude():
    """Le même taux sur un gros échantillon, lui, tranche."""
    from app.backtest import BacktestResult, BacktestTrade

    r = BacktestResult(instrument="EUR_USD", granularity="H4", candles=99999,
                       reward_ratio=1.5, risk_amount=2.5)
    for i in range(650):
        t = BacktestTrade("buy", i, 1.10, 1.09, 1.12, 1000.0)
        t.won = i < 220
        t.pl = 3.75 if t.won else -2.50
        r.trades.append(t)

    assert r.is_conclusive, f"p = {r.p_value:.1%}"
    assert r.verdict == "PERDANT", r.verdict
    print(f"  650 trades au même taux -> p = {r.p_value:.2%}, PERDANT")


def test_binomial_matches_exact_coefficients():
    """Le calcul en logarithmes doit égaler le calcul exact, et ne pas déborder.

    `math.comb` renvoie un entier exact : au-delà de quelques centaines de
    tirages il dépasse la capacité d'un flottant et le produit lève
    OverflowError. C'est exactement ce qui s'est produit au premier jet.
    """
    import math

    from app.backtest import binomial_tail_p

    for n, k, p in [(65, 22, 0.40), (10, 3, 0.5), (200, 60, 0.41), (1, 0, 0.3)]:
        exact = sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))
        assert abs(binomial_tail_p(n, k, p) - exact) < 1e-12, (n, k, p)

    # Ne doit pas lever : c'est le cas que trades_needed() atteint.
    assert 0.0 <= binomial_tail_p(4000, 1350, 0.41) <= 1.0

    # Bornes. La somme complète vaut 1 à l'arrondi flottant près : exiger
    # l'égalité stricte serait exiger que l'addition de 101 termes ne perde
    # aucun bit.
    assert abs(binomial_tail_p(100, 100, 0.5) - 1.0) < 1e-9
    assert binomial_tail_p(0, 0, 0.5) == 1.0
    print("  logarithmes == exact, et aucun débordement à 4000 tirages")


def test_p_value_also_works_in_the_winning_direction():
    """Le test de significativité doit couper des deux côtés.

    Un calcul qui ne sait mesurer que la malchance déclarerait concluant
    n'importe quel résultat positif, y compris une série chanceuse de dix
    trades. La queue haute doit être testée avec la même exigence.
    """
    # Spread nul : cette hausse a une amplitude si faible qu'un spread de
    # 1,2 pip y fait refuser TOUTES les entrées — exactement le phénomène
    # observé sur H1 en réel. Ici on veut mesurer la queue haute, pas le
    # filtre de spread.
    gagnant = run_backtest(
        steady_uptrend(3000), spread=0.0, financing_rate_annual=0.0
    )
    assert gagnant.closed
    assert gagnant.win_rate > gagnant.breakeven_win_rate
    assert gagnant.p_value < 0.05, f"p = {gagnant.p_value:.1%}"
    assert gagnant.verdict == "RENTABLE", gagnant.verdict
    assert gagnant.trades_needed() is None  # déjà tranché

    print(
        f"  hausse régulière : {len(gagnant.closed)} trades à "
        f"{gagnant.win_rate:.1%} -> p = {gagnant.p_value:.2%}, RENTABLE"
    )


def test_backtest_spread_ceiling_matches_production():
    """Le plafond du backtest doit égaler celui du serveur.

    S'ils divergent, le backtest mesure une stratégie que le bot ne tradera
    pas — et c'est le genre d'écart qui ne se voit dans aucun chiffre.
    """
    import os

    from app.backtest import MAX_SPREAD_RATIO
    from app.config import get_settings

    ancien = os.environ.pop("MAX_SPREAD_RATIO", None)
    get_settings.cache_clear()
    try:
        production = get_settings().max_spread_ratio
    finally:
        if ancien is not None:
            os.environ["MAX_SPREAD_RATIO"] = ancien
        get_settings.cache_clear()

    assert MAX_SPREAD_RATIO == production, (
        f"backtest {MAX_SPREAD_RATIO} != production {production}"
    )
    print(f"  plafond de spread aligné : {MAX_SPREAD_RATIO:.0%} des deux côtés")
