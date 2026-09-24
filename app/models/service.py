"""systemd unit and journal models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


class UnitState(enum.Enum):
    """Normalised active state of a systemd unit."""

    ACTIVE = "Active"
    INACTIVE = "Inactive"
    FAILED = "Failed"
    ACTIVATING = "Activating"
    DEACTIVATING = "Deactivating"
    RELOADING = "Reloading"
    UNKNOWN = "Unknown"

    @classmethod
    def parse(cls, raw: str) -> UnitState:
        """Map a systemd ``ActiveState`` string onto this enum."""
        return {
            "active": cls.ACTIVE,
            "inactive": cls.INACTIVE,
            "failed": cls.FAILED,
            "activating": cls.ACTIVATING,
            "deactivating": cls.DEACTIVATING,
            "reloading": cls.RELOADING,
        }.get(raw.strip().lower(), cls.UNKNOWN)


@dataclass(frozen=True, slots=True)
class ServiceUnit:
    """One systemd unit."""

    name: str
    description: str
    state: UnitState
    sub_state: str = ""
    load_state: str = ""
    #: "enabled" | "disabled" | "static" | "masked" | ...
    enablement: Maybe[str] = None
    main_pid: Maybe[int] = None
    memory_bytes: Maybe[int] = None
    cpu_seconds: Maybe[float] = None
    tasks: Maybe[int] = None
    restart_count: Maybe[int] = None
    since: Maybe[float] = None
    #: Result of the last run: "success", "exit-code", "timeout", ...
    result: Maybe[str] = None
    fragment_path: Maybe[str] = None
    dependencies: tuple[str, ...] = ()
    reverse_dependencies: tuple[str, ...] = ()

    @property
    def is_failed(self) -> bool:
        """True when the unit is in a failed state."""
        return self.state is UnitState.FAILED

    @property
    def unit_type(self) -> str:
        """The suffix after the final dot, e.g. ``service`` or ``timer``."""
        return self.name.rpartition(".")[2] or "unit"


@dataclass(frozen=True, slots=True)
class BootTiming:
    """Boot performance figures from ``systemd-analyze``."""

    firmware_s: Maybe[float] = None
    loader_s: Maybe[float] = None
    kernel_s: Maybe[float] = None
    initrd_s: Maybe[float] = None
    userspace_s: Maybe[float] = None
    total_s: Maybe[float] = None
    #: Slowest units, as ``(unit_name, seconds)`` pairs.
    slowest: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceSnapshot(Snapshot):
    """One complete systemd sample."""

    units: tuple[ServiceUnit, ...] = ()
    boot: BootTiming = field(default_factory=BootTiming)
    note: str = ""

    @property
    def failed(self) -> list[ServiceUnit]:
        """Units currently in a failed state."""
        return [u for u in self.units if u.is_failed]

    @property
    def active_count(self) -> int:
        """Number of active units."""
        return sum(1 for u in self.units if u.state is UnitState.ACTIVE)


class LogPriority(enum.IntEnum):
    """syslog severity levels, as used by the journal."""

    EMERGENCY = 0
    ALERT = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    DEBUG = 7

    @property
    def label(self) -> str:
        """Title-cased display label."""
        return self.name.title()


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One journal record."""

    timestamp: float
    message: str
    priority: LogPriority = LogPriority.INFO
    unit: Maybe[str] = None
    identifier: Maybe[str] = None
    pid: Maybe[int] = None
    hostname: Maybe[str] = None
    #: Monotonic cursor, used to resume a follow without duplicating entries.
    cursor: Maybe[str] = None

    @property
    def is_problem(self) -> bool:
        """True for warning severity or worse."""
        return self.priority <= LogPriority.WARNING
