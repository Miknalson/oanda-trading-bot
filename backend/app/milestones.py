"""Paliers de progression d'une session (25 / 50 / 75 / 100 %).

Une session progresse dans deux directions opposées : vers son objectif de
gain, ou vers sa perte max. On surveille les deux, et on émet un palier dès
qu'un seuil est franchi.

Règle importante : un palier n'est émis **qu'une seule fois**. Le P/L d'une
session oscille (un trade gagnant, un perdant, un gagnant...), donc sans
mémoire on renverrait la même notification "50%" à chaque aller-retour
autour du seuil. On garde donc un « plus haut atteint » par direction, et un
palier déjà franchi ne se redéclenche jamais, même si le P/L redescend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

THRESHOLDS = (0.25, 0.50, 0.75, 1.00)

# Libellés des fins de session qui n'ont rien à voir avec un palier atteint.
# Sans notification dédiée, ces arrêts se produiraient en silence : la
# session s'arrête, plus aucun ordre n'est passé, et rien ne le signale.
END_REASONS = {
    "max_trades_reached": (
        "⏹️ Session terminée — plafond de trades",
        "Le nombre maximum de trades a été atteint. Ni l'objectif ni la perte "
        "max n'ont été touchés.",
    ),
    "spread_too_wide": (
        "⏸️ Session arrêtée — marché trop cher",
        "Le spread est devenu trop large par rapport au stop-loss. Aucun ordre "
        "n'a été passé à perte structurelle.",
    ),
    "error": (
        "⚠️ Session interrompue — erreur",
        "Une erreur est survenue. La session s'est arrêtée plutôt que de "
        "continuer à trader à l'aveugle.",
    ),
    "stopped_by_user": (
        "⏹️ Session arrêtée",
        "Arrêt manuel. Aucun nouveau trade ne sera ouvert ; un trade déjà "
        "ouvert garde son stop-loss et son take-profit.",
    ),
}


def session_end_notice(reason: str, realized_pl: float, trades: int) -> Milestone:
    """Notification de fin de session pour un arrêt sans palier atteint."""
    title, body = END_REASONS.get(
        reason,
        ("⏹️ Session terminée", "La session s'est arrêtée."),
    )
    resultat = f"{realized_pl:+.2f}"
    return Milestone(
        kind="end",
        threshold=1.0,
        realized_pl=realized_pl,
        amount=0.0,
        title=title,
        body=f"{body} Résultat : {resultat} sur {trades} trade(s).",
    )


@dataclass
class Milestone:
    kind: str  # "gain" (vers l'objectif) ou "loss" (vers la perte max)
    threshold: float  # 0.25, 0.5, 0.75 ou 1.0
    realized_pl: float
    amount: float  # le montant de référence (objectif ou perte max)
    title: str
    body: str
    reached_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "threshold": self.threshold,
            "percent": int(self.threshold * 100),
            "realized_pl": round(self.realized_pl, 2),
            "amount": self.amount,
            "title": self.title,
            "body": self.body,
            "reached_at": self.reached_at,
        }


@dataclass
class MilestoneTracker:
    """Suit la progression et n'émet chaque palier qu'une fois."""

    objective_amount: float
    max_loss_amount: float
    gain_watermark: float = 0.0
    loss_watermark: float = 0.0

    def check(self, realized_pl: float) -> list[Milestone]:
        """Renvoie les paliers nouvellement franchis pour ce P/L cumulé."""
        crossed: list[Milestone] = []

        if realized_pl > 0 and self.objective_amount > 0:
            progress = realized_pl / self.objective_amount
            for threshold in THRESHOLDS:
                if progress >= threshold > self.gain_watermark:
                    crossed.append(self._make("gain", threshold, realized_pl))
            self.gain_watermark = max(self.gain_watermark, progress)

        elif realized_pl < 0 and self.max_loss_amount > 0:
            progress = -realized_pl / self.max_loss_amount
            for threshold in THRESHOLDS:
                if progress >= threshold > self.loss_watermark:
                    crossed.append(self._make("loss", threshold, realized_pl))
            self.loss_watermark = max(self.loss_watermark, progress)

        return crossed

    def force_final(self, kind: str, realized_pl: float) -> Milestone | None:
        """Émet le palier 100% de force, s'il ne l'a pas déjà été.

        Nécessaire parce que la session s'arrête *avant* d'ouvrir un trade
        qui ferait dépasser la perte max : le P/L plafonne alors juste en
        dessous (ex. -19.97 sur -20.00) et le seuil 100% ne serait jamais
        franchi. Sans ça, tu ne recevrais aucune notification au moment où
        la session s'arrête pour cause de perte max — le cas où elle est
        justement la plus utile.
        """
        watermark = self.gain_watermark if kind == "gain" else self.loss_watermark
        if watermark >= 1.0:
            return None
        if kind == "gain":
            self.gain_watermark = 1.0
        else:
            self.loss_watermark = 1.0
        return self._make(kind, 1.0, realized_pl)

    def _make(self, kind: str, threshold: float, realized_pl: float) -> Milestone:
        pct = int(threshold * 100)
        if kind == "gain":
            amount = self.objective_amount
            if threshold >= 1.0:
                title = "🎉 Objectif atteint"
                body = (
                    f"+{realized_pl:.2f} sur un objectif de {amount:.2f}. "
                    "La session est terminée."
                )
            else:
                title = f"📈 {pct}% de l'objectif"
                body = f"+{realized_pl:.2f} sur {amount:.2f} visés."
        else:
            amount = self.max_loss_amount
            if threshold >= 1.0:
                title = "🛑 Perte max atteinte"
                body = (
                    f"{realized_pl:.2f} sur une limite de -{amount:.2f}. "
                    "La session s'est arrêtée."
                )
            else:
                title = f"⚠️ {pct}% de la perte max"
                body = f"{realized_pl:.2f} sur -{amount:.2f} autorisés."
        return Milestone(
            kind=kind,
            threshold=threshold,
            realized_pl=realized_pl,
            amount=amount,
            title=title,
            body=body,
        )
