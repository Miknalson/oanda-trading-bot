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
code("""import getpass, subprocess, os, sys

REPO = "Miknalson/oanda-trading-bot"

gh_token = getpass.getpass("Token GitHub (laisse vide si le dépôt est public) : ").strip()
url = f"https://{gh_token}@github.com/{REPO}.git" if gh_token else f"https://github.com/{REPO}.git"

if not os.path.isdir("oanda-trading-bot"):
    subprocess.run(["git", "clone", "--depth", "1", url], check=True)

sys.path.insert(0, "oanda-trading-bot/backend")
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "httpx", "python-dotenv"], check=True)
print("✅ Code récupéré")"""),

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
code("""import asyncio
from app.broker_factory import make_broker
from app.broker import BrokerError

async def verifier():
    courtier = make_broker()
    compte = await courtier.get_account_summary()
    print(f"Solde : {compte.balance:,.2f} {compte.currency}")
    cours = await courtier.get_quote("EUR_USD")
    print(f"EUR/USD : vente {cours.bid:.5f} / achat {cours.ask:.5f} "
          f"— spread {cours.spread:.5f}")
    return courtier

try:
    courtier = asyncio.run(verifier())
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

async def backtester():
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
        print()
    return resultats

resultats = asyncio.run(backtester())"""),

md("""## 5. Le verdict

Une lecture directe, sans enrobage.
"""),
code("""print(f"{'Intervalle':<12}{'Trades':>8}{'Réussite':>11}{'Seuil':>9}{'P/L':>11}  Verdict")
print("-" * 62)
for r in resultats:
    if not r.closed:
        print(f"{r.granularity:<12}{'aucun trade sur la période':>48}")
        continue
    verdict = "RENTABLE" if r.expectancy > 0 else "PERDANT"
    print(f"{r.granularity:<12}{len(r.closed):>8}{r.win_rate:>10.1%}"
          f"{r.breakeven_win_rate:>9.1%}{r.net_pl:>+11.2f}  {verdict}")

print()
gagnants = [r for r in resultats if r.closed and r.expectancy > 0]
if gagnants:
    print("Au moins un réglage est rentable sur cette période.")
    print("⚠️ Une période favorable ne prouve rien : teste sur plusieurs")
    print("   instruments et plusieurs années avant d'en conclure quoi que ce soit.")
else:
    print("Aucun réglage n'est rentable sur cette période.")
    print("C'est une réponse utile : tu l'as obtenue sans risquer un centime.")
    print("La suite consisterait à tester d'autres stratégies — le backtester")
    print("accepte n'importe quelle logique de signal.")"""),
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
