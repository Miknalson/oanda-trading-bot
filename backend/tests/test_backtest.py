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
    """L'optimisation doit être exactement neutre sur les indicateurs.

    `run_backtest` ne passe plus tout l'historique aux indicateurs mais une
    fenêtre des dernières bougies : la SMA lente en regarde 50, l'ATR 15. Le
    reste ne change rien à leur valeur — c'est cette affirmation qu'on teste.

    On la vérifie au niveau des indicateurs eux-mêmes plutôt qu'en comparant
    deux backtests complets. Deux exécutions démarrant à des amorçages
    différents ne sont pas synchronisées : à la frontière l'une détient déjà
    une position et l'autre non, et les entrées suivantes divergent pour une
    raison qui n'a rien à voir avec la fenêtre.
    """
    from app import backtest as module
    from app.indicators import atr as atr_reel
    from app.indicators import trend_direction as tendance_reelle

    candles = random_walk(1500, seed=21)
    W = module.INDICATOR_WINDOW
    verifies = 0

    for i in range(module.WARMUP, len(candles), 37):
        complet = candles[: i + 1]
        fenetre = candles[max(0, i + 1 - W) : i + 1]

        a, b = atr_reel(fenetre, module.ATR_PERIOD), atr_reel(complet, module.ATR_PERIOD)
        assert a is not None and b is not None
        assert abs(a - b) < 1e-15, f"ATR diffère à la bougie {i} : {a} contre {b}"

        ta = tendance_reelle([c.close for c in fenetre],
                             module.FAST_PERIOD, module.SLOW_PERIOD)
        tb = tendance_reelle([c.close for c in complet],
                             module.FAST_PERIOD, module.SLOW_PERIOD)
        assert ta == tb, f"direction diffère à la bougie {i} : {ta} contre {tb}"
        verifies += 1

    assert verifies > 20, f"seulement {verifies} points vérifiés"

    # La fenêtre doit couvrir ce dont les indicateurs ont besoin, et l'amorçage
    # doit lui être au moins égal : sinon le découpage partirait d'un indice
    # négatif, donc de la FIN du tableau — des bougies futures.
    assert W >= module.SLOW_PERIOD
    assert W >= module.ATR_PERIOD + 1
    r = run_backtest(candles, spread=0.00008, granularity="H1")
    assert r.warmup_used >= W, (
        f"amorçage {r.warmup_used} inférieur à la fenêtre {W} : "
        f"la première barre examinée n'a pas assez de passé"
    )
    print(f"  {verifies} points vérifiés : fenêtre de {W} bougies == historique complet")


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


def test_l_entree_aleatoire_est_reproductible():
    """Même graine, même résultat — sinon la comparaison n'est pas honnête."""
    candles = random_walk(2000, seed=31)
    a = run_backtest(candles, spread=0.00008, entry_mode="random", seed=7)
    b = run_backtest(candles, spread=0.00008, entry_mode="random", seed=7)
    c = run_backtest(candles, spread=0.00008, entry_mode="random", seed=8)

    assert [t.direction for t in a.trades] == [t.direction for t in b.trades]
    assert [t.direction for t in a.trades] != [t.direction for t in c.trades], (
        "deux graines différentes donnent les mêmes tirages"
    )
    print(f"  graine 7 reproductible, graine 8 différente ({len(a.trades)} trades)")


def test_l_etalon_aleatoire_ne_change_que_la_regle_de_direction():
    """Seule la règle de direction doit différer — pas le terrain de jeu.

    Les instants d'entrée ne restent PAS identiques, et ils ne le peuvent
    pas : dès qu'un trade se dénoue différemment, la position se libère à un
    autre moment et l'entrée suivante se décale. Seule la première entrée est
    commune. Ce qui doit rester identique, c'est le reste : mêmes bougies,
    même filtre de spread, même dimensionnement.
    """
    candles = random_walk(2000, seed=33)
    signal = run_backtest(candles, spread=0.00008, entry_mode="signal")
    hasard = run_backtest(candles, spread=0.00008, entry_mode="random", seed=3)

    assert signal.trades and hasard.trades
    assert signal.trades[0].entry_index == hasard.trades[0].entry_index, (
        "la première entrée diffère : ce n'est plus le même point de départ"
    )
    assert signal.candles == hasard.candles
    assert signal.risk_amount == hasard.risk_amount
    assert signal.max_spread_ratio == hasard.max_spread_ratio
    assert all(t.units > 0 for t in hasard.trades)

    # Les deux doivent trader dans le même ordre de grandeur, sinon on
    # comparerait une stratégie active à une stratégie quasi absente.
    ecart = abs(len(signal.trades) - len(hasard.trades)) / len(signal.trades)
    assert ecart < 0.35, (
        f"{len(signal.trades)} trades avec signal contre {len(hasard.trades)} "
        f"au hasard : volumes trop différents pour comparer"
    )
    print(
        f"  même départ et mêmes règles de coût ; {len(signal.trades)} contre "
        f"{len(hasard.trades)} trades ({ecart:.0%} d'écart de volume)"
    )


def test_sur_une_vraie_tendance_le_signal_bat_le_hasard():
    """Contrôle positif : l'étalon doit savoir détecter un signal QUI MARCHE.

    Sans ce test, un étalon cassé — qui renverrait toujours « pas de
    différence » — ferait conclure à tort qu'aucune stratégie ne vaut rien.
    """
    candles = steady_uptrend(3000)
    signal = run_backtest(candles, spread=0.0, entry_mode="signal",
                          financing_rate_annual=0.0)
    hasard = run_backtest(candles, spread=0.0, entry_mode="random", seed=5,
                          financing_rate_annual=0.0)

    assert signal.win_rate > hasard.win_rate + 0.20, (
        f"signal {signal.win_rate:.1%} contre hasard {hasard.win_rate:.1%} : "
        f"l'étalon ne distingue pas une tendance franche"
    )
    assert signal.net_pl > hasard.net_pl
    print(
        f"  hausse franche : signal {signal.win_rate:.1%} contre hasard "
        f"{hasard.win_rate:.1%} -> l'étalon détecte bien un vrai avantage"
    )


def test_sur_une_marche_aleatoire_le_signal_ne_bat_pas_le_hasard():
    """Contrôle négatif : sans avantage exploitable, les deux se valent.

    C'est la mesure qui compte pour le verdict réel : si la stratégie fait
    comme le hasard sur les vraies données, le signal n'apporte rien et le
    régler davantage ne changera rien.
    """
    candles = random_walk(5000, seed=41)
    signal = run_backtest(candles, spread=0.00008, financing_rate_annual=0.0)
    tirages = [
        run_backtest(candles, spread=0.00008, entry_mode="random", seed=g,
                     financing_rate_annual=0.0)
        for g in range(5)
    ]
    moyenne = sum(t.win_rate for t in tirages) / len(tirages)

    assert abs(signal.win_rate - moyenne) < 0.05, (
        f"signal {signal.win_rate:.1%} contre {moyenne:.1%} au hasard : écart "
        f"inattendu sur des données sans tendance exploitable"
    )
    print(
        f"  marche aléatoire : signal {signal.win_rate:.1%} contre "
        f"{moyenne:.1%} au hasard (5 tirages) -> aucun avantage, comme attendu"
    )


def trend_with_pullbacks(n, seed=5, drift=0.00025, noise=0.0006):
    """Hausse nette MAIS avec des replis — un marché en tendance réaliste.

    Une hausse parfaitement monotone ne ferme jamais un stop suiveur : il
    remonte indéfiniment sans être touché, et aucun trade ne se dénoue. Il
    faut des respirations pour que la sortie suiveuse se déclenche.
    """
    rng = random.Random(seed)
    candles, price = [], 1.10
    for _ in range(n):
        o = price
        c = price + drift + rng.gauss(0, noise)
        candles.append(candle(
            o, max(o, c) + abs(rng.gauss(0, noise / 2)),
            min(o, c) - abs(rng.gauss(0, noise / 2)), c,
        ))
        price = c
    return candles


def test_le_stop_suiveur_laisse_courir_les_gagnants():
    """C'est toute la raison d'être du mode : ne plus couper à 1,5x.

    L'objectif fixe encaisse 1,5x le risque et sort. Le stop suiveur doit
    capter bien davantage sur les vraies tendances — sinon le mode ne sert
    à rien.
    """
    candles = trend_with_pullbacks(3000)
    fixe = run_backtest(candles, spread=0.0, financing_rate_annual=0.0)
    suiveur = run_backtest(candles, spread=0.0, financing_rate_annual=0.0,
                           exit_mode="trailing")

    assert fixe.closed and suiveur.closed
    meilleur_fixe = max(t.pl for t in fixe.closed)
    meilleur_suiveur = max(t.pl for t in suiveur.closed)
    assert meilleur_suiveur > meilleur_fixe * 2, (
        f"meilleur gain suiveur {meilleur_suiveur:.2f} contre {meilleur_fixe:.2f} "
        f"en fixe : le stop suiveur ne laisse pas courir"
    )
    print(
        f"  hausse franche : meilleur gain {meilleur_fixe:.2f} (fixe) -> "
        f"{meilleur_suiveur:.2f} (suiveur)"
    )


def test_le_stop_suiveur_ne_regarde_pas_le_futur():
    """Le stop doit remonter APRÈS le test de déclenchement, jamais avant.

    Remonter le stop avec le plus haut de la bougie en cours, puis tester le
    déclenchement dans cette même bougie, reviendrait à connaître le sommet
    avant de l'avoir vécu. Ce serait invisible dans les chiffres, sauf qu'ils
    seraient trop beaux.

    Vérification : les décisions passées ne doivent pas changer quand on
    ajoute des bougies futures.
    """
    complet = random_walk(1200, seed=77)
    court = complet[:900]

    entier = run_backtest(complet, spread=0.00008, exit_mode="trailing",
                          financing_rate_annual=0.0)
    tronque = run_backtest(court, spread=0.00008, exit_mode="trailing",
                           financing_rate_annual=0.0)

    # Les trades entièrement dénoués avant la coupure doivent être identiques.
    clos_avant = [t for t in tronque.closed if t.exit_index is not None
                  and t.exit_index < 880]
    assert clos_avant, "aucun trade dénoué avant la coupure : test sans portée"

    par_entree = {t.entry_index: t for t in entier.trades}
    for t in clos_avant:
        jumeau = par_entree.get(t.entry_index)
        assert jumeau is not None, f"trade à {t.entry_index} absent du run complet"
        assert jumeau.exit_index == t.exit_index, (
            f"trade entré à {t.entry_index} : sortie {t.exit_index} sur données "
            f"tronquées, {jumeau.exit_index} sur données complètes"
        )
        assert abs(jumeau.pl - t.pl) < 1e-9
    print(f"  {len(clos_avant)} trades identiques avec et sans les bougies futures")


def test_le_stop_suiveur_ne_descend_jamais():
    """Un stop suiveur qui redescend transformerait une perte bornée en gouffre."""
    candles = random_walk(2000, seed=51)
    r = run_backtest(candles, spread=0.00008, exit_mode="trailing",
                     financing_rate_annual=0.0)
    assert r.closed

    for t in r.closed:
        perte_initiale = -t.pl_if_loss  # risque de départ, positif
        assert t.pl >= -perte_initiale * 1.01, (
            f"perte de {t.pl:.2f} alors que le risque initial valait "
            f"{perte_initiale:.2f} : le stop a reculé"
        )
    pires = sorted(t.pl for t in r.closed)[:3]
    print(f"  {len(r.closed)} trades, pires pertes : "
          f"{', '.join(f'{p:.2f}' for p in pires)} (risque initial 2,50)")


def test_le_bootstrap_remplace_le_binomial_en_sortie_suiveuse():
    """Le seuil d'équilibre n'a plus de sens quand les gains sont inégaux."""
    from app.backtest import bootstrap_p_value

    candles = random_walk(3000, seed=61)
    suiveur = run_backtest(candles, spread=0.00008, exit_mode="trailing",
                           financing_rate_annual=0.0)
    assert suiveur.closed

    attendu = bootstrap_p_value([t.pl for t in suiveur.closed])
    assert abs(suiveur.p_value - attendu) < 1e-12, "le bootstrap n'est pas utilisé"
    assert "bootstrap" in suiveur.summary()
    assert "seuil sans objet" in suiveur.summary()
    assert suiveur.trades_needed() is None, (
        "trades_needed projette depuis le taux de réussite : sans objet ici"
    )

    # Et les tailles de gains doivent bien être inégales, sinon le test ne
    # porte sur rien.
    gains = [t.pl for t in suiveur.closed if t.pl > 0]
    assert len(set(round(g, 4) for g in gains)) > len(gains) * 0.5, (
        "les gains sont tous identiques : ce n'est pas une sortie suiveuse"
    )
    print(f"  bootstrap utilisé, p = {suiveur.p_value:.1%}, "
          f"{len(set(round(g, 2) for g in gains))} tailles de gains distinctes")


def test_le_bootstrap_detecte_une_esperance_clairement_positive():
    """Contrôle du test lui-même, dans les deux sens."""
    from app.backtest import bootstrap_p_value

    gagnant = [10.0] * 30 + [-2.0] * 70     # espérance +0,6
    perdant = [2.0] * 70 + [-10.0] * 30     # espérance -1,6
    neutre = [1.0] * 50 + [-1.0] * 50       # espérance 0

    p_gagnant = bootstrap_p_value(gagnant)
    p_perdant = bootstrap_p_value(perdant)
    p_neutre = bootstrap_p_value(neutre)

    assert p_gagnant < 0.05, p_gagnant
    assert p_perdant < 0.05, p_perdant
    assert p_neutre > 0.20, p_neutre
    print(f"  espérance +0,6 -> p={p_gagnant:.1%} | -1,6 -> p={p_perdant:.1%} | "
          f"0 -> p={p_neutre:.1%}")


def test_la_coupe_est_chronologique_et_sans_recouvrement():
    """Mélanger les bougies laisserait des morceaux du futur dans la mise au point."""
    from app.backtest import split_history

    candles = random_walk(1000, seed=91)
    mise_au_point, validation = split_history(candles, validation_fraction=0.3)

    assert len(mise_au_point) == 700
    assert len(validation) == 300
    assert mise_au_point + validation == candles, "l'ordre n'est pas préservé"
    # Aucune bougie ne doit apparaître des deux côtés.
    assert mise_au_point[-1] is candles[699]
    assert validation[0] is candles[700]
    print(f"  {len(mise_au_point)} bougies de mise au point puis "
          f"{len(validation)} de validation, dans l'ordre")


def test_la_coupe_refuse_une_fraction_absurde():
    from app.backtest import split_history

    for mauvaise in (0.0, 1.0, -0.2, 1.5):
        try:
            split_history(random_walk(200), validation_fraction=mauvaise)
            raise AssertionError(f"{mauvaise} accepté")
        except ValueError:
            pass
    print("  fractions hors ]0,1[ refusées")


def test_chaque_filtre_ajoute_reduit_le_nombre_de_trades():
    """La sélectivité doit être réelle, pas décorative.

    Le diagnostic mesuré était « on prend presque tout ce qui passe » : une
    entrée toutes les douze bougies. Si ajouter des filtres ne réduisait pas
    le nombre d'entrées, ils ne filtreraient rien.
    """
    candles = random_walk(4000, seed=17)
    jeux = [
        ("trend",),
        ("trend", "long_trend"),
        ("trend", "long_trend", "breakout"),
        ("trend", "long_trend", "breakout", "volatility"),
    ]
    comptes = []
    for jeu in jeux:
        r = run_backtest(candles, spread=0.00008, filters=jeu, granularity="H1")
        comptes.append(len(r.trades))
        print(f"  {len(jeu)} filtre(s) {str(list(jeu)):<50} -> {len(r.trades):>4} trades")

    for precedent, suivant in zip(comptes, comptes[1:]):
        assert suivant <= precedent, (
            f"ajouter un filtre a AUGMENTÉ le nombre de trades : "
            f"{precedent} -> {suivant}"
        )
    assert comptes[-1] < comptes[0] / 2, (
        f"quatre filtres ne retirent que {comptes[0] - comptes[-1]} entrées "
        f"sur {comptes[0]} : la sélectivité est décorative"
    )


def test_le_motif_de_rejet_nomme_le_filtre_responsable():
    """« Aucun trade » doit dire QUEL filtre a tout bloqué."""
    candles = random_walk(3000, seed=23)
    r = run_backtest(candles, spread=0.00008,
                     filters=("trend", "long_trend", "breakout", "volatility"),
                     granularity="H1")
    assert r.rejected_by, "aucun rejet compté alors que des filtres sont actifs"
    total = sum(r.rejected_by.values()) + len(r.trades) + r.skipped_no_signal \
        + r.skipped_spread_too_wide
    assert total == r.bars_examined, (
        f"{total} bougies classées pour {r.bars_examined} examinées"
    )
    print(f"  rejets par filtre : {dict(sorted(r.rejected_by.items()))}")


def test_le_filtre_trend_est_obligatoire():
    """Sans lui, rien ne donne le SENS du trade."""
    try:
        run_backtest(random_walk(500), filters=("long_trend", "breakout"))
        raise AssertionError("filtres sans 'trend' acceptés")
    except ValueError as exc:
        assert "trend" in str(exc)
    print("  filtres sans 'trend' -> refusés avec explication")


def test_un_filtre_inconnu_est_refuse():
    try:
        run_backtest(random_walk(500), filters=("trend", "rvol"))
        raise AssertionError("filtre inconnu accepté")
    except ValueError as exc:
        assert "rvol" in str(exc)
    print("  filtre inconnu -> refusé en le nommant")


def test_l_amorcage_suit_le_filtre_le_plus_gourmand():
    """Un filtre à 200 périodes exige 200 bougies avant de pouvoir répondre.

    L'oublier le rendrait systématiquement faux avec une fenêtre de 50, et le
    backtest conclurait « aucun trade » pour une raison sans rapport avec le
    marché.
    """
    candles = random_walk(1000, seed=29)
    court = run_backtest(candles, spread=0.00008, filters=("trend",))
    long = run_backtest(candles, spread=0.00008,
                        filters=("trend", "long_trend"))

    assert long.warmup_used >= 200, long.warmup_used
    assert long.warmup_used > court.warmup_used

    # Et avec trop peu d'historique, le message doit nommer les filtres.
    maigre = run_backtest(candles[:150], spread=0.00008,
                          filters=("trend", "long_trend"))
    assert maigre.insufficient_candles
    motif = maigre.no_trade_reason()
    assert "long_trend" in motif, motif
    assert str(maigre.warmup_used + 1) in motif, motif
    print(f"  amorçage : {court.warmup_used} (trend) -> {long.warmup_used} "
          f"(+ long_trend)")
