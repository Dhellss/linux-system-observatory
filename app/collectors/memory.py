"""Memory, swap, zram and pressure collector.

Sources
-------
``/proc/meminfo``
    The authoritative source, read directly.  psutil's ``virtual_memory`` is a
    useful summary but hides the fields that explain *why* memory looks full --
    slab, page tables, dirty pages, commit limits.
``/proc/swaps`` and ``/sys/block/zram*``
    Per-area swap accounting.  zram deserves special handling because its
    interesting property is the compression ratio, which no aggregate reports.
``/proc/pressure/*``
    PSI, the only metric that reliably distinguishes "memory is full of useful
    cache" from "memory is exhausted and tasks are stalling".
"""

from __future__ import annotations

import time

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.memory import (
    HugePagePool,
    MemorySnapshot,
    PressureStall,
    SwapDevice,
    ZramDevice,
)
from app.utils import sysfs

KIB = 1024


class MemoryCollector(Collector[MemorySnapshot]):
    """Samples RAM, swap, compressed memory and kernel memory pressure."""

    domain = "memory"
    title = "Memory"

    def _collect(self) -> MemorySnapshot:
        now = time.monotonic()
        info = self._read_meminfo()

        total = info.get("MemTotal", 0)
        free = info.get("MemFree", 0)
        available = info.get("MemAvailable", free)
        buffers = info.get("Buffers", 0)
        cached = info.get("Cached", 0)
        sreclaimable = info.get("SReclaimable", 0)

        # "Used" is defined the way free(1) and every modern tool define it:
        # total minus available, NOT total minus free.  Page cache is available
        # to applications on demand, so counting it as used is what makes naive
        # monitors report 90% usage on a perfectly healthy machine.
        used = max(0, total - available)

        # Reclaimable slab is genuinely cache-like, so it is grouped with the
        # page cache in the composition bar, matching free(1)'s buff/cache.
        cache_total = cached + sreclaimable

        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        swap_used = max(0, swap_total - swap_free)

        composition = (
            ("Applications", max(0, used - buffers)),
            ("Cache", cache_total),
            ("Buffers", buffers),
            ("Free", free),
        )

        return MemorySnapshot(
            timestamp=now,
            total_bytes=total,
            available_bytes=available,
            used_bytes=used,
            free_bytes=free,
            cached_bytes=cache_total,
            buffers_bytes=buffers,
            shared_bytes=info.get("Shmem", 0),
            slab_bytes=info.get("Slab"),
            slab_reclaimable_bytes=info.get("SReclaimable"),
            page_tables_bytes=info.get("PageTables"),
            kernel_stack_bytes=info.get("KernelStack"),
            committed_bytes=info.get("Committed_AS"),
            commit_limit_bytes=info.get("CommitLimit"),
            dirty_bytes=info.get("Dirty"),
            writeback_bytes=info.get("Writeback"),
            mapped_bytes=info.get("Mapped"),
            anonymous_bytes=info.get("AnonPages"),
            swap_total_bytes=swap_total,
            swap_used_bytes=swap_used,
            swap_free_bytes=swap_free,
            swap_cached_bytes=info.get("SwapCached"),
            swap_devices=self._read_swaps(),
            zram_devices=self._read_zram(),
            hugepages=self._read_hugepages(info),
            transparent_hugepages=self._read_thp(),
            pressure_memory=self._read_pressure("memory"),
            pressure_io=self._read_pressure("io"),
            pressure_cpu=self._read_pressure("cpu"),
            composition=composition,
        )

    def _empty(self) -> MemorySnapshot:
        return MemorySnapshot()

    # ---------------------------------------------------------------- meminfo

    def _read_meminfo(self) -> dict[str, int]:
        """Parse ``/proc/meminfo`` into a byte-valued mapping.

        Values are converted from the file's kibibytes to bytes at the boundary
        so that no downstream code has to remember the unit.
        """
        result: dict[str, int] = {}
        for line in sysfs.read_lines(sysfs.PROC / "meminfo"):
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            # Every field is in kB except HugePages_* which are page counts.
            result[key.strip()] = value * KIB if len(parts) > 1 else value
        return result

    # ------------------------------------------------------------------- swap

    def _read_swaps(self) -> tuple[SwapDevice, ...]:
        """Enumerate active swap areas from ``/proc/swaps``."""
        devices: list[SwapDevice] = []
        lines = sysfs.read_lines(sysfs.PROC / "swaps")
        for line in lines[1:]:  # skip the header row
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                devices.append(
                    SwapDevice(
                        path=parts[0],
                        kind=parts[1],
                        size_bytes=int(parts[2]) * KIB,
                        used_bytes=int(parts[3]) * KIB,
                        priority=int(parts[4]),
                    )
                )
            except ValueError:
                continue
        return tuple(devices)

    # ------------------------------------------------------------------- zram

    def _read_zram(self) -> tuple[ZramDevice, ...]:
        """Read compressed RAM devices and their compression efficiency.

        Modern kernels expose ``mm_stat`` -- a single line of whitespace-
        separated counters.  Older ones had individual files.  Both are handled
        because zram-on-swap is common on exactly the kind of older or
        memory-constrained system where this matters most.
        """
        if not self.capabilities.zram:
            return ()
        devices: list[ZramDevice] = []
        for node in sysfs.glob("block/zram*"):
            disk_size = sysfs.read_int(node / "disksize")
            if not disk_size:
                continue  # an initialised-but-unused device
            original = compressed = total_mem = 0
            if raw := sysfs.read_text(node / "mm_stat"):
                fields = raw.split()
                # mm_stat layout: orig_data_size compr_data_size mem_used_total ...
                try:
                    original = int(fields[0])
                    compressed = int(fields[1])
                    total_mem = int(fields[2])
                except (IndexError, ValueError):
                    pass
            else:  # pre-4.1 kernels
                original = int(sysfs.read_int(node / "orig_data_size") or 0)
                compressed = int(sysfs.read_int(node / "compr_data_size") or 0)
                total_mem = int(sysfs.read_int(node / "mem_used_total") or 0)
            devices.append(
                ZramDevice(
                    name=node.name,
                    disk_size_bytes=int(disk_size),
                    original_bytes=original,
                    compressed_bytes=compressed,
                    total_memory_bytes=total_mem,
                    algorithm=self._zram_algorithm(node),
                    max_compression_streams=int(
                        sysfs.read_int(node / "max_comp_streams") or 0
                    ) or None,
                )
            )
        return tuple(devices)

    @staticmethod
    def _zram_algorithm(node) -> str | Unavailable:
        """Active compression algorithm.

        ``comp_algorithm`` lists every supported algorithm with the active one
        wrapped in square brackets, so the brackets must be parsed out.
        """
        raw = sysfs.read_text(node / "comp_algorithm")
        if not raw:
            return Unavailable(Reason.NOT_EXPOSED, "algorithm not published")
        for token in raw.split():
            if token.startswith("[") and token.endswith("]"):
                return token[1:-1]
        return raw.split()[0] if raw.split() else Unavailable(Reason.NOT_EXPOSED)

    # -------------------------------------------------------------- hugepages

    def _read_hugepages(self, info: dict[str, int]) -> tuple[HugePagePool, ...]:
        """Enumerate huge page pools of every size class."""
        pools: list[HugePagePool] = []
        for node in sysfs.glob("kernel/mm/hugepages/hugepages-*"):
            try:
                size_kb = int(node.name.split("-")[1].rstrip("kB"))
            except (IndexError, ValueError):
                continue
            total = int(sysfs.read_int(node / "nr_hugepages") or 0)
            if total == 0 and not sysfs.exists(node / "free_hugepages"):
                continue
            pools.append(
                HugePagePool(
                    size_kb=size_kb,
                    total=total,
                    free=int(sysfs.read_int(node / "free_hugepages") or 0),
                    reserved=int(sysfs.read_int(node / "resv_hugepages") or 0),
                    surplus=int(sysfs.read_int(node / "surplus_hugepages") or 0),
                )
            )
        if not pools and "HugePages_Total" in info:
            # Fall back to the meminfo summary when sysfs is unavailable.
            size_kb = info.get("Hugepagesize", 2048 * KIB) // KIB
            pools.append(
                HugePagePool(
                    size_kb=int(size_kb),
                    total=info.get("HugePages_Total", 0),
                    free=info.get("HugePages_Free", 0),
                    reserved=info.get("HugePages_Rsvd", 0),
                    surplus=info.get("HugePages_Surp", 0),
                )
            )
        return tuple(pools)

    @staticmethod
    def _read_thp() -> str | Unavailable:
        """Transparent huge pages mode, with the active option unbracketed."""
        raw = sysfs.read_text(sysfs.SYS / "kernel/mm/transparent_hugepage/enabled")
        if not raw:
            return Unavailable(Reason.UNSUPPORTED, "THP not compiled in")
        for token in raw.split():
            if token.startswith("["):
                return token.strip("[]")
        return raw

    # --------------------------------------------------------------- pressure

    def _read_pressure(self, resource: str) -> PressureStall | Unavailable:
        """Parse one ``/proc/pressure/*`` file.

        The ``full`` line is absent for CPU (a CPU cannot stall every task by
        definition), so the ``some`` line is always used for the averages and
        the distinction is left to the caller.
        """
        if not self.capabilities.psi:
            return self.capabilities.psi.as_unavailable()
        lines = sysfs.read_lines(sysfs.PROC / f"pressure/{resource}")
        if not lines:
            return Unavailable(Reason.NOT_EXPOSED, f"no PSI data for {resource}")
        # Prefer "full" where it exists: it indicates genuine starvation rather
        # than incidental contention.
        chosen = next((ln for ln in lines if ln.startswith("full")), lines[0])
        fields: dict[str, float] = {}
        for token in chosen.split()[1:]:
            key, _, value = token.partition("=")
            try:
                fields[key] = float(value)
            except ValueError:
                continue
        return PressureStall(
            avg10=fields.get("avg10", 0.0),
            avg60=fields.get("avg60", 0.0),
            avg300=fields.get("avg300", 0.0),
            total_us=int(fields.get("total", 0)),
        )
