"""Génère notebooks/backtest_saxo.ipynb.

Le carnet est un artefact : le modifier dans Colab n'a aucun effet ici.
Toute évolution passe par ce script, puis `python scripts/build_notebook.py`.
"""
import json, pathlib

def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)}

def code(text):
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": text.splitlines(keepends=True)}

cells = [
md("""# Backtest de la stratégie — Saxo

Ce carnet répond à **une** question : la stratégie tient-elle debout sur des
données réelles, frais compris ?

Il tourne entièrement dans le navigateur (Google Colab), donc **depuis un
téléphone**. Rien à installer.

Exécute les cellules dans l'ordre avec le bouton ▶.
"""),

md("""## 1. Récupérer le code

Si le dépôt est public, laisse le champ vide et valide.

S'il est privé, colle un *personal access token* GitHub en lecture seule.
"""),
code("""import getpass, importlib, os, shutil, subprocess, sys

REPO = "Miknalson/oanda-trading-bot"
DOSSIER = "oanda-trading-bot"

gh_token = getpass.getpass("Token GitHub (laisse vide si le dépôt est public) : ").strip()
url = f"https://{gh_token}@github.com/{REPO}.git" if gh_token else f"https://github.com/{REPO}.git"

# Relancer cette cellule doit TOUJOURS ramener la dernière version. Sans ça,
# une correction poussée entre deux essais ne serait jamais récupérée : le
# dossier existe déjà, le clone est sauté, et on réexécute l'ancien code en
# croyant tester le nouveau.
if os.path.isdir(DOSSIER):
    subprocess.run(["git", "-C", DOSSIER, "fetch", "--depth", "1", "origin", "main"], check=True)
    subprocess.run(["git", "-C", DOSSIER, "reset", "--hard", "origin/main"], check=True)
else:
    subprocess.run(["git", "clone", "--depth", "1", url, DOSSIER], check=True)

# Trois caches distincts empêchent le code mis à jour d'être réellement
# chargé. Les oublier donne le pire des symptômes : le fichier est corrigé
# sur le disque, et c'est pourtant l'ancienne version qui s'exécute.
#
# 1. Les modules déjà importés.
for nom in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    del sys.modules[nom]

# 2. Les fichiers compilés (.pyc). Python les valide sur la TAILLE et la
#    DATE : deux versions de même longueur écrites dans la même seconde
#    passent pour identiques, et l'ancien code est rechargé tel quel.
for racine, dossiers, _ in os.walk(DOSSIER):
    for d in list(dossiers):
        if d == "__pycache__":
            shutil.rmtree(os.path.join(racine, d), ignore_errors=True)

# 3. Le cache des répertoires du système d'import.
importlib.invalidate_caches()

# Un dépôt vide ou renommé se clone SANS ERREUR : git est content, le dossier
# existe, et la panne n'apparaît que plus loin sous la forme d'un
# « ModuleNotFoundError: app » incompréhensible. On vérifie donc que le code
# est réellement là, et on nomme la cause.
chemin = f"{DOSSIER}/backend"
if not os.path.isfile(f"{chemin}/app/main.py"):
    contenu = sorted(os.listdir(DOSSIER)) if os.path.isdir(DOSSIER) else []
    print(f"❌ Le dépôt {REPO} ne contient pas le code attendu :")
    print(f"   {chemin}/app/main.py est absent.")
    print(f"   Contenu récupéré : {contenu or 'vide'}")
    print("   Vérifie le nom du dépôt dans la variable REPO ci-dessus.")
    raise SystemExit(1)

if chemin not in sys.path:
    sys.path.insert(0, chemin)

subprocess.run([sys.executable, "-m", "pip", "install", "-q", "httpx", "python-dotenv"], check=True)

version = subprocess.run(["git", "-C", DOSSIER, "log", "-1", "--format=%h %s"],
                         capture_output=True, text=True).stdout.strip()
print(f"✅ Code récupéré — {version}")"""),

md("""## 2. Ton jeton Saxo

Récupère-le sur [developer.saxo](https://www.developer.saxo/) — il est valable
**24 heures**.

Il est saisi en masqué et reste en mémoire : il n'apparaît ni dans le carnet,
ni dans les sorties, ni sur le disque.
"""),
code("""import os, getpass

os.environ["BROKER"] = "saxo"
os.environ["SAXO_ENVIRONMENT"] = "sim"
os.environ["SAXO_ACCESS_TOKEN"] = getpass.getpass("Jeton Saxo (24 h) : ").strip()

# La configuration est mise en cache au premier appel. Sans ce vidage, une
# correction de jeton n'aurait aucun effet : tu réexécuterais la cellule
# suivante avec l'ancienne valeur et conclurais à tort que le nouveau jeton
# est mauvais lui aussi.
try:
    from app.config import get_settings
    get_settings.cache_clear()
except ImportError:
    pass  # Première exécution : rien à vider.

print("✅ Jeton enregistré pour cette session")"""),

md("""## 3. Vérifier la connexion

Si cette cellule affiche ton solde, tout le reste fonctionnera. Si elle
échoue, le message dit quoi faire — le cas le plus courant étant un jeton
expiré (ils ne durent que 24 h).
"""),
code("""from app.broker_factory import make_broker
from app.broker import BrokerError

# `await` directement, sans asyncio.run() : Colab fait déjà tourner une
# boucle d'événements, et asyncio.run() refuse de s'exécuter dans ce cas
# (« cannot be called from a running event loop »). Les carnets acceptent
# await au niveau de la cellule, c'est la façon prévue de faire.
try:
    courtier = make_broker()
    compte = await courtier.get_account_summary()
    print(f"Solde : {compte.balance:,.2f} {compte.currency}")

    cours = await courtier.get_quote("EUR_USD")
    print(f"EUR/USD : vente {cours.bid:.5f} / achat {cours.ask:.5f} "
          f"— spread {cours.spread:.5f}")

    print("\\n✅ Connexion établie")
except BrokerError as e:
    print(f"❌ {e}")"""),

md("""## 4. Le backtest

On rejoue la stratégie sur l'historique, bougie par bougie, **sans jamais
regarder le futur** : à chaque instant seules les bougies passées sont
visibles, et l'entrée se fait à l'ouverture de la suivante.

Les deux frais sont comptés : le spread (payé en pertes plus fréquentes) et
le financement (déduit du résultat).

Le chiffre qui décide de tout est le **taux de réussite comparé au seuil
d'équilibre**. En dessous, la stratégie perd de l'argent — quoi qu'en dise
l'impression générale.

Le carnet remonte l'historique **par pages** : Saxo plafonne chaque requête à
1200 bougies, ce qui ne donnait que ~65 trades sur H4 — trop peu pour
conclure quoi que ce soit. La récupération enchaîne donc plusieurs requêtes
en reculant dans le temps, et vérifie à chaque page que le courtier a
réellement reculé (sinon elle s'arrête en erreur, plutôt que d'empiler des
doublons qui donneraient un backtest crédible mesurant du vide).

⚠️ **Le spread ne se relève pas, il se balaie.** Un seul relevé instantané
ne dit rien sur 1200 bougies : il change d'heure en heure, et **marché fermé
il est élargi et figé**. Un premier essai un samedi soir a relevé 5,2 pips
sur EUR/USD (le normal est 0,6 à 1,5) et a fait refuser *toutes* les entrées
sur H1 — ce qui ressemblait à « aucun signal » n'était qu'un spread de
week-end. Le carnet teste donc une fourchette de spreads, et n'utilise le
tien que si le marché est réellement ouvert.
"""),
code("""from app.backtest import run_backtest, fetch_history

INSTRUMENT = "EUR_USD"
INTERVALLES = ["H1", "H4"]   # M1 et M5 sont refusés : le spread y est ruineux
BOUGIES = 6000               # remonté par pages : le plafond par requête
                             # chez Saxo est de 1200, fetch_history enchaîne

cours = await courtier.get_quote(INSTRUMENT)
pips = cours.spread * 10000
etat = "OUVERT" if cours.tradeable else "FERMÉ"
print(f"Spread instantané : {cours.spread:.5f} ({pips:.1f} pip) — marché {etat}")

# Marché fermé, le spread affiché est élargi et figé : l'appliquer à 1200
# bougies de cotations en semaine fausse tout le backtest. C'est exactement
# ce qui s'est produit au premier essai, un samedi soir.
if not cours.tradeable:
    print("⚠️ Marché fermé : ce spread ne représente pas les conditions de")
    print("   trading réelles. Il n'est PAS utilisé ci-dessous.")

# Le spread ne modifie pas un détail du résultat, il le décide. On balaie
# donc une fourchette réaliste pour EUR/USD au lieu de parier sur un relevé.
SPREADS = {
    "0,8 pip (serré)": 0.00008,
    "1,2 pip (courant)": 0.00012,
    "2,0 pip (large)": 0.00020,
}
if cours.tradeable:
    SPREADS[f"{pips:.1f} pip (le tien)"] = cours.spread

# L'historique ne dépend pas du spread : on le récupère une fois par
# intervalle, puis on rejoue la stratégie dessus avec chaque spread.
# Cette étape prend quelques dizaines de secondes : elle enchaîne plusieurs
# requêtes pour remonter au-delà du plafond de 1200 bougies.
historique = {}
for g in INTERVALLES:
    historique[g] = await fetch_history(courtier, INSTRUMENT, g, BOUGIES)
    bougies = historique[g]
    periode = ""
    if bougies and bougies[0].time and bougies[-1].time:
        periode = f" — du {bougies[0].time[:10]} au {bougies[-1].time[:10]}"
    print(f"{g} : {len(bougies)} bougies{periode}")

resultats = []
for g in INTERVALLES:
    for libelle, sp in SPREADS.items():
        r = run_backtest(
            historique[g], instrument=INSTRUMENT, granularity=g,
            spread=sp, reward_ratio=1.5, risk_amount=2.5,
            financing_rate_annual=0.02,
        )
        resultats.append((libelle, r))

for libelle, r in resultats:
    print(f"\\n--- spread {libelle} ---")
    print(r.summary())"""),

md("""## 4 bis. Le signal vaut-il mieux que pile ou face ?

Un backtest qui perd ne dit pas **pourquoi**. Deux causes très différentes
donnent le même résultat négatif : une stratégie qui a un avantage mais que
les frais annulent, ou une stratégie qui n'a aucun avantage du tout.

On les sépare en rejouant les mêmes bougies avec une entrée **tirée au sort**,
tout le reste identique — mêmes instants d'analyse, mêmes stops, mêmes frais.
Seule la règle de direction change.

- Si la stratégie fait nettement mieux que le hasard : il y a un avantage, et
  le combat est contre les frais.
- Si elle fait pareil : le signal ne vaut rien, et le régler ne changera rien.
- Si elle fait pire : le signal est activement nuisible.
"""),
code("""SPREAD_TEST = SPREADS.get("1,2 pip (courant)", 0.00012)
TIRAGES = 5   # plusieurs graines : un seul tirage serait lui-même du hasard

print(f"{'Interv.':<9}{'Stratégie':>11}{'Hasard (moy.)':>15}{'Écart':>9}"
      f"{'P/L strat.':>12}{'P/L hasard':>12}")
print("-" * 68)

for g in INTERVALLES:
    strat = run_backtest(
        historique[g], instrument=INSTRUMENT, granularity=g,
        spread=SPREAD_TEST, reward_ratio=1.5, risk_amount=2.5,
        financing_rate_annual=0.02,
    )
    if not strat.closed:
        print(f"{g:<9}{'aucun trade':>11}")
        continue

    tirages = [
        run_backtest(
            historique[g], instrument=INSTRUMENT, granularity=g,
            spread=SPREAD_TEST, reward_ratio=1.5, risk_amount=2.5,
            financing_rate_annual=0.02, entry_mode="random", seed=graine,
        )
        for graine in range(TIRAGES)
    ]
    taux_hasard = sum(t.win_rate for t in tirages) / TIRAGES
    pl_hasard = sum(t.net_pl for t in tirages) / TIRAGES
    ecart = strat.win_rate - taux_hasard

    print(f"{g:<9}{strat.win_rate:>10.1%}{taux_hasard:>15.1%}{ecart:>+9.1%}"
          f"{strat.net_pl:>+12.2f}{pl_hasard:>+12.2f}")

    if ecart > 0.02:
        print(f"         -> le signal apporte quelque chose sur {g}")
    elif ecart < -0.02:
        print(f"         -> le signal fait PIRE que pile ou face sur {g}")
    else:
        print(f"         -> indiscernable du hasard sur {g} : le signal ne vaut rien")"""),

md("""## 4 ter. Changer de stratégie sans se mentir

Le croisement de moyennes mobiles avec objectif fixe a été mesuré perdant.
Avant d'en essayer une autre, une règle non négociable :

> **Essayer dix stratégies sur les mêmes données et garder la meilleure
> garantit d'en trouver une qui paraît rentable — par pur hasard.**

Exactement comme dix pièces lancées dix fois donnent forcément une belle série
de faces. Cette stratégie-là perdra en réel, et elle aura l'air excellente en
backtest. C'est une erreur bien plus coûteuse que celle qu'on vient d'éviter.

D'où la coupe chronologique : on cherche et on compare sur les **70 %
premiers**, et on n'exécute les **30 % derniers qu'une seule fois**, tout à la
fin. Chaque essai supplémentaire sur la période de validation la transforme en
période de mise au point, et le garde-fou disparaît sans prévenir.

**La première hypothèse testée ici** : le suivi de tendance gagne par quelques
très gros gagnants, et un objectif fixe à 1,5x les coupe systématiquement. On
compare donc l'objectif fixe à un **stop suiveur** qui laisse courir.

Sur données synthétiques, à spread 2 pips : en légère tendance la sortie
suiveuse fait +438 contre +290 avec un taux de réussite plus BAS (44 % contre
50 %) ; sur du bruit pur elle perd quand même, moins. Elle exploite un
avantage, elle n'en fabrique pas.
"""),
code("""from app.backtest import split_history

SPREAD_REEL = SPREADS.get("2,0 pip (large)", 0.00020)

for g in INTERVALLES:
    mise_au_point, validation = split_history(historique[g], validation_fraction=0.3)
    print(f"=== {g} — {len(mise_au_point)} bougies de mise au point, "
          f"{len(validation)} de validation ===")
    print(f"{'période':<14}{'sortie':<11}{'trades':>7}{'réussite':>10}"
          f"{'P/L':>10}{'meilleur':>10}{'p':>8}  verdict")

    for nom_periode, bougies in [("mise au point", mise_au_point),
                                 ("VALIDATION", validation)]:
        for mode in ("fixed", "trailing"):
            r = run_backtest(
                bougies, instrument=INSTRUMENT, granularity=g,
                spread=SPREAD_REEL, reward_ratio=1.5, risk_amount=2.5,
                financing_rate_annual=0.02, exit_mode=mode,
            )
            if not r.closed:
                print(f"{nom_periode:<14}{mode:<11}  aucun trade dénoué "
                      f"({r.no_trade_reason()[:40]})")
                continue
            meilleur = max(t.pl for t in r.closed)
            ouverts = f"  (+{r.still_open} ouvert)" if r.still_open else ""
            print(f"{nom_periode:<14}{mode:<11}{len(r.closed):>7}{r.win_rate:>9.1%}"
                  f"{r.net_pl:>+10.2f}{meilleur:>+10.2f}{r.p_value:>8.1%}"
                  f"  {r.verdict}{ouverts}")
    print()

print("Lis la ligne VALIDATION en dernier, et une seule fois.")
print("Si elle contredit la mise au point, c'est la VALIDATION qui a raison :")
print("c'est la seule période que la stratégie n'a pas eu l'occasion de")
print("flatter. Et si tu relances cette cellule après avoir changé un réglage,")
print("la validation n'en est plus une.")"""),

md("""## 5. Le verdict

Une lecture directe, sans enrobage — mais **prudente sur trois points** qui
font dire n'importe quoi à un backtest :

1. *Aucun trade* n'est pas un résultat. Le carnet dit toujours pourquoi :
   pas assez d'historique, aucune tendance, ou — le cas le plus fréquent —
   un spread trop large pour qu'une entrée soit rentable. « Trop cher pour
   entrer » et « marché calme » se ressemblent et signifient l'inverse.
2. *Perdant sur 65 trades* n'est pas un verdict. Un taux de réussite 7 points
   sous le seuil d'équilibre arrive par simple malchance à peu près une fois
   sur sept. Tant que ce n'est pas tranché, le carnet affiche
   **NON CONCLUANT** et dit combien de trades il faudrait.
3. *Un verdict vaut pour un spread donné.* Lis toujours la ligne avec sa
   colonne Spread. La même stratégie peut être gagnante à 0,8 pip et
   intradable à 2 pips.
"""),
code("""print(f"{'Interv.':<9}{'Spread':<20}{'Trades':>7}{'Réussite':>10}{'Seuil':>8}"
      f"{'P/L':>10}{'Malch.':>8}  Verdict")
print("-" * 86)
for libelle, r in resultats:
    if not r.closed:
        print(f"{r.granularity:<9}{libelle:<20}{'—':>7}{'—':>10}{'—':>8}"
              f"{'—':>10}{'—':>8}  AUCUN TRADE")
        continue
    print(f"{r.granularity:<9}{libelle:<20}{len(r.closed):>7}{r.win_rate:>9.1%}"
          f"{r.breakeven_win_rate:>8.1%}{r.net_pl:>+10.2f}{r.p_value:>8.1%}  {r.verdict}")

# Une ligne vide n'est pas une information : dire pourquoi.
for libelle, r in resultats:
    if not r.closed:
        print(f"\\n{r.granularity} / spread {libelle} — {r.no_trade_reason()}")

print()
concluants = [(l, r) for l, r in resultats if r.is_conclusive]
gagnants = [(l, r) for l, r in concluants if r.expectancy > 0]
perdants = [(l, r) for l, r in concluants if r.expectancy <= 0]
indecis = [(l, r) for l, r in resultats if r.closed and not r.is_conclusive]

if gagnants:
    print("Rentable de façon statistiquement nette :")
    for l, r in gagnants:
        print(f"  {r.granularity} à {l}")
    print("⚠️ Une période favorable ne prouve pas une stratégie : teste sur")
    print("   plusieurs instruments et plusieurs années avant d'y mettre un euro.")
    print("   Et vérifie le spread que ton courtier facture VRAIMENT en semaine.")

if perdants:
    print("Perdant de façon statistiquement nette :")
    for l, r in perdants:
        print(f"  {r.granularity} à {l}")
    print("C'est une réponse utile : tu l'as obtenue sans risquer un centime.")

if indecis:
    print("Indécis — l'échantillon ne permet pas de conclure :")
    for l, r in indecis:
        besoin = r.trades_needed()
        combien = f"il en faudrait ~{besoin}" if besoin else "bien davantage"
        pluriel = "s" if len(r.closed) > 1 else ""
        print(f"  {r.granularity} à {l} : {len(r.closed)} trade{pluriel}, {combien}.")
    print("Ne conclus rien de ces lignes, ni en bien ni en mal. Pour trancher il")
    print("faut plus d'historique (pagination par date, voir fetch_history) ou")
    print("plusieurs instruments.")

if not concluants and not indecis:
    print("Aucun trade nulle part — regarde les motifs ci-dessus avant de")
    print("changer quoi que ce soit à la stratégie.")

# La leçon du balayage, à ne pas rater. On ne compare que des échantillons
# comparables : un taux mesuré sur 2 trades n'est pas un taux, et le mettre
# en face d'un autre mesuré sur 120 fabriquerait un écart qui n'existe pas.
COMPARABLE = 30
taux = [r.win_rate for _, r in resultats if len(r.closed) >= COMPARABLE]
if len(taux) > 1 and max(taux) - min(taux) > 0.02:
    print()
    print(f"Le spread seul déplace le taux de réussite de {min(taux):.1%} à "
          f"{max(taux):.1%}.")
    print("Ce n'est pas un détail de réglage : c'est le facteur dominant.")"""),
]

nb = {
    "cells": cells,
    "metadata": {
        "colab": {"provenance": []},
        "kernelspec": {"display_name": "Python 3", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

path = pathlib.Path("/home/user/oanda-trading-bot/notebooks/backtest_saxo.ipynb")
path.write_text(json.dumps(nb, ensure_ascii=False, indent=1))
print(f"écrit : {path}")
