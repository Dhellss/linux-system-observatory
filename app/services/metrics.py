"""Extraction of scalar metrics from domain snapshots.

Why a separate module?
----------------------
Three features need "the current value of a named metric": history recording,
alert evaluation and the dashboard's metric pickers.  Without a shared
abstraction each would need its own ``if domain == "cpu": ...`` ladder, and
adding a metric would mean editing three places.

Instead, each domain declares its scalar metrics once here as a dotted name and
an accessor.  History persists them, alerts compare them, and the settings UI
enumerates them -- all from the same registry.  Adding a new alertable metric is a
one-line change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.models.base import Unavailable, is_number
from app.models.cpu import CpuSnapshot
from app.models.gpu import GpuSnapshot
from app.models.memory import MemorySnapshot, PressureStall
from app.models.sensors import SensorSnapshot
from app.models.storage import StorageSnapshot


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """One extractable scalar metric."""

    #: Dotted identifier, e.g. ``cpu.usage``.  Stable; used as a database key.
    name: str
    #: Human-readable label for pickers and alert descriptions.
    label: str
    #: Unit hint driving formatting: "percent", "bytes", "celsius", "rate", "count".
    unit: str
    #: Extracts the value from a snapshot, or returns ``None`` when unavailable.
    #: Typed against ``Any`` rather than a specific snapshot because each
    #: extractor is registered alongside the domain whose snapshot it reads;
    #: the registry dispatches by domain, so the pairing is correct by
    #: construction and a narrower type would make every entry a cast.
    extract: Callable[[Any], float | None]
    #: Domain the metric belongs to, matching the collector's ``domain``.
    domain: str
    #: Sensible alert threshold, pre-filled in the rule editor.
    suggested_threshold: float = 0.0
    #: True when a *low* value is the problem (free space, battery charge).
    inverted: bool = False


def _safe(value: object) -> float | None:
    """Coerce a possibly-``Unavailable`` reading to a float, or ``None``."""
    if isinstance(value, Unavailable) or value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # reject NaN


# --------------------------------------------------------------------- extractors

def _cpu_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a CPU snapshot."""
    def temp(s: CpuSnapshot) -> float | None:
        return _safe(s.temperature)

    return [
        MetricDefinition("cpu.usage", "CPU usage", "percent",
                         lambda s: _safe(s.usage), "cpu", 90.0),
        MetricDefinition("cpu.temperature", "CPU temperature", "celsius",
                         temp, "cpu", 85.0),
        MetricDefinition("cpu.frequency", "CPU frequency", "mhz",
                         lambda s: _safe(s.frequency_mhz), "cpu"),
        MetricDefinition("cpu.load1", "Load average (1 min)", "count",
                         lambda s: _safe(s.load_average[0]), "cpu", 8.0),
        MetricDefinition("cpu.iowait", "CPU I/O wait", "percent",
                         lambda s: _safe(s.times.iowait), "cpu", 25.0),
        MetricDefinition("cpu.steal", "CPU steal time", "percent",
                         lambda s: _safe(s.times.steal), "cpu", 10.0),
        MetricDefinition("cpu.power", "CPU package power", "watts",
                         lambda s: _safe(s.package_power_w), "cpu"),
        MetricDefinition("cpu.context_switches", "Context switches", "rate",
                         lambda s: _safe(s.context_switches_per_s), "cpu"),
    ]


def _memory_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a memory snapshot."""
    def pressure(s: MemorySnapshot) -> float | None:
        stall = s.pressure_memory
        return stall.avg10 if isinstance(stall, PressureStall) else None

    return [
        MetricDefinition("memory.percent", "Memory usage", "percent",
                         lambda s: _safe(s.percent), "memory", 90.0),
        MetricDefinition("memory.used", "Memory used", "bytes",
                         lambda s: _safe(s.used_bytes), "memory"),
        MetricDefinition("memory.available", "Memory available", "bytes",
                         lambda s: _safe(s.available_bytes), "memory"),
        MetricDefinition("memory.cached", "Memory cached", "bytes",
                         lambda s: _safe(s.cached_bytes), "memory"),
        MetricDefinition("memory.swap_percent", "Swap usage", "percent",
                         lambda s: _safe(s.swap_percent), "memory", 50.0),
        MetricDefinition("memory.pressure", "Memory pressure (10 s)", "percent",
                         pressure, "memory", 20.0),
    ]


def _gpu_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a GPU snapshot, using the busiest adapter."""
    def primary(getter: Callable[[Any], object]) -> Callable[[Any], float | None]:
        """Wrap a per-adapter getter so it reads the busiest adapter."""
        def extract(snapshot: GpuSnapshot) -> float | None:
            gpu = snapshot.primary
            return _safe(getter(gpu)) if gpu is not None else None

        return extract

    return [
        MetricDefinition("gpu.usage", "GPU usage", "percent",
                         primary(lambda g: g.utilisation), "gpu", 95.0),
        MetricDefinition("gpu.temperature", "GPU temperature", "celsius",
                         primary(lambda g: g.temperature), "gpu", 85.0),
        MetricDefinition("gpu.vram_percent", "GPU memory usage", "percent",
                         primary(lambda g: g.vram_percent), "gpu", 90.0),
        MetricDefinition("gpu.vram_used", "GPU memory used", "bytes",
                         primary(lambda g: g.vram_used_bytes), "gpu"),
        MetricDefinition("gpu.power", "GPU power draw", "watts",
                         primary(lambda g: g.power_w), "gpu"),
        MetricDefinition("gpu.clock", "GPU core clock", "mhz",
                         primary(lambda g: g.core_clock_mhz), "gpu"),
        MetricDefinition("gpu.fan", "GPU fan speed", "percent",
                         primary(lambda g: g.fan_percent), "gpu"),
    ]


def _storage_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a storage snapshot."""
    def worst_filesystem(snapshot: StorageSnapshot) -> float | None:
        """The fullest mounted filesystem, which is the one that matters."""
        values = [
            p.percent for p in snapshot.filesystems
            if is_number(p.percent)
        ]
        return max(values) if values else None

    def hottest_disk(snapshot: StorageSnapshot) -> float | None:
        values = [
            d.health.temperature for d in snapshot.disks
            if is_number(d.health.temperature)
        ]
        return max(values) if values else None

    return [
        MetricDefinition("storage.read_rate", "Disk read rate", "rate",
                         lambda s: _safe(s.total_io.read_bytes_per_s), "storage"),
        MetricDefinition("storage.write_rate", "Disk write rate", "rate",
                         lambda s: _safe(s.total_io.write_bytes_per_s), "storage"),
        MetricDefinition("storage.utilisation", "Disk busy", "percent",
                         lambda s: _safe(s.total_io.utilisation_percent),
                         "storage", 95.0),
        MetricDefinition("storage.fullest", "Fullest filesystem", "percent",
                         worst_filesystem, "storage", 90.0),
        MetricDefinition("storage.temperature", "Hottest drive", "celsius",
                         hottest_disk, "storage", 65.0),
    ]


def _network_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a network snapshot."""
    return [
        MetricDefinition("network.download", "Download rate", "rate",
                         lambda s: _safe(s.totals.download_bytes_per_s), "network"),
        MetricDefinition("network.upload", "Upload rate", "rate",
                         lambda s: _safe(s.totals.upload_bytes_per_s), "network"),
        MetricDefinition("network.connections", "TCP connections", "count",
                         lambda s: _safe(s.protocols.tcp_total), "network", 2000.0),
        MetricDefinition("network.errors", "Interface errors", "count",
                         lambda s: _safe(s.totals.error_count), "network", 100.0),
        MetricDefinition("network.online", "Network online", "count",
                         lambda s: 1.0 if s.online else 0.0, "network", 1.0, True),
    ]


def _process_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a process snapshot."""
    return [
        MetricDefinition("processes.total", "Process count", "count",
                         lambda s: _safe(s.total), "processes", 800.0),
        MetricDefinition("processes.threads", "Thread count", "count",
                         lambda s: _safe(s.threads), "processes", 4000.0),
        MetricDefinition("processes.zombie", "Zombie processes", "count",
                         lambda s: _safe(s.zombie), "processes", 5.0),
        MetricDefinition("processes.running", "Runnable processes", "count",
                         lambda s: _safe(s.running), "processes"),
    ]


def _sensor_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a sensor snapshot."""
    def hottest(snapshot: SensorSnapshot) -> float | None:
        sensor = snapshot.hottest
        return _safe(sensor.value) if sensor is not None else None

    def max_fan(snapshot: SensorSnapshot) -> float | None:
        values = [_safe(f.value) for f in snapshot.fans()]
        present = [v for v in values if v is not None]
        return max(present) if present else None

    return [
        MetricDefinition("sensors.max_temperature", "Hottest sensor", "celsius",
                         hottest, "sensors", 85.0),
        MetricDefinition("sensors.max_fan", "Fastest fan", "count",
                         max_fan, "sensors"),
    ]


def _battery_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a battery snapshot."""
    return [
        MetricDefinition("battery.percent", "Battery charge", "percent",
                         lambda s: _safe(s.percent), "battery", 15.0, True),
        MetricDefinition("battery.health", "Battery health", "percent",
                         lambda s: _safe(s.health_percent), "battery", 60.0, True),
        MetricDefinition("battery.power", "Battery power flow", "watts",
                         lambda s: _safe(s.power_now_w), "battery"),
        MetricDefinition("battery.temperature", "Battery temperature", "celsius",
                         lambda s: _safe(s.temperature), "battery", 50.0),
    ]


def _service_metrics() -> list[MetricDefinition]:
    """Scalar metrics extracted from a systemd snapshot."""
    return [
        MetricDefinition("services.failed", "Failed services", "count",
                         lambda s: float(len(s.failed)), "services", 1.0),
        MetricDefinition("services.active", "Active units", "count",
                         lambda s: float(s.active_count), "services"),
    ]


class MetricRegistry:
    """All extractable metrics, indexed by name and by domain."""

    def __init__(self) -> None:
        definitions: list[MetricDefinition] = [
            *_cpu_metrics(), *_memory_metrics(), *_gpu_metrics(),
            *_storage_metrics(), *_network_metrics(), *_process_metrics(),
            *_sensor_metrics(), *_battery_metrics(), *_service_metrics(),
        ]
        self._by_name = {definition.name: definition for definition in definitions}
        self._by_domain: dict[str, list[MetricDefinition]] = {}
        for definition in definitions:
            self._by_domain.setdefault(definition.domain, []).append(definition)

    def __len__(self) -> int:
        return len(self._by_name)

    def get(self, name: str) -> MetricDefinition | None:
        """Look up one metric definition."""
        return self._by_name.get(name)

    def for_domain(self, domain: str) -> list[MetricDefinition]:
        """Metrics belonging to one domain."""
        return list(self._by_domain.get(domain, ()))

    def all(self) -> list[MetricDefinition]:
        """Every metric, in declaration order."""
        return list(self._by_name.values())

    def names(self) -> list[str]:
        """Every metric name."""
        return list(self._by_name)

    def extract(self, domain: str, snapshot: object) -> dict[str, float]:
        """Extract every available metric for ``domain`` from ``snapshot``.

        Metrics that cannot be read on this hardware are simply absent from the
        result, so neither history nor alerts ever see a fabricated zero.
        """
        values: dict[str, float] = {}
        for definition in self._by_domain.get(domain, ()):
            try:
                value = definition.extract(snapshot)
            except (AttributeError, TypeError, IndexError, ValueError):
                # A snapshot shape that does not match -- skip rather than fail
                # the whole extraction pass.
                continue
            if value is not None:
                values[definition.name] = value
        return values

    def grouped(self) -> Iterable[tuple[str, list[MetricDefinition]]]:
        """Metrics grouped by domain, for building menus."""
        return sorted(self._by_domain.items())
