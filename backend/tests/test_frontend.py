"""Garde-fous sur la PWA.

Le front n'a pas de compilateur : une faute de nom ne produit aucune erreur,
juste un écran qui affiche « undefined » ou un bouton qui ne fait rien. Deux
bugs réels de ce genre ont vécu longtemps dans ce dépôt :

- `app.js` lisait `t.currentUnits` et `t.unrealizedPL`, les noms camelCase
  d'OANDA, alors que l'API neutre renvoie `units` et `unrealized_pl` depuis la
  refonte courtier. Le panneau des positions affichait « undefined unités ».
- `push.js` exposait ses fonctions avec `export` alors qu'`index.html` le
  charge en script classique : « Unexpected token 'export' » empêchait TOUT le
  fichier de s'exécuter, donc le bouton de notifications restait muet.

Ces tests relient les trois fichiers entre eux et à l'API, pour que la
prochaine dérive échoue ici et non sur un téléphone.
"""
from __future__ import annotations

import re
import sys
from dataclasses import fields
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
FRONTEND = RACINE / "frontend"
sys.path.insert(0, str(RACINE / "backend"))


def lire(nom: str) -> str:
    return (FRONTEND / nom).read_text()


def sans_commentaires(js: str) -> str:
    """Retire les commentaires : seul ce qui s'exécute compte."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(
        ligne for ligne in js.splitlines() if not ligne.strip().startswith("//")
    )


def test_chaque_id_lu_existe_dans_le_html():
    """Un getElementById sans balise correspondante renvoie null en silence.

    La suite du code fait alors `null.textContent` et s'arrête au milieu d'une
    fonction, souvent sans rien afficher.
    """
    html = lire("index.html")
    ids_html = set(re.findall(r'id="([^"]+)"', html))

    js = sans_commentaires(lire("app.js"))
    # Les identifiants sont déclarés en un seul tableau passé à els([...]).
    tableau = re.search(r"const el = els\(\[(.*?)\]\);", js, flags=re.S)
    assert tableau, "la liste des identifiants a changé de forme"
    ids_js = set(re.findall(r'"([^"]+)"', tableau.group(1)))
    ids_js |= set(re.findall(r'getElementById\("([^"]+)"\)', js))

    manquants = sorted(ids_js - ids_html)
    assert not manquants, f"identifiants lus mais absents du HTML : {manquants}"
    print(f"  {len(ids_js)} identifiants lus, tous présents dans le HTML")


def test_aucun_id_du_html_n_est_orphelin():
    """L'inverse : une balise avec un id que personne n'utilise est du code mort.

    « Utiliser » couvre deux usages légitimes : lu par app.js, ou ciblé par le
    CSS (les sections n'ont souvent d'id que pour être stylées).
    """
    html = lire("index.html")
    js = sans_commentaires(lire("app.js"))
    css = lire("style.css")
    ids_html = set(re.findall(r'id="([^"]+)"', html))
    orphelins = sorted(
        i for i in ids_html if f'"{i}"' not in js and f"#{i}" not in css
    )
    assert not orphelins, (
        f"identifiants ni lus par app.js ni stylés par le CSS : {orphelins}"
    )
    print(f"  aucun des {len(ids_html)} identifiants du HTML n'est orphelin")


def test_les_champs_lus_sur_les_positions_existent_dans_l_api():
    """Le bug historique : les noms camelCase d'OANDA après la refonte."""
    from app.broker import OpenTrade

    js = sans_commentaires(lire("app.js"))
    bloc = js[js.index("async function loadPositions"):]
    bloc = bloc[: bloc.index("\n}")]

    lus = set(re.findall(r"\bt\.([A-Za-z_][A-Za-z0-9_]*)", bloc))
    connus = {f.name for f in fields(OpenTrade)}
    inconnus = sorted(lus - connus)
    assert not inconnus, (
        f"champs lus sur une position mais absents de OpenTrade : {inconnus}. "
        f"L'API neutre expose {sorted(connus)}."
    )
    print(f"  positions : {sorted(lus)} — tous présents dans OpenTrade")


def test_les_champs_lus_sur_la_session_existent_dans_l_api():
    """Même vérification pour la session, le cœur de l'app."""
    from app.session import TradingSession

    js = sans_commentaires(lire("app.js"))
    lus = set(re.findall(r"\bsession\.([A-Za-z_][A-Za-z0-9_]*)", js))

    # Clés réellement produites par to_dict(), pas les attributs de la classe.
    exemple = TradingSession(
        id="x", instrument="EUR_USD", risk_pct=0.01, objective_amount=20.0,
        max_loss_amount=10.0, reward_ratio=1.5, granularity="H1",
    )
    connus = set(exemple.to_dict())
    inconnus = sorted(lus - connus)
    assert not inconnus, (
        f"champs lus sur la session mais absents de to_dict() : {inconnus}. "
        f"L'API expose {sorted(connus)}."
    )
    print(f"  session : {len(lus)} champs lus, tous produits par to_dict()")


def test_les_champs_lus_sur_les_paliers_existent_dans_l_api():
    """Les paliers échappaient au contrôle, et portaient deux fautes muettes.

    `app.js` lisait `m.threshold` (0.25) au lieu de `m.percent` (25) — donc
    « 0.25 % » affiché — et `m.direction` au lieu de `m.kind`, ce qui donnait
    une classe CSS `chip undefined`, sans couleur et sans erreur.
    """
    from app.milestones import Milestone

    js = sans_commentaires(lire("app.js"))
    lus = set(re.findall(r"\bm\.([A-Za-z_][A-Za-z0-9_]*)", js))

    exemple = Milestone(
        kind="gain", threshold=0.25, realized_pl=5.0, amount=20.0,
        title="t", body="b", reached_at="2026-01-01T00:00:00Z",
    )
    connus = set(exemple.to_dict())
    inconnus = sorted(lus - connus)
    assert not inconnus, (
        f"champs lus sur un palier mais absents de to_dict() : {inconnus}. "
        f"L'API expose {sorted(connus)}."
    )
    # Le pourcentage affiché doit être l'entier, pas la fraction.
    assert "m.percent" in js, "le palier doit afficher `percent`, pas `threshold`"
    print(f"  paliers : {sorted(lus)} — tous produits par to_dict()")


def test_chaque_appel_reseau_vise_une_route_existante():
    """Une URL qui n'existe plus ne se voit qu'à l'exécution, en 404."""
    import os

    os.environ.setdefault("SAXO_ACCESS_TOKEN", "factice-pour-import")
    from app.main import app

    routes = {getattr(r, "path", "") for r in app.routes}
    js = sans_commentaires(lire("app.js")) + sans_commentaires(lire("push.js"))

    appelees = set()
    for chemin in re.findall(r"`\$\{(?:API_BASE|PUSH_API_BASE)\}([^`]*)`", js):
        chemin = chemin.split("?")[0]
        # Le helper générique `fetch(`${API_BASE}${path}`)` ne désigne aucune
        # URL en particulier : son chemin est une variable, pas un littéral.
        if not chemin.startswith("/"):
            continue
        # Les interpolations deviennent des paramètres de route.
        chemin = re.sub(r"\$\{[^}]+\}", "{p}", chemin)
        appelees.add(chemin)
    for chemin in re.findall(r'api\("([^"?]+)', js):
        appelees.add(chemin)

    assert appelees, "aucun appel réseau détecté — l'extraction a dû casser"

    def existe(chemin: str) -> bool:
        if chemin in routes:
            return True
        # Compare en ignorant le nom des paramètres : {p} vaut {session_id}.
        motif = re.sub(r"\{[^}]+\}", "{}", chemin)
        return any(re.sub(r"\{[^}]+\}", "{}", r) == motif for r in routes)

    introuvables = sorted(c for c in appelees if not existe(c))
    assert not introuvables, f"URLs appelées sans route côté serveur : {introuvables}"
    print(f"  {len(appelees)} URLs appelées, toutes servies par le backend")


def test_les_scripts_charges_en_classique_n_utilisent_pas_export():
    """`export` dans un script non-module empêche tout le fichier de tourner."""
    html = lire("index.html")
    classiques = re.findall(r'<script src="([^"]+)"(?![^>]*type="module")', html)
    assert classiques, "plus aucun script classique — vérifier le balisage"

    for nom in classiques:
        code = sans_commentaires(lire(nom))
        fautes = [l.strip() for l in code.splitlines()
                  if re.match(r"^\s*export\b", l)]
        assert not fautes, (
            f"{nom} est chargé en script classique mais contient : {fautes}"
        )
    print(f"  {', '.join(classiques)} : aucun `export` interdit")


def test_les_fonctions_push_sont_bien_exposees():
    """app.js les appelle via window : elles doivent y être attachées."""
    push = sans_commentaires(lire("push.js"))
    app = sans_commentaires(lire("app.js"))

    for nom in re.findall(r"window\.(\w+)\(", app):
        if nom in {"API_BASE"}:
            continue
        assert f"window.{nom} =" in push, (
            f"app.js appelle window.{nom}() mais push.js ne l'expose pas"
        )
    print("  les fonctions push appelées sont bien attachées à window")


def test_la_perte_max_n_a_pas_de_valeur_par_defaut_dans_le_formulaire():
    """Côté serveur elle est obligatoire ; le formulaire ne doit pas la pré-remplir.

    Une valeur par défaut ferait démarrer une session avec un plafond que
    l'utilisateur n'a pas choisi — exactement ce que l'obligation côté serveur
    cherche à empêcher.
    """
    html = lire("index.html")
    champ = re.search(r'<input id="max-loss-amount"[^>]*>', html)
    assert champ, "le champ de perte max a disparu du formulaire"
    assert "value=" not in champ.group(0), (
        f"la perte max est pré-remplie : {champ.group(0)}"
    )

    js = sans_commentaires(lire("app.js"))
    assert 'el["max-loss-amount"]' in js, "la perte max n'est pas lue"
    assert "maxLoss > 0" in js, "le formulaire n'exige pas une perte max positive"
    print("  perte max : aucun défaut, et refus de démarrer sans elle")


def test_le_contexte_non_securise_est_diagnostique_et_non_avale():
    """Sur http://192.168.x.x, le push est impossible — il faut le DIRE.

    Les notifications exigent un contexte sécurisé (HTTPS, ou localhost). Quand
    on ouvre l'app depuis son téléphone vers un PC du même Wi-Fi, l'adresse est
    une IP en HTTP : le navigateur retire alors `navigator.serviceWorker`
    entièrement.

    L'ancien code faisait `.catch(() => {})` sur l'enregistrement, puis
    annonçait « ce navigateur ne gère pas les notifications » — ce qui est faux
    et envoie l'utilisateur chercher la solution du mauvais côté. Le navigateur
    les gère ; c'est la connexion qui n'est pas sûre, et ça se règle côté
    serveur.
    """
    app = sans_commentaires(lire("app.js"))
    push = sans_commentaires(lire("push.js"))

    assert "isSecureContext" in app, (
        "app.js ne distingue pas un contexte non sécurisé"
    )
    assert "isSecureContext" in push, (
        "push.js ne distingue pas un contexte non sécurisé"
    )
    assert ".catch(() => {})" not in app, (
        "un échec d'enregistrement du service worker est encore avalé en silence"
    )
    # Le message doit nommer HTTPS, la vraie cause.
    assert "HTTPS" in app, "le message n'explique pas qu'il faut HTTPS"

    # Et le diagnostic « non sécurisé » doit passer AVANT celui du support
    # navigateur, sinon c'est le mauvais message qui sort.
    assert push.index("isSecureContext") < push.index('"serviceWorker" in navigator'), (
        "push.js teste le support du navigateur avant la sécurité du contexte : "
        "le message trompeur sortirait en premier"
    )
    print("  contexte non sécurisé : diagnostiqué, et avant le test de support")


def test_une_interface_absente_s_explique_au_lieu_de_faire_404():
    """Un 404 nu sur « / » envoie chercher du mauvais côté.

    Si le dossier frontend/ manque, l'API répond, /docs s'affiche, et seule la
    page d'accueil est introuvable. Sans message, on soupçonne le port, le
    pare-feu ou l'adresse — alors que c'est un dossier manquant.
    """
    import importlib
    import os
    import shutil
    import sys
    import tempfile

    os.environ.setdefault("SAXO_ACCESS_TOKEN", "factice-pour-import")

    # Recharge le module avec un backend copié SANS le dossier frontend,
    # exactement le cas d'un dépôt incomplet.
    with tempfile.TemporaryDirectory() as tmp:
        faux = Path(tmp) / "depot" / "backend"
        shutil.copytree(RACINE / "backend" / "app", faux / "app")

        sauvegarde = list(sys.path)
        modules = {n: m for n, m in sys.modules.items()
                   if n == "app" or n.startswith("app.")}
        try:
            for nom in modules:
                del sys.modules[nom]
            sys.path.insert(0, str(faux))
            main = importlib.import_module("app.main")

            assert not main.FRONTEND_DIR.is_dir(), (
                f"le dossier ne devait pas exister : {main.FRONTEND_DIR}"
            )
            racines = [r for r in main.app.routes if getattr(r, "path", "") == "/"]
            assert racines, "aucune route « / » : le 404 serait nu et muet"
        finally:
            sys.path[:] = sauvegarde
            for nom in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
                del sys.modules[nom]
            sys.modules.update(modules)

    print("  interface absente -> route « / » qui explique, pas un 404 nu")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    echecs = 0
    for t in tests:
        try:
            print(f"\n{t.__name__}:")
            t()
            print("  ✅ OK")
        except AssertionError as exc:
            echecs += 1
            print(f"  ❌ ÉCHEC: {exc}")
    print(f"\n{len(tests) - echecs}/{len(tests)} tests passés")
    sys.exit(1 if echecs else 0)
