r"""PCI device enumeration and name resolution.

``lspci -mm`` emits a machine-readable line per device, but its fields are
shell-quoted and several of them contain the spaces, commas and brackets that
naive splitting mangles.  Real output looks like::

    00:02.0 "VGA compatible controller" "Intel Corporation" \\
        "TigerLake-H GT1 [UHD Graphics]" -r01 "Device" "5015"

Splitting on ``'" "'`` produces garbage because the revision token sits
*outside* the quotes.  :func:`shlex.split` understands the quoting rules exactly,
so it is used instead -- this module exists to make sure that lesson is applied
in one place rather than rediscovered in each collector.

Results are cached: PCI topology does not change without a reboot (or a
Thunderbolt event, which is rare enough not to justify re-running a subprocess on
every poll).
"""

from __future__ import annotations

import logging
import shlex
import threading
from dataclasses import dataclass

from app.utils.shell import run

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PciDevice:
    """One device from the PCI bus."""

    slot: str
    device_class: str
    vendor: str
    name: str
    revision: str = ""
    driver: str = ""

    @property
    def description(self) -> str:
        """Vendor and model as a single display string."""
        return f"{self.vendor} {self.name}".strip()


class PciInventory:
    """Cached, parsed view of the PCI bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: list[PciDevice] | None = None

    def devices(self) -> list[PciDevice]:
        """Every PCI device, enumerated once per process."""
        with self._lock:
            if self._devices is None:
                self._devices = self._enumerate()
            return list(self._devices)

    def _enumerate(self) -> list[PciDevice]:
        """Run ``lspci -mm -D`` and attach each device's bound kernel driver.

        Note that ``-mm`` silently ignores ``-k``: the machine-readable format
        emits no driver lines at all.  Rather than parsing the human-readable
        output -- which is localised and reflows -- the driver is read from the
        sysfs ``driver`` symlink.  That is faster, needs no subprocess, and is
        the kernel's own answer rather than a formatted rendering of it.
        """
        # -D prints full domain-qualified slots, matching sysfs device names so
        # callers can correlate the two without string surgery.
        result = run(["lspci", "-mm", "-D"], timeout=5.0)
        if not result.ok:
            return []
        devices: list[PciDevice] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parsed = self._parse_line(stripped)
            if parsed is not None:
                devices.append(parsed)
        return devices

    @staticmethod
    def _driver_of(slot: str) -> str:
        """Kernel driver bound to ``slot``, read from sysfs."""
        from app.utils import sysfs

        link = sysfs.SYS / "bus/pci/devices" / slot / "driver"
        try:
            return link.resolve().name if link.exists() else ""
        except OSError:
            return ""

    @staticmethod
    def _parse_line(line: str) -> PciDevice | None:
        """Parse one ``lspci -mm`` record."""
        try:
            fields = shlex.split(line)
        except ValueError:
            _log.debug("Unparseable lspci line: %r", line)
            return None
        if len(fields) < 4:
            return None
        revision = next(
            (f[2:] for f in fields[4:] if f.startswith("-r")), ""
        )
        slot = fields[0]
        return PciDevice(
            slot=slot,
            device_class=fields[1],
            vendor=fields[2],
            name=fields[3],
            revision=revision,
            driver=PciInventory._driver_of(slot),
        )

    def find(self, slot: str) -> PciDevice | None:
        """Look up one device by PCI slot.

        Accepts slots with or without the domain prefix, because sysfs uses
        ``0000:00:02.0`` while ``lspci`` without ``-D`` prints ``00:02.0``.
        """
        normalised = slot if slot.count(":") == 2 else f"0000:{slot}"
        short = normalised.split(":", 1)[1]
        for device in self.devices():
            if device.slot in (normalised, short):
                return device
        return None

    def name_of(self, slot: str, fallback: str = "") -> str:
        """Display name for one slot, or ``fallback`` when unknown."""
        device = self.find(slot)
        return device.description if device else fallback

    def of_class(self, prefix: str) -> list[PciDevice]:
        """Devices whose class description starts with ``prefix``."""
        lowered = prefix.lower()
        return [d for d in self.devices() if d.device_class.lower().startswith(lowered)]


#: Process-wide inventory, shared by the GPU back-ends and the system collector.
inventory = PciInventory()
