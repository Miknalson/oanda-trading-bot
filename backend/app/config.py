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
    # Quel courtier utiliser : "saxo" ou "oanda". Voir broker.py pour le
    # contrat que chacun implémente.
    broker: str
    # --- Saxo (OpenAPI) ---
    # Jeton du portail développeur, valable 24 h :
    # https://www.developer.saxo/accounts/sim/signup
    saxo_access_token: str
    # "sim" (simulation, argent fictif) ou "live" (argent réel).
    saxo_environment: str
    # --- OANDA (API REST v20) ---
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
    # Plafond serveur sur la perte cumulée d'UNE session (fraction du solde).
    # Une session enchaîne plusieurs trades ; sans ce plafond une série de
    # pertes pourrait éroder tout le compte. Le client doit fournir sa propre
    # perte max, qui est ensuite bornée par celle-ci.
    max_session_loss_pct: float
    # Nombre maximum de trades qu'une session peut enchaîner, quoi qu'il
    # arrive — filet de sécurité si ni l'objectif ni la perte max ne sont
    # atteints (marché qui stagne, série de trades quasi nuls).
    max_trades_per_session: int
    # Intervalle de vérification de l'état du trade en cours, en secondes.
    session_poll_seconds: float
    # Le spread est le coût réel de chaque trade. Rapporté à la distance du
    # stop-loss, il dit quelle part du risque part en frais : au-delà de ce
    # seuil le trade est refusé, parce que les frais rongent l'espérance de
    # gain au point de la rendre négative. Voir la section Frais du README.
    max_spread_ratio: float
    # Clés VAPID pour les notifications Web Push (paliers 25/50/75/100%).
    # Vides = push désactivé ; les paliers restent lisibles via l'API.
    vapid_private_key: str
    vapid_public_key: str
    vapid_claim_email: str
    # Taille minimale d'ordre imposée par le courtier, en unités. 0 = désactivé.
    #
    # Valeur par défaut plutôt que champ obligatoire : sans elle, ajouter un
    # réglage casse tout code qui construit Settings en listant ses champs.
    #
    # Désactivé par défaut parce que la valeur réelle de Saxo n'a pas pu être
    # vérifiée ici. Elle protège d'un piège concret des petits comptes :
    # respecter un minimum de 10 000 unités avec 100 € de capital revient à
    # risquer 12 € par trade, soit 12 % du compte — six fois le plafond.
    min_trade_units: int = 0

    @property
    def is_live(self) -> bool:
        """Vrai si le courtier actif engage de l'argent réel."""
        if self.broker == "saxo":
            return self.saxo_environment == "live"
        return self.oanda_environment == "live"

    @property
    def orders_allowed(self) -> bool:
        """False tant qu'on est en live sans confirmation explicite."""
        return not self.is_live or self.live_trading_confirmed


@lru_cache
def get_settings() -> Settings:
    return Settings(
        broker=os.environ.get("BROKER", "saxo").lower(),
        saxo_access_token=os.environ.get("SAXO_ACCESS_TOKEN", ""),
        saxo_environment=os.environ.get("SAXO_ENVIRONMENT", "sim"),
        oanda_api_key=os.environ.get("OANDA_API_KEY", ""),
        oanda_account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
        oanda_environment=os.environ.get("OANDA_ENVIRONMENT", "practice"),
        scanner_cache_ttl_seconds=int(os.environ.get("SCANNER_CACHE_TTL_SECONDS", "30")),
        live_trading_confirmed=os.environ.get("LIVE_TRADING_CONFIRMED", "false").lower() == "true",
        max_risk_pct=float(os.environ.get("MAX_RISK_PCT", "0.02")),
        max_session_loss_pct=float(os.environ.get("MAX_SESSION_LOSS_PCT", "0.10")),
        max_trades_per_session=int(os.environ.get("MAX_TRADES_PER_SESSION", "20")),
        session_poll_seconds=float(os.environ.get("SESSION_POLL_SECONDS", "5")),
        max_spread_ratio=float(os.environ.get("MAX_SPREAD_RATIO", "0.15")),
        min_trade_units=int(os.environ.get("MIN_TRADE_UNITS", "0")),
        vapid_private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
        vapid_public_key=os.environ.get("VAPID_PUBLIC_KEY", ""),
        vapid_claim_email=os.environ.get("VAPID_CLAIM_EMAIL", ""),
    )
