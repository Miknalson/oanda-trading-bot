"""Filtres d'entrée cumulatifs — la sélectivité.

Le diagnostic mesuré sur données réelles était sans appel : notre signal
unique (croisement de moyennes mobiles) donne un taux de réussite
indiscernable du pile ou face, et il déclenche une entrée toutes les douze
bougies. Autrement dit on prenait presque tout ce qui passait.

L'idée reprise ici est celle d'une stratégie d'actions vue en vidéo : ne pas
chercher un signal fréquent, mais les rares moments où PLUSIEURS conditions
indépendantes s'alignent. Ses cinq filtres cumulés ne laissent passer qu'une
ou deux candidates par jour sur cinq cents actions.

Transposition honnête vers le Forex — deux de ses filtres ne passent pas :

- **Gap d'ouverture ≥ 3 %** : le Forex ne fait pas de gaps significatifs, sauf
  au week-end. Abandonné, il ne mesurerait rien.
- **Volume relatif ≥ 2x** : nos bougies n'ont PAS de volume (voir `Candle`),
  et le volume du Forex de gré à gré n'est de toute façon pas centralisé.
  Remplacé par une expansion de volatilité, qui capte la même intuition —
  « il se passe quelque chose d'inhabituel maintenant » — mais ce n'est pas
  la même mesure et il ne faut pas le lire comme si c'en était une.

Chaque filtre reçoit la fenêtre des bougies PASSÉES uniquement, et le sens
proposé par le signal de direction. Il répond : est-ce que ça confirme ?
"""
from __future__ import annotations

from .broker import Candle
from .indicators import atr, sma

# Périodes de regard en arrière de chaque filtre. Servent à dimensionner la
# fenêtre d'historique : un filtre qui regarde 200 bougies ne peut pas être
# évalué avant d'en avoir 200.
FILTER_PERIODS: dict[str, int] = {
    "trend": 50,        # la SMA lente du signal de direction
    "long_trend": 200,  # tendance de fond
    "breakout": 20,     # cassure d'un plus haut récent
    "volatility": 50,   # expansion par rapport à la volatilité habituelle
}

# Seuil du filtre de volatilité : l'ATR courant doit valoir au moins ce
# multiple de sa moyenne longue. Équivalent d'esprit du « volume relatif 2x ».
VOLATILITY_EXPANSION = 1.2

# Période courte de l'ATR pour mesurer la volatilité « de maintenant ».
VOLATILITY_FAST = 14


def filtre_long_trend(fenetre: list[Candle], direction: str) -> bool:
    """La tendance de fond va-t-elle dans le même sens ?

    Équivalent de « la clôture doit être au-dessus de la moyenne 200 jours » :
    on ne prend une position que dans le sens du courant dominant.
    """
    closes = [c.close for c in fenetre]
    moyenne = sma(closes, FILTER_PERIODS["long_trend"])
    if moyenne is None:
        return False
    return closes[-1] > moyenne if direction == "buy" else closes[-1] < moyenne


def filtre_breakout(fenetre: list[Candle], direction: str) -> bool:
    """Le prix vient-il de franchir son extrême récent ?

    Équivalent de « au-dessus du plus haut de la veille et du plus haut du
    jour ». On exige que le marché soit en train d'aller quelque part, pas
    seulement qu'une moyenne soit au-dessus d'une autre.

    Le dernier extrême est calculé SANS la bougie courante : la comparer à
    elle-même la rendrait vraie par construction.
    """
    periode = FILTER_PERIODS["breakout"]
    if len(fenetre) < periode + 1:
        return False
    precedentes = fenetre[-(periode + 1):-1]
    dernier = fenetre[-1].close
    if direction == "buy":
        return dernier > max(c.high for c in precedentes)
    return dernier < min(c.low for c in precedentes)


def filtre_volatility(fenetre: list[Candle], direction: str) -> bool:
    """Le marché bouge-t-il plus que d'habitude en ce moment ?

    Remplaçant du « volume relatif ≥ 2x » : nos bougies n'ont pas de volume.
    L'intuition conservée est « il se passe quelque chose d'inhabituel », mais
    volatilité et volume ne sont pas la même chose — un marché peut s'agiter
    sans participation, et inversement.

    Le sens n'intervient pas : une expansion de volatilité n'a pas de
    direction.
    """
    court = atr(fenetre[-(VOLATILITY_FAST + 1):], VOLATILITY_FAST)
    long = atr(fenetre, FILTER_PERIODS["volatility"])
    if not court or not long or long <= 0:
        return False
    return court / long >= VOLATILITY_EXPANSION


# Le filtre « trend » n'est pas ici : c'est lui qui DONNE la direction, il est
# évalué en amont dans le backtest. Les autres la confirment ou la refusent.
CONFIRMATIONS = {
    "long_trend": filtre_long_trend,
    "breakout": filtre_breakout,
    "volatility": filtre_volatility,
}

AVAILABLE = ("trend", *CONFIRMATIONS)


def window_needed(filters: tuple[str, ...]) -> int:
    """Bougies d'historique nécessaires pour évaluer ces filtres.

    Sans ce calcul, activer un filtre à 200 périodes avec une fenêtre de 50
    le rendrait systématiquement faux — et le backtest conclurait « aucun
    trade » pour une raison qui n'a rien à voir avec le marché.
    """
    inconnus = [f for f in filters if f not in AVAILABLE]
    if inconnus:
        raise ValueError(
            f"filtres inconnus : {inconnus}. Disponibles : {list(AVAILABLE)}"
        )
    return max(FILTER_PERIODS[f] for f in filters) if filters else 0
