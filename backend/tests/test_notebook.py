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


def cellule_contenant(*marqueurs: str) -> str:
    """Retrouve une cellule par son CONTENU, jamais par sa position.

    Repérer par index (`[-2]`, `[-1]`) casse dès qu'on insère une cellule
    ailleurs dans le carnet — ce qui est arrivé en ajoutant la comparaison au
    hasard entre le backtest et le verdict.
    """
    trouvees = [c for c in cellules_de_code()
                if all(m in c for m in marqueurs)]
    assert len(trouvees) == 1, (
        f"{len(trouvees)} cellules contiennent {marqueurs} — marqueur à revoir"
    )
    return trouvees[0]


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
            # Et surtout : purger TOUT `app` déjà chargé. Ce test a réimporté
            # le paquet depuis un dossier temporaire qui va disparaître ; le
            # laisser en place ferait échouer le premier `from app...` d'un
            # test suivant avec un ModuleNotFoundError incompréhensible.
            for nom in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
                sys.modules.pop(nom, None)

    print("  correction poussée entre deux passages : bien chargée")


def test_le_spread_marche_ferme_n_est_jamais_utilise():
    """Un spread relevé marché fermé ne doit pas paramétrer le backtest.

    Le marché des changes ferme du vendredi soir au dimanche soir. Le spread
    affiché alors est élargi et figé : 5,2 pips sur EUR/USD au lieu de 1. En
    l'appliquant à 1200 bougies de cotations en semaine, un essai réel a fait
    refuser 100 % des entrées sur H1 — résultat qui ressemblait à « aucun
    signal » et ne venait que du week-end.
    """
    import ast

    cellule = code_seul(cellule_contenant("SPREADS", "cours.tradeable"))
    assert "cours.tradeable" in cellule, (
        "la cellule de backtest ne vérifie pas si le marché est ouvert"
    )

    # Vérification sur l'arbre syntaxique et non ligne par ligne : une simple
    # recherche de texte ne voit pas si l'affectation est sous un `if`.
    arbre = ast.parse(cellule)
    sous_garde: set[int] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.If) and "cours.tradeable" in ast.unparse(noeud.test):
            for enfant in noeud.body:
                for descendant in ast.walk(enfant):
                    sous_garde.add(id(descendant))

    # Seules les affectations qui ALIMENTENT le backtest comptent : calculer
    # `pips` pour l'afficher ne fausse rien, l'injecter dans SPREADS si.
    affectations = [
        n for n in ast.walk(arbre)
        if isinstance(n, ast.Assign)
        and "cours.spread" in ast.unparse(n.value)
        and any("SPREADS" in ast.unparse(cible) for cible in n.targets)
    ]
    assert affectations, "le spread live n'alimente plus le backtest du tout"
    for affectation in affectations:
        assert id(affectation) in sous_garde, (
            f"spread live utilisé hors du test d'ouverture : "
            f"{ast.unparse(affectation)}"
        )
    print("  le spread live n'est retenu que sous `if cours.tradeable`")


def _courtier_factice(marche_ouvert: bool):
    """Courtier minimal : assez pour exécuter les cellules pour de vrai.

    Il date ses bougies et honore le bornage par date, comme un vrai : sinon
    le carnet emprunterait la voie « pagination impossible » et le test ne
    vérifierait jamais le chemin réellement emprunté en production.
    """
    import random
    from datetime import datetime, timedelta, timezone

    sys.path.insert(0, str(RACINE / "backend"))
    from app.broker import Candle, Quote

    class Factice:
        # Même plafond que Saxo : le carnet doit donc paginer.
        max_candles_per_request = 1200

        def __init__(self):
            self.appels = 0
            self._histoire: dict[str, list] = {}

        def _construire(self, granularity):
            if granularity in self._histoire:
                return self._histoire[granularity]
            # Graine stable : `hash()` sur une chaîne est randomisé à chaque
            # processus, le test deviendrait capricieux d'une exécution à l'autre.
            rng = random.Random(sum(granularity.encode()))
            # Amplitude plus large sur H4 que sur H1, comme en réel.
            pas = 0.0004 if granularity == "H4" else 0.0001
            heures = 4 if granularity == "H4" else 1
            base = datetime(2020, 1, 1, tzinfo=timezone.utc)
            bougies, prix = [], 1.10
            for i in range(20_000):
                o = prix
                c = prix + rng.gauss(0, pas)
                bougies.append(Candle(
                    open=o, high=max(o, c) + abs(rng.gauss(0, pas / 2)),
                    low=min(o, c) - abs(rng.gauss(0, pas / 2)), close=c,
                    time=(base + timedelta(hours=i * heures)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                ))
                prix = c
            self._histoire[granularity] = bougies
            return bougies

        async def get_quote(self, instrument):
            # Marché fermé : spread élargi, comme chez un vrai courtier.
            spread = 0.00052 if not marche_ouvert else 0.00011
            return Quote(bid=1.10, ask=1.10 + spread, tradeable=marche_ouvert)

        async def get_candles(self, instrument, granularity, count, before=None):
            self.appels += 1
            # Deux paires indisponibles : un vrai compte n'a pas tout, et la
            # cellule d'agrégat doit le traverser sans échouer. Sans ça, son
            # `except` n'est jamais évalué et une faute s'y cacherait.
            if instrument in ("NZD_USD", "EUR_GBP"):
                from app.broker import BrokerError
                raise BrokerError(f"{instrument} indisponible sur ce compte")
            dispo = self._construire(granularity)
            if before:
                dispo = [c for c in dispo if c.time < before]
            return dispo[-min(count, self.max_candles_per_request):]

    return Factice()


def _executer(code: str, espace: dict) -> None:
    """Exécute une cellule, `await` de haut niveau compris, comme IPython."""
    import ast
    import asyncio

    compile_ = compile(code, "cellule", "exec",
                       flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    resultat = eval(compile_, espace)  # noqa: S307 — code du dépôt, pas d'entrée externe
    if asyncio.iscoroutine(resultat):
        asyncio.run(resultat)


def test_les_cellules_de_backtest_s_executent_vraiment(capsys):
    """Exécute les deux dernières cellules de bout en bout.

    Les fautes d'un carnet n'apparaissent qu'à l'exécution, sur le téléphone
    de quelqu'un, souvent tard. Une vérification syntaxique ne les attrape
    pas : une variable mal nommée ou un format d'affichage invalide passe la
    compilation et casse au moment de s'en servir.
    """
    backtest = cellule_contenant("SPREADS", "fetch_history")
    baseline = cellule_contenant("entry_mode", "TIRAGES")
    strategies = cellule_contenant("split_history")
    selectivite = cellule_contenant("JEUX_DE_FILTRES")
    agregat = cellule_contenant("run_pooled_backtest")
    verdict = cellule_contenant("Verdict", "no_trade_reason")

    for marche_ouvert in (True, False):
        espace = {"__name__": "__main__",
                  "courtier": _courtier_factice(marche_ouvert)}
        _executer(backtest, espace)
        _executer(baseline, espace)
        _executer(strategies, espace)
        _executer(selectivite, espace)
        _executer(agregat, espace)
        _executer(verdict, espace)

        sortie = capsys.readouterr().out
        assert "Traceback" not in sortie
        assert "Verdict" in sortie, sortie[-500:]

        # La cellule d'agrégat doit avoir survécu aux paires indisponibles.
        assert "indisponible" in sortie or "ignorée" in sortie, (
            "les paires en erreur n'ont pas été signalées"
        )
        # Soit un verdict, soit un motif — jamais un silence.
        assert "AGRÉGAT" in sortie, "l'agrégat n'a produit ni verdict ni motif"
        assert espace["courtier"].appels > 2, (
            f"{espace['courtier'].appels} requêtes — le carnet n'a pas paginé"
        )
        libelles = [l for l, _ in espace["resultats"]]
        if marche_ouvert:
            assert any("le tien" in l for l in libelles), libelles
            assert "marché OUVERT" in sortie
        else:
            assert not any("le tien" in l for l in libelles), (
                f"spread de marché fermé retenu : {libelles}"
            )
            assert "Marché fermé" in sortie
        print(f"  marché {'ouvert' if marche_ouvert else 'fermé'} : "
              f"{len(espace['resultats'])} backtests, sortie cohérente")


def test_un_depot_vide_est_detecte_et_nomme():
    """Cloner le mauvais dépôt ne doit pas échouer trois cellules plus loin.

    Un dépôt vide ou renommé se clone SANS erreur : git est content, le
    dossier existe. La panne n'apparaît qu'à l'import, sous la forme d'un
    « ModuleNotFoundError: app » qui n'oriente vers rien. Le cas est concret :
    ce compte possède un second dépôt, vide, dont le nom ressemble au bon.
    """
    import os
    import subprocess
    import tempfile

    cellule = "".join(
        [c for c in json.loads(CARNET.read_text())["cells"]
         if c["cell_type"] == "code"][0]["source"]
    )
    assert "main.py" in cellule, "la cellule 1 ne vérifie plus le contenu cloné"

    with tempfile.TemporaryDirectory() as tmp:
        vide = os.path.join(tmp, "vide")
        subprocess.run(["git", "init", "-q", "-b", "main", vide],
                       check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", vide, "commit", "-q", "--allow-empty", "-m", "v"],
                       check=True, capture_output=True)
        clone = os.path.join(tmp, "clone")
        subprocess.run(["git", "clone", "-q", "file://" + vide, clone],
                       check=True, capture_output=True)

        code = (cellule
                .replace('gh_token = getpass.getpass('
                         '"Token GitHub (laisse vide si le dépôt est public) : ").strip()',
                         'gh_token = ""')
                .replace('DOSSIER = "oanda-trading-bot"', f'DOSSIER = {clone!r}')
                .replace('subprocess.run([sys.executable, "-m", "pip", "install", '
                         '"-q", "httpx", "python-dotenv"], check=True)', 'pass'))

        sauvegarde = list(sys.path)
        try:
            exec(compile(code, "cellule_1", "exec"), {"__name__": "__main__"})
            raise AssertionError(
                "la cellule a continué sur un dépôt vide au lieu de s'arrêter"
            )
        except SystemExit as sortie:
            assert sortie.code == 1, sortie.code
        finally:
            sys.path[:] = sauvegarde

    print("  dépôt vide -> arrêt immédiat en nommant la cause")


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
