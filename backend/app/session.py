"""Sessions de trading — enchaîne plusieurs petits trades vers un objectif.

Une session ouvre un trade, attend qu'il se ferme (stop-loss ou take-profit
touché chez OANDA), encaisse le résultat, puis recommence — jusqu'à ce que
l'une de ces conditions d'arrêt soit remplie :

- l'objectif de gain cumulé est atteint ;
- la PERTE MAX CUMULÉE est atteinte (obligatoire, jamais optionnelle) ;
- le nombre max de trades est atteint ;
- l'utilisateur arrête la session ;
- une erreur survient (on s'arrête, on ne trade pas à l'aveugle).

Sécurité : la perte max fournie par le client est toujours bornée par
`Settings.max_session_loss_pct` du solde réel du compte. Une seule session
peut être active à la fois — sinon plusieurs boucles, chacune respectant sa
propre limite, pourraient ensemble vider le compte.

Si le serveur redémarre en cours de session, la boucle s'arrête : le trade
éventuellement ouvert garde son stop-loss et son take-profit chez OANDA, donc
il reste protégé et se fermera tout seul.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .analysis import TradeAnalyzer
from .config import Settings
from .oanda_client import OandaClient, OandaError

# Combien de temps on attend au maximum qu'un trade se ferme avant de
# considérer que quelque chose cloche et d'arrêter la session.
TRADE_TIMEOUT_SECONDS = 60 * 60 * 6


@dataclass
class SessionTrade:
    trade_id: str
    instrument: str
    direction: str
    units: int
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    realized_pl: float | None = None
    closed: bool = False
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class TradingSession:
    id: str
    instrument: str
    risk_pct: float
    objective_amount: float
    max_loss_amount: float
    reward_ratio: float
    granularity: str
    status: str = "running"
    stop_reason: str | None = None
    realized_pl: float = 0.0
    trades: list[SessionTrade] = field(default_factory=list)
    error: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "running"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "instrument": self.instrument,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "realized_pl": round(self.realized_pl, 2),
            "objective_amount": self.objective_amount,
            "max_loss_amount": self.max_loss_amount,
            "risk_pct": self.risk_pct,
            "reward_ratio": self.reward_ratio,
            "trades_count": len(self.trades),
            "trades": [t.__dict__ for t in self.trades],
            "error": self.error,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class SessionError(RuntimeError):
    """Paramètres de session invalides ou refusés par les garde-fous."""


class SessionManager:
    """Détient au plus UNE session active, et sa boucle asyncio."""

    def __init__(self, client: OandaClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.analyzer = TradeAnalyzer(client)
        self.sessions: dict[str, TradingSession] = {}
        self._task: asyncio.Task | None = None
        self._active_id: str | None = None

    @property
    def active_session(self) -> TradingSession | None:
        if self._active_id is None:
            return None
        session = self.sessions.get(self._active_id)
        return session if session and session.is_active else None

    async def start(
        self,
        instrument: str,
        risk_pct: float,
        objective_amount: float,
        max_loss_amount: float,
        reward_ratio: float = 1.5,
        granularity: str = "M15",
    ) -> TradingSession:
        if self.active_session is not None:
            raise SessionError(
                "Une session est déjà en cours. Arrête-la avant d'en démarrer une autre."
            )
        if max_loss_amount <= 0:
            raise SessionError("max_loss_amount est obligatoire et doit être positif.")
        if objective_amount <= 0:
            raise SessionError("objective_amount doit être positif.")
        if risk_pct <= 0 or risk_pct > self.settings.max_risk_pct:
            raise SessionError(
                f"risk_pct doit être entre 0 et {self.settings.max_risk_pct:.2%}."
            )
        if reward_ratio <= 0:
            raise SessionError("reward_ratio doit être positif.")

        # Plafond serveur : la perte max ne peut pas dépasser une fraction
        # du solde réel, quoi que le client demande.
        account = await self.client.get_account_summary()
        balance = float(account.get("balance", 0))
        if balance <= 0:
            raise SessionError("Solde de compte introuvable ou nul.")
        server_cap = balance * self.settings.max_session_loss_pct
        if max_loss_amount > server_cap:
            raise SessionError(
                f"max_loss_amount ({max_loss_amount:.2f}) dépasse le plafond de "
                f"sécurité serveur de {server_cap:.2f} "
                f"({self.settings.max_session_loss_pct:.0%} du solde de {balance:.2f})."
            )

        session = TradingSession(
            id=str(uuid.uuid4()),
            instrument=instrument,
            risk_pct=risk_pct,
            objective_amount=objective_amount,
            max_loss_amount=max_loss_amount,
            reward_ratio=reward_ratio,
            granularity=granularity,
        )
        self.sessions[session.id] = session
        self._active_id = session.id
        self._task = asyncio.create_task(self._run(session))
        return session

    def stop(self, session_id: str) -> TradingSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError(f"Session inconnue : {session_id}")
        if not session.is_active:
            return session
        self._finish(session, "stopped_by_user")
        if self._task is not None:
            self._task.cancel()
        return session

    def _finish(self, session: TradingSession, reason: str, error: str | None = None) -> None:
        session.status = "stopped" if reason == "stopped_by_user" else reason
        session.stop_reason = reason
        session.error = error
        session.ended_at = datetime.now(timezone.utc).isoformat()
        if self._active_id == session.id:
            self._active_id = None

    async def _run(self, session: TradingSession) -> None:
        try:
            while session.is_active:
                if len(session.trades) >= self.settings.max_trades_per_session:
                    self._finish(session, "max_trades_reached")
                    return

                suggestion = await self.analyzer.suggest(
                    instrument=session.instrument,
                    risk_pct=session.risk_pct,
                    granularity=session.granularity,
                    max_risk_pct=self.settings.max_risk_pct,
                    reward_ratio=session.reward_ratio,
                )

                # Dernière vérification avant d'engager de l'argent : est-ce
                # que perdre ce trade ferait dépasser la perte max ? Si oui,
                # on ne l'ouvre pas — on s'arrête proprement avant.
                worst_case = session.realized_pl - suggestion.risk_amount
                if worst_case <= -session.max_loss_amount:
                    self._finish(session, "max_loss_would_be_exceeded")
                    return

                order = await self.client.create_market_order_with_brackets(
                    instrument=suggestion.instrument,
                    units=suggestion.suggested_units,
                    stop_loss_price=suggestion.stop_loss_price,
                    take_profit_price=suggestion.take_profit_price,
                )
                trade_id = (
                    order.get("orderFillTransaction", {})
                    .get("tradeOpened", {})
                    .get("tradeID")
                )
                if not trade_id:
                    self._finish(
                        session,
                        "error",
                        f"Ordre passé mais aucun tradeID renvoyé par OANDA : {order}",
                    )
                    return

                trade = SessionTrade(
                    trade_id=trade_id,
                    instrument=suggestion.instrument,
                    direction=suggestion.direction,
                    units=suggestion.suggested_units,
                    entry_price=suggestion.entry_price,
                    stop_loss_price=suggestion.stop_loss_price,
                    take_profit_price=suggestion.take_profit_price,
                )
                session.trades.append(trade)

                realized = await self._wait_for_close(trade)
                if realized is None:
                    self._finish(
                        session,
                        "error",
                        f"Trade {trade_id} toujours ouvert après le délai maximum.",
                    )
                    return

                trade.realized_pl = realized
                trade.closed = True
                session.realized_pl += realized

                if session.realized_pl >= session.objective_amount:
                    self._finish(session, "objective_reached")
                    return
                if session.realized_pl <= -session.max_loss_amount:
                    self._finish(session, "max_loss_reached")
                    return

        except asyncio.CancelledError:
            # Arrêt manuel : _finish a déjà été appelé par stop().
            raise
        except OandaError as exc:
            self._finish(session, "error", str(exc))
        except Exception as exc:  # noqa: BLE001 - on arrête plutôt que de trader à l'aveugle
            self._finish(session, "error", f"{type(exc).__name__}: {exc}")

    async def _wait_for_close(self, trade: SessionTrade) -> float | None:
        """Attend la fermeture du trade chez OANDA, renvoie son P/L réalisé."""
        waited = 0.0
        poll = self.settings.session_poll_seconds
        while waited < TRADE_TIMEOUT_SECONDS:
            await asyncio.sleep(poll)
            waited += poll
            detail = await self.client.get_trade(trade.trade_id)
            if detail.get("state") == "CLOSED":
                return float(detail.get("realizedPL", 0))
        return None
