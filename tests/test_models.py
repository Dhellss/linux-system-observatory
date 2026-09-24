"""Tests for the domain models and the Unavailable sentinel."""

from __future__ import annotations

from app.models.base import (
    NO_HARDWARE,
    Reason,
    Snapshot,
    Unavailable,
    is_available,
    value_or,
)
from app.models.process import ProcessInfo, ProcessSnapshot, ProcessState
from app.models.sensors import BatterySnapshot, Sensor, SensorKind, ThermalStatus


class TestUnavailable:
    """The absence sentinel."""

    def test_is_falsy(self) -> None:
        """``if value:`` reads naturally against an absent reading."""
        assert not Unavailable(Reason.NO_HARDWARE)
        assert not NO_HARDWARE

    def test_stringifies_to_an_explanation(self) -> None:
        """The user is told why, not merely that."""
        marker = Unavailable(Reason.NO_TOOL, "smartctl")
        assert "not installed" in str(marker)
        assert "smartctl" in str(marker)

    def test_actionable_distinguishes_fixable_causes(self) -> None:
        """A missing tool is actionable; absent hardware is not."""
        assert Unavailable(Reason.NO_TOOL).actionable
        assert Unavailable(Reason.PERMISSION).actionable
        assert not Unavailable(Reason.NO_HARDWARE).actionable

    def test_helpers(self) -> None:
        """The convenience predicates behave as documented."""
        assert value_or(NO_HARDWARE, 5) == 5
        assert value_or(12, 5) == 12
        assert not is_available(NO_HARDWARE)
        assert not is_available(None)
        assert is_available(0)


class TestSnapshot:
    """The snapshot base class."""

    def test_age_is_never_negative(self) -> None:
        """A monotonic clock guarantees a non-negative age."""
        assert Snapshot().age >= 0.0


class TestProcessTree:
    """Process hierarchy construction."""

    @staticmethod
    def _process(pid: int, ppid: int | None, **kwargs) -> ProcessInfo:
        return ProcessInfo(
            pid=pid, name=f"proc{pid}", state=ProcessState.SLEEPING,
            ppid=ppid, **kwargs,
        )

    def test_builds_a_hierarchy(self) -> None:
        """Children are nested beneath their parent."""
        snapshot = ProcessSnapshot(processes=(
            self._process(1, None),
            self._process(2, 1),
            self._process(3, 1),
            self._process(4, 2),
        ))
        roots = snapshot.build_tree()
        assert len(roots) == 1
        assert len(roots[0].children) == 2
        assert roots[0].children[0].children[0].process.pid == 4

    def test_orphans_become_roots(self) -> None:
        """A process whose parent exited is still shown, not dropped."""
        snapshot = ProcessSnapshot(processes=(
            self._process(1, None),
            self._process(99, 12345),  # parent is not in the sample
        ))
        roots = snapshot.build_tree()
        assert {node.process.pid for node in roots} == {1, 99}

    def test_self_parent_does_not_recurse(self) -> None:
        """A process claiming to be its own parent must not hang the tree."""
        snapshot = ProcessSnapshot(processes=(self._process(7, 7),))
        roots = snapshot.build_tree()
        assert len(roots) == 1
        assert roots[0].children == ()

    def test_subtree_aggregation(self) -> None:
        """A node reports the resources of its whole subtree."""
        snapshot = ProcessSnapshot(processes=(
            self._process(1, None, cpu_percent=1.0, memory_rss_bytes=100),
            self._process(2, 1, cpu_percent=2.0, memory_rss_bytes=200),
            self._process(3, 2, cpu_percent=4.0, memory_rss_bytes=400),
        ))
        root = snapshot.build_tree()[0]
        assert root.subtree_cpu == 7.0
        assert root.subtree_memory == 700

    def test_top_consumers_are_ordered(self) -> None:
        """Ranking helpers sort descending."""
        snapshot = ProcessSnapshot(processes=(
            self._process(1, None, cpu_percent=5.0, memory_rss_bytes=10),
            self._process(2, None, cpu_percent=50.0, memory_rss_bytes=1),
        ))
        assert snapshot.top_cpu(1)[0].pid == 2
        assert snapshot.top_memory(1)[0].pid == 1


class TestThermalStatus:
    """Temperature banding against hardware-reported limits."""

    @staticmethod
    def _sensor(value: float, high: float | None, critical: float | None) -> Sensor:
        return Sensor(
            key="k", label="Test", kind=SensorKind.TEMPERATURE,
            value=value, high=high, critical=critical,
        )

    def test_uses_hardware_limits_not_fixed_numbers(self) -> None:
        """The same reading is judged against each part's own limits.

        85 °C is unremarkable on a CPU rated to 105 °C but past the warning
        point of a part rated to 90 °C. A fixed threshold could not express
        both.
        """
        assert self._sensor(85, 95, 105).status is ThermalStatus.WARM
        assert self._sensor(85, 80, 90).status is ThermalStatus.HOT
        assert self._sensor(70, 95, 105).status is ThermalStatus.NORMAL

    def test_integer_readings_are_assessed(self) -> None:
        """A whole-number temperature is a reading, not an absence.

        Guards a real defect: an ``isinstance(value, float)`` availability test
        classified every integer sensor reading as unknown.
        """
        assert self._sensor(95, 80, 100).status is ThermalStatus.HOT
        assert self._sensor(100, 80, 100).status is ThermalStatus.CRITICAL

    def test_critical_at_the_limit(self) -> None:
        """Reaching the advertised critical point reports critical."""
        assert self._sensor(100, 90, 100).status is ThermalStatus.CRITICAL

    def test_falls_back_when_no_limits_published(self) -> None:
        """Without limits, conservative defaults apply."""
        assert self._sensor(50, None, None).status is ThermalStatus.NORMAL
        assert self._sensor(95, None, None).status is ThermalStatus.CRITICAL

    def test_non_temperature_sensors_are_unknown(self) -> None:
        """A fan has no thermal status."""
        fan = Sensor(key="f", label="Fan", kind=SensorKind.FAN, value=1200)
        assert fan.status is ThermalStatus.UNKNOWN


class TestBatteryHealth:
    """Battery health arithmetic."""

    def test_health_is_full_over_design(self) -> None:
        """Health is present capacity as a share of the factory rating."""
        battery = BatterySnapshot(
            present=True, energy_full_wh=32.0, energy_design_wh=46.0
        )
        assert battery.health_percent == 32.0 / 46.0 * 100
        assert round(battery.wear_percent, 1) == round(100 - 32 / 46 * 100, 1)

    def test_health_absent_without_design_capacity(self) -> None:
        """Health cannot be computed without a design figure."""
        battery = BatterySnapshot(present=True, energy_full_wh=32.0)
        assert battery.health_percent is None

    def test_health_never_exceeds_one_hundred(self) -> None:
        """A battery reporting above its design rating is clamped."""
        battery = BatterySnapshot(
            present=True, energy_full_wh=50.0, energy_design_wh=46.0
        )
        assert battery.health_percent == 100.0
