"""Configuration centralisée, chargée depuis les variables d'environnement.

Ne jamais committer de vraies clés API. Copier `.env.example` en `.env`
et remplir tes propres valeurs (le `.env` est ignoré par git).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    oanda_api_key: str
    oanda_account_id: str
    # "practice" (compte démo, argent fictif) ou "live" (argent réel).
    oanda_environment: str
    scanner_cache_ttl_seconds: int
    # Garde-fou supplémentaire : même avec OANDA_ENVIRONMENT=live, aucun ordre
    # réel n'est envoyé tant que ce flag n'est pas explicitement activé.
    live_trading_confirmed: bool
    # Plafond serveur : aucune requête ne peut faire risquer plus que ça sur
    # un seul trade, quel que soit le risk_pct demandé par le client.
    max_risk_pct: float

    @property
    def oanda_base_url(self) -> str:
        if self.oanda_environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    @property
    def is_live(self) -> bool:
        return self.oanda_environment == "live"

    @property
    def orders_allowed(self) -> bool:
        """False tant qu'on est en live sans confirmation explicite."""
        return not self.is_live or self.live_trading_confirmed


@lru_cache
def get_settings() -> Settings:
    return Settings(
        oanda_api_key=os.environ.get("OANDA_API_KEY", ""),
        oanda_account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
        oanda_environment=os.environ.get("OANDA_ENVIRONMENT", "practice"),
        scanner_cache_ttl_seconds=int(os.environ.get("SCANNER_CACHE_TTL_SECONDS", "30")),
        live_trading_confirmed=os.environ.get("LIVE_TRADING_CONFIRMED", "false").lower() == "true",
        max_risk_pct=float(os.environ.get("MAX_RISK_PCT", "0.02")),
    )
