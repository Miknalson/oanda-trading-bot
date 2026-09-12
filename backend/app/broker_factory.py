"""Choix du courtier selon la configuration.

Un seul endroit décide quel client instancier. Le reste du code ne connaît
que l'interface `Broker`.
"""
from __future__ import annotations

from .broker import Broker, BrokerError
from .config import Settings, get_settings


def make_broker(settings: Settings | None = None) -> Broker:
    settings = settings or get_settings()

    if settings.broker == "saxo":
        from .saxo_client import SaxoClient

        return SaxoClient(settings)

    if settings.broker == "oanda":
        from .oanda_client import OandaClient

        return OandaClient(settings)

    raise BrokerError(
        f"Courtier inconnu : « {settings.broker} ». Valeurs acceptées : "
        "saxo, oanda. Règle BROKER dans backend/.env."
    )
