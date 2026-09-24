"""Storage, filesystem and SMART domain models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot, is_number


class DiskKind(enum.Enum):
    """Physical media class, inferred from rotational and transport hints."""

    NVME = "NVMe"
    SSD = "SSD"
    HDD = "HDD"
    REMOVABLE = "Removable"
    OPTICAL = "Optical"
    LOOP = "Loop"
    VIRTUAL = "Virtual"
    UNKNOWN = "Unknown"


class SmartVerdict(enum.Enum):
    """Overall health assessment."""

    PASSED = "Healthy"
    FAILING = "Failing"
    WARNING = "Warning"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class SmartAttribute:
    """One SMART attribute (ATA) or health item (NVMe)."""

    identifier: str
    name: str
    value: str
    raw: Maybe[str] = None
    threshold: Maybe[str] = None
    worst: Maybe[str] = None
    #: True when this attribute alone indicates a problem.
    concerning: bool = False


@dataclass(frozen=True, slots=True)
class SmartHealth:
    """Device health summary drawn from SMART or the NVMe log page."""

    verdict: SmartVerdict = SmartVerdict.UNKNOWN
    temperature: Maybe[float] = None
    power_on_hours: Maybe[int] = None
    power_cycles: Maybe[int] = None
    #: Percentage of rated write endurance consumed (NVMe ``percentage_used``).
    wear_percent: Maybe[float] = None
    #: Remaining spare blocks, as a percentage.
    spare_percent: Maybe[float] = None
    data_written_bytes: Maybe[int] = None
    data_read_bytes: Maybe[int] = None
    reallocated_sectors: Maybe[int] = None
    pending_sectors: Maybe[int] = None
    crc_errors: Maybe[int] = None
    unsafe_shutdowns: Maybe[int] = None
    media_errors: Maybe[int] = None
    attributes: tuple[SmartAttribute, ...] = ()
    note: str = ""

    @property
    def estimated_life_left(self) -> Maybe[float]:
        """Remaining endurance as a percentage, when wear is reported."""
        if is_number(self.wear_percent):
            return max(0.0, 100.0 - self.wear_percent)
        return None


@dataclass(frozen=True, slots=True)
class IoCounters:
    """Rate-converted block I/O statistics for one device."""

    read_bytes_per_s: float = 0.0
    write_bytes_per_s: float = 0.0
    read_iops: float = 0.0
    write_iops: float = 0.0
    #: Fraction of the interval the device had at least one request in flight.
    utilisation_percent: Maybe[float] = None
    #: Average number of requests in flight (Little's law: throughput x latency).
    queue_length: Maybe[float] = None
    read_latency_ms: Maybe[float] = None
    write_latency_ms: Maybe[float] = None

    @property
    def total_bytes_per_s(self) -> float:
        """Combined read and write throughput."""
        return self.read_bytes_per_s + self.write_bytes_per_s


@dataclass(frozen=True, slots=True)
class Partition:
    """A partition, with its mount point when mounted."""

    name: str
    device: str
    size_bytes: int
    mountpoint: Maybe[str] = None
    filesystem: Maybe[str] = None
    label: Maybe[str] = None
    uuid: Maybe[str] = None
    total_bytes: Maybe[int] = None
    used_bytes: Maybe[int] = None
    free_bytes: Maybe[int] = None
    options: tuple[str, ...] = ()
    encrypted: bool = False

    @property
    def percent(self) -> Maybe[float]:
        """Space used as a percentage of capacity."""
        if isinstance(self.total_bytes, int) and self.total_bytes:
            used = self.used_bytes if isinstance(self.used_bytes, int) else 0
            return 100.0 * used / self.total_bytes
        return None


@dataclass(frozen=True, slots=True)
class Disk:
    """A physical or virtual block device and everything known about it."""

    name: str
    path: str
    kind: DiskKind
    size_bytes: int
    model: Maybe[str] = None
    serial: Maybe[str] = None
    firmware: Maybe[str] = None
    transport: Maybe[str] = None
    rotational: Maybe[bool] = None
    rotation_rate: Maybe[int] = None
    scheduler: Maybe[str] = None
    logical_block_size: Maybe[int] = None
    physical_block_size: Maybe[int] = None
    removable: bool = False
    partitions: tuple[Partition, ...] = ()
    io: IoCounters = field(default_factory=IoCounters)
    health: SmartHealth = field(default_factory=SmartHealth)

    @property
    def used_bytes(self) -> int:
        """Bytes used across mounted partitions of this disk."""
        return sum(
            p.used_bytes for p in self.partitions if isinstance(p.used_bytes, int)
        )


@dataclass(frozen=True, slots=True)
class StorageSnapshot(Snapshot):
    """One complete storage sample."""

    disks: tuple[Disk, ...] = ()
    #: Mounted filesystems including network and virtual ones, keyed by mountpoint.
    filesystems: tuple[Partition, ...] = ()
    total_io: IoCounters = field(default_factory=IoCounters)

    @property
    def total_capacity_bytes(self) -> int:
        """Combined capacity of every physical disk."""
        return sum(d.size_bytes for d in self.disks if d.kind not in
                   (DiskKind.LOOP, DiskKind.VIRTUAL))
