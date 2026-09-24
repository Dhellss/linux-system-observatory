"""Storage collector: block devices, filesystems, I/O rates and health.

Sources
-------
``/sys/block/*``
    Device topology, rotational flag, model, scheduler and queue depth.  sysfs is
    preferred over ``lsblk`` because it needs no subprocess and no JSON parsing.
``/proc/diskstats``
    The canonical I/O counters.  psutil exposes these too, but reading the file
    directly gives access to fields 10 and 11 (in-flight requests and weighted
    I/O time) that psutil discards -- and those are what make queue depth and
    device utilisation calculable.
``smartctl``
    Health, on a long cadence and cached.
"""

from __future__ import annotations

import time
from typing import TypedDict

import psutil

from app.collectors.base import Collector
from app.collectors.smart import SmartReader
from app.models.base import Reason, Unavailable, is_number
from app.models.storage import (
    Disk,
    DiskKind,
    IoCounters,
    Partition,
    SmartHealth,
    StorageSnapshot,
)
from app.utils import sysfs

#: Sector size assumed by ``/proc/diskstats``, fixed by the kernel ABI at 512 B
#: regardless of the device's real logical block size.
DISKSTATS_SECTOR = 512


class _Topology(TypedDict):
    """Static attributes cached per block device.

    A TypedDict rather than a plain ``dict[str, object]`` so the field types
    survive into the snapshot construction below; with ``object`` every value
    needed a cast, and a wrong key would only surface at run time.
    """

    size: int
    model: str | None
    serial: str | None
    firmware: str | None
    rotational: bool | None
    scheduler: str | None
    logical_block: int | None
    physical_block: int | None
    removable: bool
    transport: str | None
    rotation_rate: int | None


def _as_int(value: float | None) -> int | None:
    """Narrow a sysfs reading to ``int``.

    ``sysfs.read_int`` returns a float because it supports unit scaling; block
    sizes and rotation rates are conceptually integers, so they are narrowed here
    rather than leaking ``512.0`` into the UI.
    """
    return None if value is None else int(value)

#: Pseudo-filesystems excluded from the filesystem list: they report the memory
#: they occupy, not durable storage, and would distort capacity totals.
_VIRTUAL_FILESYSTEMS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "pstore",
    "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "hugetlbfs",
    "mqueue", "bpf", "binfmt_misc", "autofs", "efivarfs", "ramfs", "nsfs",
    "squashfs", "overlay", "rpc_pipefs", "selinuxfs",
})


class StorageCollector(Collector[StorageSnapshot]):
    """Samples block devices, mounted filesystems and I/O throughput."""

    domain = "storage"
    title = "Storage"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._smart = SmartReader(capabilities)
        self._health_cache: dict[str, SmartHealth] = {}
        self._health_read_at: dict[str, float] = {}
        self._topology_cache: dict[str, _Topology] = {}
        #: Seconds between SMART reads.  Health changes over days, and each read
        #: costs a subprocess and can wake a sleeping disk.
        self._health_interval = 300.0

    def _collect(self) -> StorageSnapshot:
        now = time.monotonic()
        diskstats = self._read_diskstats(now)
        mounts = self._read_mounts()
        nvme_temps = self._read_nvme_temperatures()

        disks: list[Disk] = []
        for node in self._block_devices():
            name = node.name
            topology = self._topology(node)
            partitions = self._partitions_of(node, mounts)
            disks.append(
                Disk(
                    name=name,
                    path=f"/dev/{name}",
                    kind=self._classify(node, topology),
                    size_bytes=topology["size"],
                    model=topology["model"] or Unavailable(
                        Reason.NOT_EXPOSED, "model not published"
                    ),
                    serial=topology["serial"] or Unavailable(
                        Reason.PERMISSION, "serial requires elevated privileges"
                    ),
                    firmware=topology["firmware"] or Unavailable(
                        Reason.NOT_EXPOSED, "firmware revision not published"
                    ),
                    transport=topology["transport"] or Unavailable(
                        Reason.NOT_EXPOSED, "transport unknown"
                    ),
                    rotational=topology["rotational"],
                    scheduler=topology["scheduler"] or Unavailable(
                        Reason.NOT_EXPOSED, "no I/O scheduler node"
                    ),
                    logical_block_size=topology["logical_block"],
                    physical_block_size=topology["physical_block"],
                    removable=topology["removable"],
                    partitions=partitions,
                    io=diskstats.get(name, IoCounters()),
                    health=self._health(name, now, nvme_temps),
                )
            )

        return StorageSnapshot(
            timestamp=now,
            disks=tuple(disks),
            filesystems=tuple(mounts.values()),
            total_io=self._aggregate_io(diskstats, disks),
        )

    def _empty(self) -> StorageSnapshot:
        return StorageSnapshot()

    # ------------------------------------------------------------------ devices

    def _block_devices(self) -> list:
        """Whole-disk block devices, excluding partitions and zram.

        A device is a whole disk if it has no ``partition`` file.  zram is
        excluded because it is compressed RAM reported on the memory page, and
        including it in storage totals would double-count.
        """
        devices = []
        for node in sysfs.glob("block/*"):
            name = node.name
            if name.startswith(("zram", "ram", "dm-")):
                continue
            if sysfs.exists(node / "partition"):
                continue
            devices.append(node)
        return devices

    def _topology(self, node) -> _Topology:
        """Static device attributes, read once per device and cached."""
        name = node.name
        if name in self._topology_cache:
            # Size can change (a resized virtual disk), so it is refreshed even
            # though everything else is genuinely static.
            cached = self._topology_cache[name]
            sectors = sysfs.read_int(node / "size")
            cached["size"] = int(sectors * DISKSTATS_SECTOR) if sectors else 0
            return cached

        sectors = sysfs.read_int(node / "size")
        rotational = sysfs.read_int(node / "queue/rotational")
        info: _Topology = {
            "size": int(sectors * DISKSTATS_SECTOR) if sectors else 0,
            "model": sysfs.read_text(node / "device/model")
            or sysfs.read_text(node / "device/name"),
            "serial": sysfs.read_text(node / "device/serial"),
            "firmware": sysfs.read_text(node / "device/firmware_rev")
            or sysfs.read_text(node / "device/rev"),
            "rotational": None if rotational is None else bool(rotational),
            "scheduler": self._active_scheduler(node),
            "logical_block": _as_int(sysfs.read_int(node / "queue/logical_block_size")),
            "physical_block": _as_int(
                sysfs.read_int(node / "queue/physical_block_size")
            ),
            "removable": bool(sysfs.read_int(node / "removable")),
            "transport": self._transport(node),
            "rotation_rate": _as_int(sysfs.read_int(node / "queue/rotation_rate")),
        }
        self._topology_cache[name] = info
        return info

    @staticmethod
    def _active_scheduler(node) -> str | None:
        """Active I/O scheduler, unbracketed from the list of available ones."""
        raw = sysfs.read_text(node / "queue/scheduler")
        if not raw:
            return None
        for token in raw.split():
            if token.startswith("["):
                return token.strip("[]")
        return raw.split()[0] if raw.split() else None

    @staticmethod
    def _transport(node) -> str | None:
        """Bus the device is attached to, inferred from its sysfs path."""
        try:
            target = str(node.resolve())
        except OSError:
            return None
        for marker, label in (
            ("nvme", "NVMe"), ("usb", "USB"), ("ata", "SATA"),
            ("scsi", "SCSI"), ("mmc", "SD / eMMC"), ("virtio", "VirtIO"),
        ):
            if marker in target:
                return label
        return None

    def _classify(self, node, topology: _Topology) -> DiskKind:
        """Determine the media class of a device."""
        name = node.name
        if name.startswith("nvme"):
            return DiskKind.NVME
        if name.startswith(("loop",)):
            return DiskKind.LOOP
        if name.startswith(("sr", "scd")):
            return DiskKind.OPTICAL
        if name.startswith(("vd", "xvd")):
            return DiskKind.VIRTUAL
        if topology["removable"]:
            return DiskKind.REMOVABLE
        rotational = topology["rotational"]
        if rotational is True:
            return DiskKind.HDD
        if rotational is False:
            return DiskKind.SSD
        return DiskKind.UNKNOWN

    # ----------------------------------------------------------------- diskstats

    def _read_diskstats(self, now: float) -> dict[str, IoCounters]:
        """Convert ``/proc/diskstats`` counters into per-device rates.

        Field layout (1-indexed, per Documentation/admin-guide/iostats.rst)::

            1 major  2 minor  3 name
            4 reads  5 read merges  6 sectors read  7 read ticks (ms)
            8 writes 9 write merges 10 sectors written 11 write ticks (ms)
            12 in-flight  13 io_ticks (ms)  14 weighted io_ticks (ms)

        ``io_ticks`` is the key to utilisation: it counts milliseconds during
        which the queue was non-empty, so its delta over the interval gives the
        busy fraction directly -- the same figure ``iostat`` prints as %util.
        """
        counters: dict[str, IoCounters] = {}
        for line in sysfs.read_lines(sysfs.PROC / "diskstats"):
            fields = line.split()
            if len(fields) < 14:
                continue
            name = fields[2]
            try:
                reads = float(fields[3])
                sectors_read = float(fields[5])
                read_ticks = float(fields[6])
                writes = float(fields[7])
                sectors_written = float(fields[9])
                write_ticks = float(fields[10])
                in_flight = float(fields[11])
                io_ticks = float(fields[12])
            except (ValueError, IndexError):
                continue

            read_iops = self.rates.rate(f"disk.{name}.r", reads, now)
            write_iops = self.rates.rate(f"disk.{name}.w", writes, now)
            busy_ms_per_s = self.rates.rate(f"disk.{name}.busy", io_ticks, now)
            read_ticks_delta = self.rates.delta(f"disk.{name}.rt", read_ticks, now)
            write_ticks_delta = self.rates.delta(f"disk.{name}.wt", write_ticks, now)
            read_delta = self.rates.delta(f"disk.{name}.rd", reads, now)
            write_delta = self.rates.delta(f"disk.{name}.wd", writes, now)

            counters[name] = IoCounters(
                read_bytes_per_s=self.rates.rate(
                    f"disk.{name}.rb", sectors_read * DISKSTATS_SECTOR, now
                ),
                write_bytes_per_s=self.rates.rate(
                    f"disk.{name}.wb", sectors_written * DISKSTATS_SECTOR, now
                ),
                read_iops=read_iops,
                write_iops=write_iops,
                # busy_ms_per_s is milliseconds of busy time per second, so
                # dividing by 10 converts it to a percentage.
                utilisation_percent=min(100.0, busy_ms_per_s / 10.0),
                queue_length=in_flight,
                read_latency_ms=(
                    read_ticks_delta / read_delta if read_delta > 0 else None
                ),
                write_latency_ms=(
                    write_ticks_delta / write_delta if write_delta > 0 else None
                ),
            )
        return counters

    def _aggregate_io(
        self, diskstats: dict[str, IoCounters], disks: list[Disk]
    ) -> IoCounters:
        """Sum I/O across physical disks only.

        Partitions appear in diskstats alongside their parent disk, so summing
        every row would double-count every byte.
        """
        physical = [d.name for d in disks
                    if d.kind not in (DiskKind.LOOP, DiskKind.VIRTUAL)]
        selected = [diskstats[name] for name in physical if name in diskstats]
        if not selected:
            return IoCounters()
        return IoCounters(
            read_bytes_per_s=sum(c.read_bytes_per_s for c in selected),
            write_bytes_per_s=sum(c.write_bytes_per_s for c in selected),
            read_iops=sum(c.read_iops for c in selected),
            write_iops=sum(c.write_iops for c in selected),
            utilisation_percent=max(
                (c.utilisation_percent for c in selected
                 if is_number(c.utilisation_percent)), default=None
            ),
            queue_length=sum(
                c.queue_length for c in selected if is_number(c.queue_length)
            ),
        )

    # ---------------------------------------------------------------- mounts

    def _read_mounts(self) -> dict[str, Partition]:
        """Enumerate mounted filesystems with their usage, keyed by device name.

        ``psutil.disk_usage`` performs a ``statvfs`` per mount point, which can
        block indefinitely on an unresponsive network mount.  Network
        filesystems are therefore skipped entirely rather than risking a stall
        of the whole collector.
        """
        mounts: dict[str, Partition] = {}
        try:
            entries = psutil.disk_partitions(all=False)
        except (OSError, RuntimeError):
            return mounts

        for entry in entries:
            if entry.fstype in _VIRTUAL_FILESYSTEMS:
                continue
            total = used = free = None
            if not self._is_network(entry.fstype):
                try:
                    usage = psutil.disk_usage(entry.mountpoint)
                    total, used, free = usage.total, usage.used, usage.free
                except (OSError, PermissionError):
                    pass

            device_name = entry.device.rsplit("/", 1)[-1]
            mounts[device_name] = Partition(
                name=device_name,
                device=entry.device,
                size_bytes=total or 0,
                mountpoint=entry.mountpoint,
                filesystem=entry.fstype or Unavailable(
                    Reason.NOT_EXPOSED, "filesystem type unknown"
                ),
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                options=tuple(entry.opts.split(",")) if entry.opts else (),
                encrypted="crypt" in entry.device or entry.fstype == "crypto_LUKS",
            )
        return mounts

    @staticmethod
    def _is_network(fstype: str) -> bool:
        """True for filesystems whose ``statvfs`` may block on a remote host."""
        return fstype.lower() in {
            "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs",
            "ceph", "glusterfs", "afs", "9p", "fuse.rclone",
        }

    def _partitions_of(
        self, node, mounts: dict[str, Partition]
    ) -> tuple[Partition, ...]:
        """Partitions belonging to one disk, enriched with mount information."""
        partitions: list[Partition] = []
        for child in sysfs.glob(f"{node.name}*", root=node):
            if not sysfs.exists(child / "partition"):
                continue
            sectors = sysfs.read_int(child / "size") or 0
            mounted = mounts.get(child.name)
            partitions.append(
                Partition(
                    name=child.name,
                    device=f"/dev/{child.name}",
                    size_bytes=int(sectors * DISKSTATS_SECTOR),
                    mountpoint=mounted.mountpoint if mounted else Unavailable(
                        Reason.NOT_EXPOSED, "not mounted"
                    ),
                    filesystem=mounted.filesystem if mounted else Unavailable(
                        Reason.NOT_EXPOSED, "not mounted"
                    ),
                    total_bytes=mounted.total_bytes if mounted else None,
                    used_bytes=mounted.used_bytes if mounted else None,
                    free_bytes=mounted.free_bytes if mounted else None,
                    options=mounted.options if mounted else (),
                    encrypted=mounted.encrypted if mounted else False,
                )
            )
        return tuple(sorted(partitions, key=lambda p: p.name))

    # ---------------------------------------------------------------- health

    def _health(
        self, name: str, now: float, nvme_temps: dict[str, float]
    ) -> SmartHealth:
        """Device health, refreshed on a long cadence and cached in between."""
        last = self._health_read_at.get(name, 0.0)
        cached = self._health_cache.get(name)
        if cached is None or now - last >= self._health_interval:
            cached = self._smart.read(f"/dev/{name}")
            self._health_cache[name] = cached
            self._health_read_at[name] = now

        # NVMe drives publish a composite temperature through hwmon, which needs
        # no privileges.  Prefer it when SMART itself is unavailable, so an
        # unprivileged user still sees drive temperature.
        if not is_number(cached.temperature):
            for key, value in nvme_temps.items():
                if name.startswith(key) or key.startswith(name):
                    from dataclasses import replace

                    return replace(cached, temperature=value)
        return cached

    def _read_nvme_temperatures(self) -> dict[str, float]:
        """NVMe composite temperatures via the targeted hwmon fast path."""
        readings = self.hwmon.chip("nvme")
        if not readings:
            return {}
        for reading in readings:
            if "composite" in reading.label.lower() or len(readings) == 1:
                # The hwmon chip is named "nvme"; drives are nvme0n1, nvme1n1...
                # The storage collector matches on prefix, so the bare name keys
                # correctly for the common single-drive case.
                return {"nvme": reading.current}
        return {"nvme": readings[0].current}
