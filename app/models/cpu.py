"""CPU domain models."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


@dataclass(frozen=True, slots=True)
class CpuTimes:
    """Normalised CPU time breakdown, as percentages of the sampling interval.

    Derived from deltas between consecutive ``/proc/stat`` reads rather than from
    cumulative totals, so the values describe *this interval* and not the whole
    uptime.
    """

    user: float = 0.0
    system: float = 0.0
    idle: float = 0.0
    nice: float = 0.0
    iowait: float = 0.0
    irq: float = 0.0
    softirq: float = 0.0
    steal: float = 0.0
    guest: float = 0.0

    @property
    def busy(self) -> float:
        """Total non-idle percentage (everything except ``idle`` and ``iowait``)."""
        return max(0.0, 100.0 - self.idle - self.iowait)

    def as_pairs(self) -> list[tuple[str, float]]:
        """Display-ordered ``(label, percentage)`` pairs for the breakdown chart."""
        return [
            ("User", self.user),
            ("System", self.system),
            ("Nice", self.nice),
            ("I/O wait", self.iowait),
            ("IRQ", self.irq),
            ("SoftIRQ", self.softirq),
            ("Steal", self.steal),
            ("Guest", self.guest),
            ("Idle", self.idle),
        ]


@dataclass(frozen=True, slots=True)
class CoreSample:
    """Per-logical-core instantaneous state."""

    index: int
    usage: float
    frequency_mhz: Maybe[float]
    #: Physical package this core belongs to (for multi-socket topology).
    package: Maybe[int]
    #: Physical core within the package; siblings share this value (SMT).
    core_id: Maybe[int]
    temperature: Maybe[float]
    online: bool = True


@dataclass(frozen=True, slots=True)
class CpuTopology:
    """Static description of the processor, probed once."""

    model: str = "Unknown"
    vendor: str = "Unknown"
    architecture: str = "Unknown"
    byte_order: str = "Unknown"
    sockets: Maybe[int] = 1
    physical_cores: Maybe[int] = None
    logical_cores: int = 1
    threads_per_core: Maybe[int] = None
    #: ``{package_index: {core_id: [logical_cpu, ...]}}`` -- the full SMT map.
    packages: dict[int, dict[int, list[int]]] = field(default_factory=dict)
    cache: dict[str, str] = field(default_factory=dict)
    flags: tuple[str, ...] = ()
    stepping: Maybe[str] = None
    family: Maybe[str] = None
    microcode: Maybe[str] = None
    bogomips: Maybe[float] = None
    numa_nodes: Maybe[int] = None
    virtualisation: Maybe[str] = None

    @property
    def smt_enabled(self) -> bool:
        """True when simultaneous multithreading is active."""
        threads = self.threads_per_core
        return isinstance(threads, int) and threads > 1


@dataclass(frozen=True, slots=True)
class CpuSnapshot(Snapshot):
    """One complete CPU sample."""

    usage: float = 0.0
    times: CpuTimes = field(default_factory=CpuTimes)
    cores: tuple[CoreSample, ...] = ()
    frequency_mhz: Maybe[float] = None
    frequency_min_mhz: Maybe[float] = None
    frequency_max_mhz: Maybe[float] = None
    governor: Maybe[str] = None
    available_governors: tuple[str, ...] = ()
    driver: Maybe[str] = None
    turbo_enabled: Maybe[bool] = None
    temperature: Maybe[float] = None
    temperature_high: Maybe[float] = None
    temperature_critical: Maybe[float] = None
    package_power_w: Maybe[float] = None
    load_average: tuple[float, float, float] = (0.0, 0.0, 0.0)
    context_switches_per_s: Maybe[float] = None
    interrupts_per_s: Maybe[float] = None
    forks_per_s: Maybe[float] = None
    processes_running: Maybe[int] = None
    processes_blocked: Maybe[int] = None
    pressure_some: Maybe[float] = None
    #: Static topology, carried on the snapshot so views need one data source.
    topology: CpuTopology = field(default_factory=CpuTopology)

    @property
    def core_usages(self) -> list[float]:
        """Per-core usage percentages in logical-CPU order."""
        return [core.usage for core in self.cores]

    @property
    def hottest_core(self) -> CoreSample | None:
        """The core reporting the highest usage this interval."""
        return max(self.cores, key=lambda c: c.usage, default=None)
