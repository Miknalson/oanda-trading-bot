"""Tests des paliers de notification 25 / 50 / 75 / 100 %."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.milestones import MilestoneTracker  # noqa: E402


def pcts(milestones):
    return [(m.kind, int(m.threshold * 100)) for m in milestones]


def test_gain_thresholds_in_order():
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(4.0)) == []            # 20% — pas encore
    assert pcts(t.check(5.0)) == [("gain", 25)]
    assert pcts(t.check(9.0)) == []            # 45%
    assert pcts(t.check(10.0)) == [("gain", 50)]
    assert pcts(t.check(15.0)) == [("gain", 75)]
    assert pcts(t.check(20.0)) == [("gain", 100)]
    print("  paliers de gain émis dans l'ordre: 25, 50, 75, 100")


def test_loss_thresholds_in_order():
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(-5.0)) == [("loss", 25)]
    assert pcts(t.check(-10.0)) == [("loss", 50)]
    assert pcts(t.check(-15.0)) == [("loss", 75)]
    assert pcts(t.check(-20.0)) == [("loss", 100)]
    print("  paliers de perte émis dans l'ordre: 25, 50, 75, 100")


def test_no_duplicate_on_oscillation():
    """Le P/L monte, redescend, remonte : pas de doublon."""
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(11.0)) == [("gain", 25), ("gain", 50)]
    assert pcts(t.check(6.0)) == []    # redescend sous 50%
    assert pcts(t.check(11.0)) == []   # remonte : NE DOIT PAS re-notifier
    assert pcts(t.check(12.0)) == []
    assert pcts(t.check(16.0)) == [("gain", 75)]
    print("  oscillation autour d'un seuil: aucun doublon émis")


def test_big_jump_emits_all_crossed():
    """Un seul gros trade qui saute plusieurs paliers les émet tous."""
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(20.0)) == [
        ("gain", 25), ("gain", 50), ("gain", 75), ("gain", 100)
    ]
    print("  saut direct à 100%: les 4 paliers sont émis")


def test_gain_and_loss_are_independent():
    """Passer en perte après un gain n'efface pas les paliers de gain."""
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(6.0)) == [("gain", 25)]
    assert pcts(t.check(-6.0)) == [("loss", 25)]
    assert pcts(t.check(6.0)) == []    # gain 25% déjà émis
    assert pcts(t.check(-11.0)) == [("loss", 50)]
    print("  gain et perte suivis indépendamment, sans interférence")


def test_overshoot_beyond_100_emits_once():
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    assert pcts(t.check(25.0)) == [
        ("gain", 25), ("gain", 50), ("gain", 75), ("gain", 100)
    ]
    assert pcts(t.check(30.0)) == []
    print("  dépassement au-delà de 100%: pas de palier supplémentaire")


def test_messages_are_readable():
    t = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    (m,) = t.check(5.0)
    assert "25%" in m.title and "objectif" in m.title.lower(), m.title
    (m,) = t.check(20.0)[-1:]
    assert "Objectif atteint" in m.title, m.title
    t2 = MilestoneTracker(objective_amount=20.0, max_loss_amount=20.0)
    (m,) = t2.check(-20.0)[-1:]
    assert "Perte max atteinte" in m.title, m.title
    print("  libellés des notifications lisibles et corrects")


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
