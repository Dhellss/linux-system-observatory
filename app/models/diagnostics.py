"""Diagnostics and alerting models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Severity(enum.IntEnum):
    """Shared severity scale for both diagnostics and alerts.

    Ordered so that ``max()`` over a collection yields the worst item, and
    integer comparison expresses "at least this bad".
    """

    OK = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        """Display label."""
        return {
            Severity.OK: "Healthy",
            Severity.INFO: "Notice",
            Severity.WARNING: "Warning",
            Severity.CRITICAL: "Critical",
        }[self]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one diagnostic check.

    ``advice`` is mandatory in spirit: a health check that reports a problem
    without suggesting what to do about it has only done half the job.
    """

    check_id: str
    title: str
    severity: Severity
    summary: str
    advice: str = ""
    category: str = "General"
    #: Supporting measurements, rendered as a detail table.
    evidence: dict[str, str] = field(default_factory=dict)
    #: True when the check could not run (missing capability rather than a fault).
    skipped: bool = False

    @property
    def passed(self) -> bool:
        """True when the check ran and found nothing wrong."""
        return not self.skipped and self.severity is Severity.OK


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """A full diagnostics pass."""

    results: tuple[CheckResult, ...]
    generated_at: float
    duration_s: float

    @property
    def worst(self) -> Severity:
        """The most severe finding in the report."""
        actionable = [r.severity for r in self.results if not r.skipped]
        return max(actionable) if actionable else Severity.OK

    @property
    def problems(self) -> list[CheckResult]:
        """Findings at warning severity or worse, most severe first."""
        return sorted(
            (r for r in self.results
             if not r.skipped and r.severity >= Severity.WARNING),
            key=lambda r: r.severity,
            reverse=True,
        )

    @property
    def counts(self) -> dict[Severity, int]:
        """Number of results at each severity."""
        tally = dict.fromkeys(Severity, 0)
        for result in self.results:
            if not result.skipped:
                tally[result.severity] += 1
        return tally


class Comparison(enum.Enum):
    """Threshold comparison operators available to alert rules."""

    ABOVE = ">"
    BELOW = "<"

    def matches(self, value: float, threshold: float) -> bool:
        """Evaluate this comparison."""
        return value > threshold if self is Comparison.ABOVE else value < threshold


@dataclass(frozen=True, slots=True)
class AlertRule:
    """A user-defined threshold rule.

    ``sustain_seconds`` exists to suppress the single most annoying failure mode
    of threshold alerting: a one-sample CPU spike from opening a browser tab
    firing a "CPU critical" notification.  A rule only triggers once its
    condition has held continuously for the configured duration.
    """

    rule_id: str
    #: Dotted metric path, e.g. ``cpu.usage`` or ``sensors.max_temperature``.
    metric: str
    comparison: Comparison
    threshold: float
    severity: Severity = Severity.WARNING
    label: str = ""
    enabled: bool = True
    sustain_seconds: float = 10.0
    notify: bool = True

    @property
    def description(self) -> str:
        """Human-readable statement of the rule."""
        name = self.label or self.metric
        return f"{name} {self.comparison.value} {self.threshold:g}"


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A rule transition -- either firing or clearing."""

    rule: AlertRule
    value: float
    timestamp: float
    #: True when the condition began, false when it ended.
    active: bool

    @property
    def headline(self) -> str:
        """Notification title."""
        verb = "triggered" if self.active else "cleared"
        return f"{self.rule.label or self.rule.metric} {verb}"

    @property
    def detail(self) -> str:
        """Notification body."""
        return f"Current value {self.value:.1f} (threshold {self.rule.threshold:g})"
