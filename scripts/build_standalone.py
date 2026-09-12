"""Génère une cellule Colab autonome à partir des sources du projet.

Le carnet qui clone le dépôt suppose que Colab a accès à un dépôt privé, ce
qui n'est pas toujours le cas. Cette cellule-là ne dépend de rien : elle est
FABRIQUÉE à partir des vrais modules, donc elle ne peut pas diverger du code
testé. Ne jamais l'écrire à la main — relancer ce script.

    python scripts/build_standalone.py
"""
from __future__ import annotations

import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / "backend" / "app"
SORTIE = pathlib.Path(__file__).resolve().parents[1] / "notebooks" / "cellule_autonome.py"

# Ordre de dépendance. config.py est remplacé par une version minimale :
# l'original lit un fichier .env qui n'existe pas dans Colab.
MODULES = ["broker.py", "indicators.py", "analysis.py", "backtest.py", "saxo_client.py"]

CONFIG_MINIMAL = '''
# --- Configuration minimale (remplace config.py, qui lit un fichier .env) ---
@dataclass(frozen=True)
class Settings:
    saxo_access_token: str = ""
    saxo_environment: str = "sim"
    max_risk_pct: float = 0.02
    max_spread_ratio: float = 0.15


def get_settings() -> Settings:
    return Settings(
        saxo_access_token=os.environ.get("SAXO_ACCESS_TOKEN", ""),
        saxo_environment=os.environ.get("SAXO_ENVIRONMENT", "sim"),
    )
'''


def nettoyer(source: str) -> str:
    """Retire les imports relatifs : tout finit dans un seul espace de noms."""
    lignes = []
    for ligne in source.splitlines():
        if re.match(r"\s*from \.\w+ import", ligne):
            continue
        if re.match(r"\s*from \.\w+ import \($", ligne):
            continue
        if ligne.startswith("from __future__"):
            continue
        lignes.append(ligne)
    texte = "\n".join(lignes)
    # Nettoie les imports relatifs multi-lignes laissés ouverts.
    texte = re.sub(r"^\s{4}\w+,?\n(?=\s{4}\w+,?\n|\)\n)", "", texte, flags=re.M)
    texte = re.sub(r"^\)\n", "", texte, flags=re.M)
    return texte


def construire() -> str:
    morceaux = [
        '"""Backtest autonome — généré par scripts/build_standalone.py.',
        "",
        "Ne pas modifier à la main : relancer le script après toute évolution",
        'du code, sinon cette cellule mesurerait autre chose que le vrai bot."""',
        "",
        "import argparse",
        "import asyncio",
        "import logging",
        "import os",
        "from dataclasses import dataclass, field",
        "from typing import Protocol, runtime_checkable",
        "",
        "import httpx",
        "",
        CONFIG_MINIMAL,
    ]
    for nom in MODULES:
        source = (APP / nom).read_text()
        morceaux.append(f"\n# {'=' * 68}\n# {nom}\n# {'=' * 68}\n")
        morceaux.append(nettoyer(source))
    return "\n".join(morceaux)


if __name__ == "__main__":
    SORTIE.parent.mkdir(exist_ok=True)
    SORTIE.write_text(construire())
    lignes = len(SORTIE.read_text().splitlines())
    print(f"écrit : {SORTIE} ({lignes} lignes)")
