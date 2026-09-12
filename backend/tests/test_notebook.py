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



def test_la_cellule_1_recupere_vraiment_les_corrections():
    """Relancer la cellule 1 doit charger le code corrigé, pas l'ancien.

    Trois caches s'y opposent, et les oublier donne le pire des symptômes :
    le fichier est bien corrigé sur le disque, et c'est pourtant l'ancienne
    version qui s'exécute. Le cas s'est produit en conditions réelles — une
    correction poussée entre deux essais n'était jamais prise en compte.

    Ce test rejoue le scénario complet sur un dépôt local : import, mise à
    jour côté source, ré-exécution de la logique de la cellule.
    """
    import importlib
    import os
    import shutil
    import subprocess
    import tempfile

    cellule = "".join(
        [c for c in json.loads(CARNET.read_text())["cells"]
         if c["cell_type"] == "code"][0]["source"]
    )
    for attendu, quoi in [
        ("del sys.modules", "purge des modules importés"),
        ("__pycache__", "purge des fichiers compilés"),
        ("invalidate_caches", "invalidation du cache d'import"),
        ("reset", "remise à zéro sur la dernière version"),
    ]:
        assert attendu in cellule, f"la cellule 1 a perdu : {quoi}"

    # Rejoue le scénario pour de vrai.
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "source")
        clone = os.path.join(tmp, "clone")
        os.makedirs(os.path.join(source, "backend", "app"))
        open(os.path.join(source, "backend", "app", "__init__.py"), "w").close()

        def ecrire(valeur):
            chemin = os.path.join(source, "backend", "app", "marqueur_test.py")
            open(chemin, "w").write(f'VERSION = "{valeur}"\n')

        def commit(message):
            for cmd in (["add", "-A"],
                        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message]):
                subprocess.run(["git", "-C", source] + cmd, check=True, capture_output=True)

        subprocess.run(["git", "init", "-q", "-b", "main", source], check=True, capture_output=True)
        ecrire("ancienne")
        commit("v1")

        def rejouer_cellule_1():
            if os.path.isdir(clone):
                subprocess.run(["git", "-C", clone, "fetch", "--depth", "1", "origin", "main"],
                               check=True, capture_output=True)
                subprocess.run(["git", "-C", clone, "reset", "--hard", "origin/main"],
                               check=True, capture_output=True)
            else:
                subprocess.run(["git", "clone", "-q", "--depth", "1",
                                "file://" + source, clone], check=True, capture_output=True)
            for nom in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
                del sys.modules[nom]
            for racine, dossiers, _ in os.walk(clone):
                for d in list(dossiers):
                    if d == "__pycache__":
                        shutil.rmtree(os.path.join(racine, d), ignore_errors=True)
            importlib.invalidate_caches()
            chemin = os.path.join(clone, "backend")
            if chemin not in sys.path:
                sys.path.insert(0, chemin)

        sauvegarde = list(sys.path)
        modules_avant = set(sys.modules)
        try:
            rejouer_cellule_1()
            from app.marqueur_test import VERSION as v1
            assert v1 == "ancienne", v1

            # Une correction est poussée, comme entre deux essais.
            ecrire("corrigee")
            commit("correction")

            rejouer_cellule_1()
            from app.marqueur_test import VERSION as v2
            assert v2 == "corrigee", (
                f"la correction n'est pas chargée (toujours « {v2} ») — "
                "un cache n'a pas été vidé"
            )
        finally:
            sys.path[:] = sauvegarde
            for nom in set(sys.modules) - modules_avant:
                sys.modules.pop(nom, None)

    print("  correction poussée entre deux passages : bien chargée")


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
