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

    @property
    def oanda_base_url(self) -> str:
        if self.oanda_environment == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    @property
    def is_live(self) -> bool:
        return self.oanda_environment == "live"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        oanda_api_key=os.environ.get("OANDA_API_KEY", ""),
        oanda_account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
        oanda_environment=os.environ.get("OANDA_ENVIRONMENT", "practice"),
        scanner_cache_ttl_seconds=int(os.environ.get("SCANNER_CACHE_TTL_SECONDS", "30")),
    )
