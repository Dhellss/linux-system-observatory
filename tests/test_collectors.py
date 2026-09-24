"""Integration tests that run the real collectors against this machine.

These cannot assert specific values -- the hardware differs per machine -- so
they assert the *contract* instead: a collector always returns its snapshot
type, never raises, and never reports a value it could not actually read.

That contract is the whole point of the design, and it is what these tests
protect.
"""

from __future__ import annotations

import time

import pytest
from app.collectors.battery import BatteryCollector
from app.collectors.cpu import CpuCollector
from app.collectors.gpu import GpuCollector
from app.collectors.memory import MemoryCollector
from app.collectors.network import NetworkCollector
from app.collectors.process import ProcessCollector
from app.collectors.sensors import SensorCollector
from app.collectors.services import ServiceCollector
from app.collectors.storage import StorageCollector
from app.collectors.system import SystemCollector
from app.models.base import Snapshot
from app.models.cpu import CpuSnapshot
from app.models.memory import MemorySnapshot

ALL_COLLECTORS = (
    CpuCollector, MemoryCollector, GpuCollector, StorageCollector,
    NetworkCollector, ProcessCollector, SensorCollector, BatteryCollector,
    SystemCollector, ServiceCollector,
)


@pytest.mark.parametrize("collector_class", ALL_COLLECTORS,
                         ids=lambda c: c.domain)
class TestCollectorContract:
    """Every collector must honour the same contract."""

    def test_returns_a_snapshot(self, collector_class, capabilities, hwmon) -> None:
        """Collection yields a snapshot of the declared type."""
        collector = collector_class(capabilities, hwmon)
        snapshot = collector.collect()
        assert isinstance(snapshot, Snapshot)

    def test_never_raises(self, collector_class, capabilities, hwmon) -> None:
        """Repeated collection is safe even on unusual hardware."""
        collector = collector_class(capabilities, hwmon)
        for _ in range(3):
            collector.collect()
        assert collector.healthy

    def test_reports_its_cost(self, collector_class, capabilities, hwmon) -> None:
        """Timing instrumentation is populated, for the diagnostics page."""
        collector = collector_class(capabilities, hwmon)
        collector.collect()
        assert collector.last_duration_ms >= 0.0

    def test_priming_is_safe(self, collector_class, capabilities, hwmon) -> None:
        """Priming never raises, whatever the hardware."""
        collector_class(capabilities, hwmon).prime()


class TestCpuCollector:
    """CPU-specific invariants."""

    @pytest.fixture
    def snapshot(self, capabilities, hwmon) -> CpuSnapshot:
        """A primed CPU sample, so its deltas reflect real elapsed time."""
        collector = CpuCollector(capabilities, hwmon)
        collector.prime()
        time.sleep(0.25)
        return collector.collect()

    def test_percentages_are_in_range(self, snapshot: CpuSnapshot) -> None:
        """Usage is a percentage, not a fraction or a jiffy count."""
        assert 0.0 <= snapshot.usage <= 100.0
        for core in snapshot.cores:
            assert 0.0 <= core.usage <= 100.0

    def test_time_breakdown_sums_to_one_hundred(self, snapshot: CpuSnapshot) -> None:
        """The breakdown accounts for the whole interval."""
        times = snapshot.times
        total = (
            times.user + times.system + times.idle + times.nice + times.iowait
            + times.irq + times.softirq + times.steal + times.guest
        )
        assert total == pytest.approx(100.0, abs=0.5)

    def test_one_core_entry_per_logical_cpu(self, snapshot: CpuSnapshot) -> None:
        """The per-core list matches the reported topology."""
        assert len(snapshot.cores) == snapshot.topology.logical_cores

    def test_topology_is_self_consistent(self, snapshot: CpuSnapshot) -> None:
        """Logical cores are never fewer than physical cores."""
        topology = snapshot.topology
        assert topology.logical_cores >= 1
        if isinstance(topology.physical_cores, int):
            assert topology.logical_cores >= topology.physical_cores


class TestMemoryCollector:
    """Memory-specific invariants."""

    @pytest.fixture
    def snapshot(self, capabilities, hwmon) -> MemorySnapshot:
        """A single memory sample."""
        return MemoryCollector(capabilities, hwmon).collect()

    def test_totals_are_coherent(self, snapshot: MemorySnapshot) -> None:
        """Used and available cannot exceed the total."""
        assert snapshot.total_bytes > 0
        assert 0 <= snapshot.used_bytes <= snapshot.total_bytes
        assert 0 <= snapshot.available_bytes <= snapshot.total_bytes

    def test_used_excludes_reclaimable_cache(self, snapshot: MemorySnapshot) -> None:
        """Used is total-minus-available, the definition free(1) uses.

        Counting page cache as used is the classic error that makes a healthy
        machine look 90% full.
        """
        assert snapshot.used_bytes == snapshot.total_bytes - snapshot.available_bytes

    def test_composition_covers_the_total(self, snapshot: MemorySnapshot) -> None:
        """The stacked bar's segments add up to roughly the installed memory."""
        total = sum(value for _label, value in snapshot.composition)
        assert total == pytest.approx(snapshot.total_bytes, rel=0.25)


class TestProcessCollector:
    """Process-table invariants."""

    def test_finds_this_process(self, capabilities, hwmon) -> None:
        """The test runner itself must appear in the table."""
        import os

        collector = ProcessCollector(capabilities, hwmon)
        snapshot = collector.collect()
        assert os.getpid() in snapshot.by_pid()

    def test_counts_are_consistent(self, capabilities, hwmon) -> None:
        """Reported totals match the actual sample."""
        snapshot = ProcessCollector(capabilities, hwmon).collect()
        assert snapshot.total == len(snapshot.processes)
        assert snapshot.threads >= snapshot.total

    def test_inspector_redacts_credentials(self, capabilities, hwmon) -> None:
        """Secrets in the environment are masked before display.

        The redaction function is exercised directly against a stub rather than
        a live process: ``/proc/<pid>/environ`` is frozen at exec time, so a
        variable set by the test would never appear there.
        """
        class _Stub:
            @staticmethod
            def environ() -> dict[str, str]:
                return {
                    "API_TOKEN": "super-secret-value",
                    "DB_PASSWORD": "hunter2",
                    "AWS_SECRET_ACCESS_KEY": "abc123",
                    "SESSION_COOKIE": "deadbeef",
                    "HOME": "/home/user",
                    "LANG": "en_GB.UTF-8",
                }

        collector = ProcessCollector(capabilities, hwmon)
        environment = collector._environment(_Stub())

        for key in ("API_TOKEN", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY",
                    "SESSION_COOKIE"):
            assert "redacted" in environment[key], key
        for secret in ("super-secret-value", "hunter2", "abc123", "deadbeef"):
            assert secret not in " ".join(environment.values())
        # Ordinary variables are left intact -- redaction must not be so broad
        # that the feature becomes useless.
        assert environment["HOME"] == "/home/user"
        assert environment["LANG"] == "en_GB.UTF-8"

    def test_inspector_reads_the_live_environment(self, capabilities, hwmon) -> None:
        """Inspecting a real process returns its actual environment."""
        import os

        collector = ProcessCollector(capabilities, hwmon)
        collector.collect()
        info = collector.inspect(os.getpid())
        assert info is not None
        assert info.environment.get("PATH")

    def test_refuses_to_signal_itself(self, capabilities, hwmon) -> None:
        """The monitor must not be able to kill its own process."""
        import os

        collector = ProcessCollector(capabilities, hwmon)
        ok, message = collector.kill(os.getpid())
        assert not ok
        assert "own process" in message

    def test_refuses_to_signal_init(self, capabilities, hwmon) -> None:
        """Signalling PID 1 would take the machine down."""
        collector = ProcessCollector(capabilities, hwmon)
        ok, _message = collector.kill(1)
        assert not ok


class TestNetworkCollector:
    """Network invariants."""

    def test_loopback_is_present_and_classified(self, capabilities, hwmon) -> None:
        """Every Linux machine has a loopback interface."""
        from app.models.network import InterfaceKind

        snapshot = NetworkCollector(capabilities, hwmon).collect()
        loopback = [i for i in snapshot.interfaces if i.name == "lo"]
        assert loopback, "no loopback interface found"
        assert loopback[0].kind is InterfaceKind.LOOPBACK

    def test_rates_are_never_negative(self, capabilities, hwmon) -> None:
        """Counter arithmetic cannot produce negative traffic."""
        collector = NetworkCollector(capabilities, hwmon)
        collector.prime()
        time.sleep(0.2)
        snapshot = collector.collect()
        for interface in snapshot.interfaces:
            assert interface.counters.download_bytes_per_s >= 0
            assert interface.counters.upload_bytes_per_s >= 0


class TestServiceCollectorSafety:
    """The systemd control path must reject anything it did not expect."""

    @pytest.fixture
    def collector(self, capabilities, hwmon) -> ServiceCollector:
        """A systemd collector; these tests never perform a unit action."""
        return ServiceCollector(capabilities, hwmon)

    @pytest.mark.parametrize("unit", [
        "evil; rm -rf /.service",
        "--version",
        "unit.service; reboot",
        "$(reboot).service",
        "`reboot`.service",
        "../../etc/passwd",
        "unit.service\nreboot",
        "",
        "a" * 400 + ".service",
    ])
    def test_rejects_malicious_unit_names(self, collector, unit) -> None:
        """A unit name can never smuggle an option or a shell construct."""
        ok, message = collector.control(unit, "start")
        assert not ok
        assert "suspicious" in message or "Unsupported" in message

    @pytest.mark.parametrize("action", ["frobnicate", "rm", "", "start;reboot"])
    def test_rejects_unknown_actions(self, collector, action) -> None:
        """Only the allow-listed verbs may reach systemctl."""
        ok, message = collector.control("sshd.service", action)
        assert not ok
        assert "Unsupported action" in message

    def test_accepts_a_well_formed_unit_name(self, collector) -> None:
        """The validator does not reject legitimate names.

        Only the name check is exercised; the action itself is not performed
        because these tests must not alter the machine.
        """
        for unit in ("sshd.service", "systemd-timesyncd.service",
                     "user@1000.service", "dev-sda1.device", "my-app.timer"):
            assert collector._valid_unit(unit), unit
