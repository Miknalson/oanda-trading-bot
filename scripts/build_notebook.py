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

chemin = f"{DOSSIER}/backend"
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
"""),
code("""from app.backtest import run_backtest, fetch_history

INSTRUMENT = "EUR_USD"
INTERVALLES = ["H1", "H4"]   # M1 et M5 sont refusés : le spread y est ruineux
BOUGIES = 1200               # plafond par requête chez Saxo

resultats = []
for g in INTERVALLES:
    bougies = await fetch_history(courtier, INSTRUMENT, g, BOUGIES)
    cours = await courtier.get_quote(INSTRUMENT)
    r = run_backtest(
        bougies, instrument=INSTRUMENT, granularity=g,
        spread=cours.spread, reward_ratio=1.5, risk_amount=2.5,
        financing_rate_annual=0.02,
    )
    resultats.append(r)
    print(r.summary())
    print()"""),

md("""## 5. Le verdict

Une lecture directe, sans enrobage — mais **prudente sur deux points** qui
font dire n'importe quoi à un backtest :

1. *Aucun trade* n'est pas un résultat. Le carnet dit toujours pourquoi :
   pas assez d'historique, aucune tendance, ou — le cas le plus fréquent —
   un spread trop large pour qu'une entrée soit rentable. « Trop cher pour
   entrer » et « marché calme » se ressemblent et signifient l'inverse.
2. *Perdant sur 65 trades* n'est pas un verdict. Un taux de réussite 7 points
   sous le seuil d'équilibre arrive par simple malchance à peu près une fois
   sur sept. Tant que ce n'est pas tranché, le carnet affiche
   **NON CONCLUANT** et dit combien de trades il faudrait.
"""),
code("""print(f"{'Intervalle':<12}{'Trades':>8}{'Réussite':>11}{'Seuil':>9}{'P/L':>11}{'Malchance':>11}  Verdict")
print("-" * 74)
for r in resultats:
    if not r.closed:
        print(f"{r.granularity:<12}{'—':>8}{'—':>11}{'—':>9}{'—':>11}{'—':>11}  AUCUN TRADE")
        continue
    print(f"{r.granularity:<12}{len(r.closed):>8}{r.win_rate:>10.1%}"
          f"{r.breakeven_win_rate:>9.1%}{r.net_pl:>+11.2f}{r.p_value:>10.1%}  {r.verdict}")

# Une ligne vide n'est pas une information : dire pourquoi.
for r in resultats:
    if not r.closed:
        print(f"\\n{r.granularity} — aucun trade : {r.no_trade_reason()}")

print()
concluants = [r for r in resultats if r.is_conclusive]
gagnants = [r for r in concluants if r.expectancy > 0]
perdants = [r for r in concluants if r.expectancy <= 0]
indecis = [r for r in resultats if r.closed and not r.is_conclusive]

if gagnants:
    noms = ", ".join(r.granularity for r in gagnants)
    print(f"Rentable de façon statistiquement nette : {noms}.")
    print("⚠️ Une période favorable ne prouve pas une stratégie : teste sur")
    print("   plusieurs instruments et plusieurs années avant d'y mettre un euro.")

if perdants:
    noms = ", ".join(r.granularity for r in perdants)
    print(f"Perdant de façon statistiquement nette : {noms}.")
    print("C'est une réponse utile : tu l'as obtenue sans risquer un centime.")

if indecis:
    print("Indécis — l'échantillon ne permet pas de conclure :")
    for r in indecis:
        besoin = r.trades_needed()
        combien = f"il en faudrait ~{besoin}" if besoin else "bien davantage"
        print(f"  {r.granularity} : {len(r.closed)} trades seulement, {combien}.")
    print("Ne conclus rien de ces lignes, ni en bien ni en mal. Pour trancher il")
    print("faut plus d'historique (pagination par date, voir fetch_history) ou")
    print("plusieurs instruments.")

if not concluants and not indecis:
    print("Aucun trade nulle part — regarde les motifs ci-dessus avant de")
    print("changer quoi que ce soit à la stratégie.")"""),
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
