"""Threshold alert evaluation.

The design problem with threshold alerting is false positives.  Opening a browser
takes CPU to 100% for a moment; a naive "CPU > 90%" rule fires immediately and
trains the user to ignore alerts.

Two mechanisms prevent that:

**Sustain windows.**  A rule only fires once its condition has held continuously
for ``sustain_seconds``.  A momentary spike resets the timer without firing.

**Hysteresis on clearing.**  Once firing, a rule clears only when the value falls
back past the threshold by a small margin.  Without it, a metric hovering exactly
at the threshold would fire and clear repeatedly.

Alerts are also rate-limited per rule so that a genuinely sustained condition
notifies once, not once per sample.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from app.core.config import ConfigService
from app.database.repositories import AlertRepository, StoredAlert
from app.models.diagnostics import AlertEvent, AlertRule, Comparison, Severity
from app.services.metrics import MetricRegistry

_log = logging.getLogger(__name__)

#: Fraction of the threshold a value must retreat past before a rule clears.
_HYSTERESIS = 0.03


@dataclass
class _RuleState:
    """Evaluation state for one rule."""

    #: When the condition first became true, or ``None`` while it is false.
    breached_since: float | None = None
    #: True while the rule is actively firing.
    firing: bool = False
    #: Monotonic time of the last notification, for rate limiting.
    last_notified: float = 0.0
    #: Most recent observed value, shown in the rules table.
    last_value: float | None = None


class AlertEngine(QObject):
    """Evaluates alert rules against extracted metric values."""

    #: Emitted when a rule starts or stops firing.
    alert_changed = Signal(object)          # AlertEvent
    #: Emitted when the rule set itself changes, so the UI can rebuild its table.
    rules_changed = Signal()

    def __init__(
        self,
        config: ConfigService,
        repository: AlertRepository,
        registry: MetricRegistry,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._repository = repository
        self._registry = registry
        self._rules: dict[str, AlertRule] = {}
        self._state: dict[str, _RuleState] = {}
        self._load_rules()

    # ----------------------------------------------------------------- rule store

    def _load_rules(self) -> None:
        """Restore persisted rules, seeding sensible defaults on first run."""
        stored = self._config.settings.alert_rules
        if not stored:
            for default in self._default_rules():
                self._rules[default.rule_id] = default
            self._persist()
        else:
            for raw in stored:
                restored = self._deserialise(raw)
                if restored is not None:
                    self._rules[restored.rule_id] = restored
        self._state = {rule_id: _RuleState() for rule_id in self._rules}

    def _default_rules(self) -> list[AlertRule]:
        """A starter rule set covering the conditions users actually care about.

        Chosen to be quiet on a healthy machine: each threshold is high enough
        that a normal desktop under load will not trip it, because an alert system
        that cries wolf on first run gets switched off.
        """
        return [
            AlertRule("cpu-hot", "cpu.temperature", Comparison.ABOVE, 88.0,
                      Severity.WARNING, "CPU temperature high", sustain_seconds=20.0),
            AlertRule("cpu-pegged", "cpu.usage", Comparison.ABOVE, 95.0,
                      Severity.INFO, "CPU sustained at maximum", sustain_seconds=60.0),
            AlertRule("memory-full", "memory.percent", Comparison.ABOVE, 92.0,
                      Severity.WARNING, "Memory nearly exhausted",
                      sustain_seconds=30.0),
            AlertRule("memory-pressure", "memory.pressure", Comparison.ABOVE, 20.0,
                      Severity.CRITICAL, "Memory pressure stalling tasks",
                      sustain_seconds=15.0),
            AlertRule("disk-full", "storage.fullest", Comparison.ABOVE, 92.0,
                      Severity.WARNING, "Filesystem nearly full", sustain_seconds=10.0),
            AlertRule("gpu-hot", "gpu.temperature", Comparison.ABOVE, 87.0,
                      Severity.WARNING, "GPU temperature high", sustain_seconds=20.0),
            AlertRule("battery-low", "battery.percent", Comparison.BELOW, 12.0,
                      Severity.WARNING, "Battery low", sustain_seconds=10.0),
            AlertRule("battery-worn", "battery.health", Comparison.BELOW, 55.0,
                      Severity.INFO, "Battery significantly worn",
                      sustain_seconds=300.0, notify=False),
            AlertRule("services-failed", "services.failed", Comparison.ABOVE, 0.0,
                      Severity.WARNING, "A systemd unit has failed",
                      sustain_seconds=5.0),
            AlertRule("network-down", "network.online", Comparison.BELOW, 1.0,
                      Severity.WARNING, "Network disconnected", sustain_seconds=20.0),
        ]

    def rules(self) -> list[AlertRule]:
        """Every rule, ordered by label."""
        return sorted(self._rules.values(), key=lambda r: r.label or r.metric)

    def rule(self, rule_id: str) -> AlertRule | None:
        """One rule by identifier."""
        return self._rules.get(rule_id)

    def add_rule(
        self,
        metric: str,
        comparison: Comparison,
        threshold: float,
        severity: Severity = Severity.WARNING,
        label: str = "",
        sustain_seconds: float = 10.0,
        notify: bool = True,
    ) -> AlertRule | None:
        """Create a rule, rejecting unknown metrics."""
        definition = self._registry.get(metric)
        if definition is None:
            _log.warning("Refusing to add a rule for unknown metric %r", metric)
            return None
        rule = AlertRule(
            rule_id=uuid.uuid4().hex[:12],
            metric=metric,
            comparison=comparison,
            threshold=threshold,
            severity=severity,
            label=label or definition.label,
            sustain_seconds=max(0.0, sustain_seconds),
            notify=notify,
        )
        self._rules[rule.rule_id] = rule
        self._state[rule.rule_id] = _RuleState()
        self._persist()
        self.rules_changed.emit()
        return rule

    def update_rule(self, rule: AlertRule) -> None:
        """Replace an existing rule, resetting its evaluation state."""
        if rule.rule_id not in self._rules:
            return
        self._rules[rule.rule_id] = rule
        # State must reset: a changed threshold invalidates any sustain timer
        # accumulated against the old one.
        self._state[rule.rule_id] = _RuleState()
        self._persist()
        self.rules_changed.emit()

    def remove_rule(self, rule_id: str) -> None:
        """Delete a rule."""
        if self._rules.pop(rule_id, None) is not None:
            self._state.pop(rule_id, None)
            self._persist()
            self.rules_changed.emit()

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        """Enable or disable one rule."""
        rule = self._rules.get(rule_id)
        if rule is None:
            return
        from dataclasses import replace

        self._rules[rule_id] = replace(rule, enabled=enabled)
        self._state[rule_id] = _RuleState()
        self._persist()
        self.rules_changed.emit()

    def restore_defaults(self) -> None:
        """Replace all rules with the default set."""
        self._rules = {rule.rule_id: rule for rule in self._default_rules()}
        self._state = {rule_id: _RuleState() for rule_id in self._rules}
        self._persist()
        self.rules_changed.emit()

    # ----------------------------------------------------------------- evaluation

    def evaluate(self, values: dict[str, float]) -> list[AlertEvent]:
        """Evaluate every rule against freshly extracted metric values.

        Rules whose metric is absent from ``values`` are left untouched rather
        than treated as not-breached: a GPU alert on a machine with no GPU should
        stay dormant, not oscillate.
        """
        now = time.monotonic()
        events: list[AlertEvent] = []
        for rule_id, rule in self._rules.items():
            if not rule.enabled or rule.metric not in values:
                continue
            state = self._state.setdefault(rule_id, _RuleState())
            event = self._evaluate_one(rule, state, values[rule.metric], now)
            if event is not None:
                events.append(event)
        for event in events:
            self._repository.record(event)
            self.alert_changed.emit(event)
        return events

    def _evaluate_one(
        self, rule: AlertRule, state: _RuleState, value: float, now: float
    ) -> AlertEvent | None:
        """Advance one rule's state machine, returning a transition if any."""
        state.last_value = value
        breached = rule.comparison.matches(value, rule.threshold)

        if breached:
            if state.breached_since is None:
                state.breached_since = now
            held_for = now - state.breached_since
            if not state.firing and held_for >= rule.sustain_seconds:
                state.firing = True
                state.last_notified = now
                return AlertEvent(rule, value, time.time(), active=True)
            return None

        # Not breached.  Clearing requires the value to retreat past the
        # threshold by a margin, so a metric sitting exactly on the boundary
        # cannot flap between firing and clear on alternate samples.
        if state.firing and self._clear_of(rule, value):
            state.firing = False
            state.breached_since = None
            return AlertEvent(rule, value, time.time(), active=False)
        if not state.firing:
            state.breached_since = None
        return None

    @staticmethod
    def _clear_of(rule: AlertRule, value: float) -> bool:
        """True when ``value`` has retreated far enough past the threshold."""
        margin = abs(rule.threshold) * _HYSTERESIS
        if rule.comparison is Comparison.ABOVE:
            return value < rule.threshold - margin
        return value > rule.threshold + margin

    # -------------------------------------------------------------------- status

    def active(self) -> list[tuple[AlertRule, float]]:
        """Currently firing rules with their latest values, worst first."""
        firing = [
            (self._rules[rule_id], state.last_value or 0.0)
            for rule_id, state in self._state.items()
            if state.firing and rule_id in self._rules
        ]
        return sorted(firing, key=lambda pair: pair[0].severity, reverse=True)

    def worst_severity(self) -> Severity:
        """The highest severity currently firing."""
        active = self.active()
        return max((rule.severity for rule, _ in active), default=Severity.OK)

    def is_firing(self, rule_id: str) -> bool:
        """True when one rule is currently firing."""
        state = self._state.get(rule_id)
        return bool(state and state.firing)

    def last_value(self, rule_id: str) -> float | None:
        """Most recent value observed for one rule's metric."""
        state = self._state.get(rule_id)
        return state.last_value if state else None

    def history(self, limit: int = 200) -> list[StoredAlert]:
        """Recorded alert transitions, newest first."""
        return self._repository.recent(limit)

    def clear_history(self) -> None:
        """Delete recorded alert transitions."""
        self._repository.clear()

    # ---------------------------------------------------------------- persistence

    def _persist(self) -> None:
        """Write the rule set into the settings document."""
        self._config.set(
            "alert_rules", [self._serialise(rule) for rule in self._rules.values()]
        )

    @staticmethod
    def _serialise(rule: AlertRule) -> dict:
        """Convert a rule to a JSON-safe mapping."""
        return {
            "rule_id": rule.rule_id,
            "metric": rule.metric,
            "comparison": rule.comparison.value,
            "threshold": rule.threshold,
            "severity": int(rule.severity),
            "label": rule.label,
            "enabled": rule.enabled,
            "sustain_seconds": rule.sustain_seconds,
            "notify": rule.notify,
        }

    @staticmethod
    def _deserialise(raw: dict) -> AlertRule | None:
        """Rebuild a rule from stored settings, tolerating malformed entries."""
        try:
            return AlertRule(
                rule_id=str(raw["rule_id"]),
                metric=str(raw["metric"]),
                comparison=Comparison(raw.get("comparison", ">")),
                threshold=float(raw["threshold"]),
                severity=Severity(int(raw.get("severity", Severity.WARNING))),
                label=str(raw.get("label", "")),
                enabled=bool(raw.get("enabled", True)),
                sustain_seconds=float(raw.get("sustain_seconds", 10.0)),
                notify=bool(raw.get("notify", True)),
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("Discarding malformed alert rule: %r", raw)
            return None
