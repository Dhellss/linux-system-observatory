"""Memory, swap and pressure domain models."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


@dataclass(frozen=True, slots=True)
class SwapDevice:
    """One active swap area from ``/proc/swaps``."""

    path: str
    kind: str          # "partition" | "file"
    size_bytes: int
    used_bytes: int
    priority: int

    @property
    def percent(self) -> float:
        """Utilisation of this area."""
        return 100.0 * self.used_bytes / self.size_bytes if self.size_bytes else 0.0


@dataclass(frozen=True, slots=True)
class ZramDevice:
    """A compressed RAM block device.

    ``compression_ratio`` is the headline figure: it tells the user how much real
    RAM their swap-on-zram is actually saving, which raw byte counts do not.
    """

    name: str
    disk_size_bytes: int
    original_bytes: int
    compressed_bytes: int
    total_memory_bytes: int
    algorithm: Maybe[str] = None
    max_compression_streams: Maybe[int] = None

    @property
    def compression_ratio(self) -> float:
        """Original bytes per byte of physical RAM consumed."""
        if not self.compressed_bytes:
            return 0.0
        return self.original_bytes / self.compressed_bytes

    @property
    def saved_bytes(self) -> int:
        """RAM saved versus storing the same pages uncompressed."""
        return max(0, self.original_bytes - self.total_memory_bytes)


@dataclass(frozen=True, slots=True)
class HugePagePool:
    """A huge page pool of one size class."""

    size_kb: int
    total: int
    free: int
    reserved: int
    surplus: int

    @property
    def used(self) -> int:
        """Pages currently allocated."""
        return max(0, self.total - self.free)

    @property
    def total_bytes(self) -> int:
        """Pool capacity in bytes."""
        return self.total * self.size_kb * 1024


@dataclass(frozen=True, slots=True)
class PressureStall:
    """Kernel PSI averages for one resource.

    ``some`` is the share of time at least one task was stalled; ``full`` the
    share where every non-idle task was.  ``full`` above a few percent is the
    strongest single indicator of genuine memory exhaustion -- far more reliable
    than a high used-memory figure, which healthy page cache also produces.
    """

    avg10: float
    avg60: float
    avg300: float
    total_us: int


@dataclass(frozen=True, slots=True)
class MemorySnapshot(Snapshot):
    """One complete memory sample."""

    total_bytes: int = 0
    available_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    cached_bytes: int = 0
    buffers_bytes: int = 0
    shared_bytes: int = 0
    slab_bytes: Maybe[int] = None
    slab_reclaimable_bytes: Maybe[int] = None
    page_tables_bytes: Maybe[int] = None
    kernel_stack_bytes: Maybe[int] = None
    committed_bytes: Maybe[int] = None
    commit_limit_bytes: Maybe[int] = None
    dirty_bytes: Maybe[int] = None
    writeback_bytes: Maybe[int] = None
    mapped_bytes: Maybe[int] = None
    anonymous_bytes: Maybe[int] = None

    swap_total_bytes: int = 0
    swap_used_bytes: int = 0
    swap_free_bytes: int = 0
    swap_cached_bytes: Maybe[int] = None
    swap_devices: tuple[SwapDevice, ...] = ()

    zram_devices: tuple[ZramDevice, ...] = ()
    hugepages: tuple[HugePagePool, ...] = ()
    transparent_hugepages: Maybe[str] = None

    pressure_memory: Maybe[PressureStall] = None
    pressure_io: Maybe[PressureStall] = None
    pressure_cpu: Maybe[PressureStall] = None

    #: Byte counts for the stacked composition bar, in display order.
    composition: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @property
    def percent(self) -> float:
        """Used memory as a share of total."""
        return 100.0 * self.used_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def swap_percent(self) -> float:
        """Used swap as a share of total swap."""
        if not self.swap_total_bytes:
            return 0.0
        return 100.0 * self.swap_used_bytes / self.swap_total_bytes

    @property
    def cache_percent(self) -> float:
        """Page cache and buffers as a share of total memory."""
        if not self.total_bytes:
            return 0.0
        return 100.0 * (self.cached_bytes + self.buffers_bytes) / self.total_bytes
