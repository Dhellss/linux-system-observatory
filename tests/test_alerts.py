"""Tests for the alert state machine.

The sustain window and the hysteresis band are the two mechanisms that stop
threshold alerting from becoming noise, so they are tested against a controlled
clock rather than real time.
"""

from __future__ import annotations

import pytest
from app.database.repositories import AlertRepository
from app.models.diagnostics import Comparison
from app.services.alerts import AlertEngine
from app.services.metrics import MetricRegistry


@pytest.fixture
def engine(config, database, monkeypatch):
    """An alert engine with no default rules and a controllable clock."""
    engine = AlertEngine(config, AlertRepository(database), MetricRegistry())
    for rule in engine.rules():
        engine.remove_rule(rule.rule_id)

    clock = {"now": 1000.0}
    monkeypatch.setattr("app.services.alerts.time.monotonic", lambda: clock["now"])
    engine.clock = clock  # type: ignore[attr-defined]
    return engine


def advance(engine, seconds: float) -> None:
    """Move the controlled clock forward."""
    engine.clock["now"] += seconds


class TestSustainWindow:
    """A rule must hold before it fires."""

    def test_momentary_spike_does_not_fire(self, engine) -> None:
        """Opening a browser briefly pegs the CPU; that is not an incident."""
        rule = engine.add_rule(
            "cpu.usage", Comparison.ABOVE, 90.0, sustain_seconds=5.0
        )
        engine.evaluate({"cpu.usage": 99.0})
        advance(engine, 2.0)
        engine.evaluate({"cpu.usage": 99.0})
        advance(engine, 1.0)
        engine.evaluate({"cpu.usage": 10.0})
        assert not engine.is_firing(rule.rule_id)

    def test_sustained_breach_fires(self, engine) -> None:
        """Holding past the window produces exactly one firing event."""
        rule = engine.add_rule(
            "cpu.usage", Comparison.ABOVE, 90.0, sustain_seconds=5.0
        )
        engine.evaluate({"cpu.usage": 99.0})
        advance(engine, 6.0)
        events = engine.evaluate({"cpu.usage": 99.0})
        assert len(events) == 1
        assert events[0].active
        assert engine.is_firing(rule.rule_id)

    def test_fires_only_once_while_held(self, engine) -> None:
        """A condition that stays true does not re-fire every sample."""
        engine.add_rule("cpu.usage", Comparison.ABOVE, 90.0, sustain_seconds=1.0)
        engine.evaluate({"cpu.usage": 99.0})
        advance(engine, 2.0)
        assert len(engine.evaluate({"cpu.usage": 99.0})) == 1
        advance(engine, 2.0)
        assert engine.evaluate({"cpu.usage": 99.0}) == []


class TestHysteresis:
    """Clearing requires retreating past the threshold by a margin."""

    def test_does_not_flap_at_the_threshold(self, engine) -> None:
        """A value hovering on the boundary stays firing rather than oscillating."""
        rule = engine.add_rule(
            "cpu.usage", Comparison.ABOVE, 100.0, sustain_seconds=0.0
        )
        engine.evaluate({"cpu.usage": 101.0})
        advance(engine, 1.0)
        engine.evaluate({"cpu.usage": 101.0})
        assert engine.is_firing(rule.rule_id)
        # Inside the 3% hysteresis band: still firing.
        advance(engine, 1.0)
        engine.evaluate({"cpu.usage": 99.0})
        assert engine.is_firing(rule.rule_id)

    def test_clears_on_genuine_recovery(self, engine) -> None:
        """Falling well below the threshold clears the alert."""
        rule = engine.add_rule(
            "cpu.usage", Comparison.ABOVE, 100.0, sustain_seconds=0.0
        )
        engine.evaluate({"cpu.usage": 101.0})
        advance(engine, 1.0)
        engine.evaluate({"cpu.usage": 101.0})
        advance(engine, 1.0)
        events = engine.evaluate({"cpu.usage": 50.0})
        assert len(events) == 1
        assert not events[0].active
        assert not engine.is_firing(rule.rule_id)


class TestRuleSemantics:
    """Rule evaluation rules."""

    def test_below_comparison(self, engine) -> None:
        """Inverted metrics such as battery charge fire on a low value."""
        rule = engine.add_rule(
            "battery.percent", Comparison.BELOW, 15.0, sustain_seconds=0.0
        )
        engine.evaluate({"battery.percent": 10.0})
        advance(engine, 1.0)
        engine.evaluate({"battery.percent": 10.0})
        assert engine.is_firing(rule.rule_id)

    def test_absent_metric_leaves_the_rule_dormant(self, engine) -> None:
        """A GPU rule on a machine with no GPU must not oscillate."""
        rule = engine.add_rule(
            "gpu.temperature", Comparison.ABOVE, 80.0, sustain_seconds=0.0
        )
        engine.evaluate({"cpu.usage": 50.0})
        advance(engine, 10.0)
        engine.evaluate({"cpu.usage": 50.0})
        assert not engine.is_firing(rule.rule_id)

    def test_disabled_rule_never_fires(self, engine) -> None:
        """Disabling a rule stops evaluation entirely."""
        rule = engine.add_rule(
            "cpu.usage", Comparison.ABOVE, 10.0, sustain_seconds=0.0
        )
        engine.set_enabled(rule.rule_id, False)
        engine.evaluate({"cpu.usage": 99.0})
        advance(engine, 5.0)
        engine.evaluate({"cpu.usage": 99.0})
        assert not engine.is_firing(rule.rule_id)

    def test_unknown_metric_is_rejected(self, engine) -> None:
        """A rule can only reference a metric the registry knows about."""
        assert engine.add_rule("not.a.metric", Comparison.ABOVE, 1.0) is None

    def test_rules_round_trip_through_settings(self, config, database) -> None:
        """Rules survive a restart."""
        registry = MetricRegistry()
        first = AlertEngine(config, AlertRepository(database), registry)
        for rule in first.rules():
            first.remove_rule(rule.rule_id)
        first.add_rule(
            "memory.percent", Comparison.ABOVE, 77.0, label="Custom rule",
            sustain_seconds=42.0,
        )
        second = AlertEngine(config, AlertRepository(database), registry)
        restored = second.rules()
        assert len(restored) == 1
        assert restored[0].label == "Custom rule"
        assert restored[0].threshold == 77.0
        assert restored[0].sustain_seconds == 42.0

    def test_defaults_are_seeded_on_first_run(self, config, database) -> None:
        """A new installation gets a useful, quiet starter rule set."""
        engine = AlertEngine(config, AlertRepository(database), MetricRegistry())
        assert len(engine.rules()) >= 8
        # Every default must reference a metric that actually exists.
        registry = MetricRegistry()
        for rule in engine.rules():
            assert registry.get(rule.metric) is not None, rule.metric
