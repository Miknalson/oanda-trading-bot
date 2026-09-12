"""Vérifie que chaque client de courtier respecte le contrat `Broker`.

Sans ce test, un courtier à moitié implémenté ne se révélerait qu'en
production, au moment de passer un ordre. Ici on vérifie la forme — méthodes
présentes, signatures compatibles, types neutres — sans aucun appel réseau.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.broker import (  # noqa: E402
    AccountSummary,
    Broker,
    BrokerError,
    Candle,
    OpenTrade,
    Quote,
    TradeStatus,
)
from app.broker_factory import make_broker  # noqa: E402
from app.config import Settings  # noqa: E402
from app.oanda_client import OandaClient  # noqa: E402
from app.saxo_client import SaxoClient  # noqa: E402

REQUIRED = [
    "list_instruments",
    "get_candles",
    "get_quote",
    "get_account_summary",
    "place_market_order",
    "get_trade_status",
    "list_open_trades",
]


def settings(**overrides) -> Settings:
    base = dict(
        broker="saxo", saxo_access_token="fake", saxo_environment="sim",
        oanda_api_key="fake", oanda_account_id="fake", oanda_environment="practice",
        scanner_cache_ttl_seconds=30, live_trading_confirmed=False,
        max_risk_pct=0.02, max_session_loss_pct=0.10, max_trades_per_session=20,
        session_poll_seconds=0.0, max_spread_ratio=0.15,
        vapid_private_key="", vapid_public_key="", vapid_claim_email="",
    )
    base.update(overrides)
    return Settings(**base)


def test_every_client_implements_the_contract():
    for cls in (OandaClient, SaxoClient):
        for name in REQUIRED:
            method = getattr(cls, name, None)
            assert method is not None, f"{cls.__name__} n'implémente pas {name}()"
            assert inspect.iscoroutinefunction(method), (
                f"{cls.__name__}.{name}() doit être asynchrone"
            )
    print(f"  {len(REQUIRED)} méthodes présentes et asynchrones sur les 2 clients")


def test_clients_satisfy_the_protocol():
    """`Broker` est vérifiable à l'exécution : les instances doivent passer."""
    for cls, cfg in ((OandaClient, settings(broker="oanda")), (SaxoClient, settings())):
        instance = cls(cfg)
        assert isinstance(instance, Broker), (
            f"{cls.__name__} ne satisfait pas le protocole Broker"
        )
    print("  les deux clients satisfont le protocole Broker")


def test_factory_selects_the_right_client():
    assert isinstance(make_broker(settings(broker="saxo")), SaxoClient)
    assert isinstance(make_broker(settings(broker="oanda")), OandaClient)
    print("  la fabrique renvoie le bon client selon BROKER")


def test_unknown_broker_is_rejected_clearly():
    try:
        make_broker(settings(broker="nimportequoi"))
    except BrokerError as exc:
        assert "saxo" in str(exc) and "oanda" in str(exc), (
            "le message doit lister les valeurs acceptées"
        )
        print(f"  courtier inconnu -> refusé : « {str(exc)[:60]}... »")
        return
    raise AssertionError("un courtier inconnu aurait dû être refusé")


def test_missing_credentials_fail_with_a_useful_message():
    """Une clé absente doit dire quoi faire, pas juste planter."""
    try:
        SaxoClient(settings(saxo_access_token=""))
    except BrokerError as exc:
        assert "developer.saxo" in str(exc), f"message peu utile : {exc}"
        print("  jeton Saxo absent -> message avec le lien d'inscription")
    else:
        raise AssertionError("un jeton vide aurait dû être refusé")

    try:
        OandaClient(settings(broker="oanda", oanda_api_key=""))
    except BrokerError as exc:
        assert ".env" in str(exc), f"message peu utile : {exc}"
        print("  clé OANDA absente -> message indiquant le fichier à remplir")
    else:
        raise AssertionError("une clé vide aurait dû être refusée")


def test_neutral_types_behave_as_expected():
    q = Quote(bid=1.1000, ask=1.1002)
    assert abs(q.spread - 0.0002) < 1e-9, q.spread
    assert abs(q.mid - 1.1001) < 1e-9, q.mid

    # Le résultat net additionne marché et financement : c'est tout l'intérêt
    # de les garder séparés puis de les recombiner explicitement.
    st = TradeStatus(closed=True, market_pl=10.0, financing=-1.5)
    assert st.net_pl == 8.5, st.net_pl

    c = Candle(open=1.0, high=1.2, low=0.9, close=1.1)
    assert c.high >= c.close >= c.low
    print("  Quote.spread, Quote.mid et TradeStatus.net_pl corrects")


def test_no_broker_json_leaks_into_strategy_code():
    """La logique métier ne doit plus connaître le format d'un courtier."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    metier = ["analysis.py", "session.py", "scanner.py", "backtest.py", "indicators.py"]
    fuites = []
    for name in metier:
        contenu = (app_dir / name).read_text()
        for marqueur in ['"mid"', "realizedPL", "orderFillTransaction", "OandaClient"]:
            if marqueur in contenu:
                fuites.append(f"{name} contient {marqueur}")
    assert not fuites, "format de courtier présent dans la logique : " + ", ".join(fuites)
    print(f"  {len(metier)} modules métier vérifiés, aucun format de courtier")



def test_aucun_endpoint_saxo_deprecie():
    """Saxo retire ses anciennes versions d'endpoints.

    La V1 des graphiques renvoie un 404 — une page d'erreur HTML, pas une
    réponse d'API. Le symptôme est déroutant : tout le reste fonctionne,
    seul cet appel échoue, et le corps de la réponse ne ressemble à rien
    d'exploitable.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "saxo_client.py").read_text()
    code = "\n".join(
        l for l in source.splitlines() if not l.strip().startswith("#")
    )
    assert "/chart/v1/" not in code, "la V1 des graphiques est dépréciée, utiliser /chart/v3/"
    assert "/chart/v3/charts" in code, "endpoint des graphiques introuvable"
    print("  graphiques en V3, aucune V1 dépréciée appelée")


def test_les_erreurs_html_sont_lisibles():
    """Un chemin inexistant renvoie une page HTML : elle doit être résumée."""
    from app.saxo_client import SaxoClient

    class FausseReponse:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    html = '<!DOCTYPE html><html><title>404 - File or directory not found.</title>' + "x" * 900
    message = SaxoClient._message_erreur(FausseReponse(404, html), "GET", "/chart/v1/charts")
    assert "<html" not in message, "la page HTML est recrachée telle quelle"
    assert len(message) < 500, f"message trop long ({len(message)} caractères)"
    assert "n\'existe pas" in message

    # Une vraie erreur d'API doit rester lisible intégralement.
    json_err = SaxoClient._message_erreur(
        FausseReponse(400, '{"Message":"Horizon invalide"}'), "GET", "/chart/v3/charts"
    )
    assert "Horizon invalide" in json_err, "le détail d'une erreur d'API a été perdu"
    print("  page HTML résumée, message d'API conservé")


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
