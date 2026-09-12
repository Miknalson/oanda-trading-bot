"""Garde-fous sur le carnet Colab.

Le carnet ne passe par aucun test unitaire : ses erreurs n'apparaissent
qu'au moment où quelqu'un l'exécute, sur son téléphone, souvent tard.
Ces vérifications attrapent les fautes structurelles avant ça.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
CARNET = RACINE / "notebooks" / "backtest_saxo.ipynb"


def cellules_de_code() -> list[str]:
    nb = json.loads(CARNET.read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def code_seul(cellule: str) -> str:
    """Retire les commentaires : seul ce qui s'exécute compte."""
    return "\n".join(
        l for l in cellule.splitlines() if not l.strip().startswith("#")
    )


def test_pas_de_asyncio_run():
    """asyncio.run() échoue dans un carnet : la boucle tourne déjà.

    C'est exactement l'erreur qui a fait planter la cellule 3 en conditions
    réelles (« asyncio.run() cannot be called from a running event loop »).
    Les carnets acceptent `await` au niveau de la cellule.
    """
    fautifs = [i for i, c in enumerate(cellules_de_code(), 1)
               if "asyncio.run" in code_seul(c)]
    assert not fautifs, (
        f"asyncio.run() appelé dans la ou les cellules {fautifs} — "
        "utiliser `await` directement"
    )
    print("  aucune cellule n'appelle asyncio.run()")


def test_le_carnet_est_du_python_valide():
    """Chaque cellule doit être analysable, `await` de haut niveau inclus."""
    import ast
    for i, cellule in enumerate(cellules_de_code(), 1):
        try:
            # Le mode async permet le `await` au niveau de la cellule, comme
            # le fait IPython.
            compile(cellule, f"cellule_{i}", "exec",
                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        except SyntaxError as exc:
            raise AssertionError(f"cellule {i} invalide : {exc}") from exc
    print(f"  {len(cellules_de_code())} cellules de code syntaxiquement valides")


def test_le_jeton_n_est_jamais_en_dur():
    """Le jeton doit toujours passer par une saisie masquée."""
    joint = "\n".join(cellules_de_code())
    assert "getpass" in joint, "la saisie masquée a disparu du carnet"
    for cellule in cellules_de_code():
        for ligne in code_seul(cellule).splitlines():
            if "SAXO_ACCESS_TOKEN" in ligne and "=" in ligne:
                assert "getpass" in ligne or "os.environ.get" in ligne, (
                    f"jeton potentiellement en dur : {ligne.strip()}"
                )
    print("  le jeton passe uniquement par une saisie masquée")


def test_le_carnet_est_a_jour():
    """Le carnet doit correspondre à ce que produit son générateur.

    Le carnet est un artefact : une modification faite dans Colab ne remonte
    nulle part, et divergerait silencieusement du générateur.
    """
    import subprocess

    avant = CARNET.read_text()
    subprocess.run(
        [sys.executable, str(RACINE / "scripts" / "build_notebook.py")],
        check=True, capture_output=True,
    )
    apres = CARNET.read_text()
    assert avant == apres, (
        "le carnet diffère de ce que produit scripts/build_notebook.py — "
        "modifie le générateur, pas le carnet"
    )
    print("  le carnet correspond exactement à son générateur")


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
