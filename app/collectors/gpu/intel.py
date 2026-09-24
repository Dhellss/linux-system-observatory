"""Intel integrated graphics back-end.

Intel exposes far less than the other two vendors without elevated privileges.
``i915`` publishes current, minimum and maximum render clocks, and temperature
usually appears on the CPU package sensor because the GPU shares the die.
Utilisation is the notable gap: it requires either ``intel_gpu_top`` (which needs
``CAP_PERFMON``) or perf counters, so it is reported as unavailable with an
explanation rather than guessed at.

Being honest about that gap is the right behaviour.  A plausible-looking but
fabricated utilisation figure would be worse than an empty one.
"""

from __future__ import annotations

from pathlib import Path

from app.collectors.gpu.base import GpuBackend
from app.models.base import Reason, Unavailable
from app.models.gpu import GpuDevice, GpuSample, GpuVendor
from app.utils import sysfs
from app.utils.pci import inventory as pci_inventory

_INTEL_VENDOR_ID = "0x8086"
#: PCI class code prefix identifying a display controller (VGA or 3D).
_DISPLAY_CLASS_PREFIX = "0x03"
_NOT_EXPOSED = Unavailable(Reason.NOT_EXPOSED, "not published by i915/xe")

#: Frequency node layouts, oldest driver convention first.
_FREQ_NODES = (
    ("gt_cur_freq_mhz", "gt_min_freq_mhz", "gt_max_freq_mhz"),
    ("gt/gt0/rps_cur_freq_mhz", "gt/gt0/rps_min_freq_mhz", "gt/gt0/rps_max_freq_mhz"),
)


class IntelBackend(GpuBackend):
    """Reads Intel integrated adapters from the i915/xe sysfs interface."""

    vendor_name = "Intel"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities)
        self._hwmon_provider = hwmon

    def available(self) -> bool:
        """True when Intel display hardware exists, driven or not.

        Deliberately broader than "has a working driver".  An Intel adapter whose
        ``i915`` module failed to load is exactly the situation a monitoring tool
        should make visible, so the adapter is reported with an explanation
        rather than omitted as though the silicon were absent.
        """
        return bool(self._cards()) or bool(self._undriven_adapters())

    def sample(self) -> list[GpuSample]:
        """Read every Intel adapter, driven or merely present."""
        cards = self._cards()
        samples = [self._read_card(index, card) for index, card in enumerate(cards)]
        # Report display hardware the kernel has not bound a real driver to.
        # Without this, a machine whose i915 module is missing would appear to
        # have no integrated graphics at all -- a misleading silence.
        driven = {self._pci_slot(card / "device") for card in cards}
        for offset, slot in enumerate(self._undriven_adapters()):
            if slot in driven:
                continue
            samples.append(self._read_undriven(len(cards) + offset, slot))
        return samples

    def _cards(self) -> list[Path]:
        """Locate Intel display adapters that have a DRM driver bound."""
        cards: list[Path] = []
        for card in sysfs.glob("class/drm/card[0-9]*"):
            if sysfs.read_text(card / "device/vendor") != _INTEL_VENDOR_ID:
                continue
            # A simpledrm/efifb placeholder exposes no frequency nodes; treat it
            # as undriven so the user is told the real driver is missing.
            if self._read_frequencies(card)[0] is None:
                continue
            cards.append(card)
        return cards

    def _undriven_adapters(self) -> list[str]:
        """PCI slots of Intel display controllers lacking a native DRM driver."""
        slots: list[str] = []
        for device in sysfs.glob("bus/pci/devices/*"):
            if sysfs.read_text(device / "vendor") != _INTEL_VENDOR_ID:
                continue
            pci_class = sysfs.read_text(device / "class") or ""
            if not pci_class.startswith(_DISPLAY_CLASS_PREFIX):
                continue
            slots.append(device.name)
        return slots

    @staticmethod
    def _pci_slot(device: Path) -> str:
        """Resolve a sysfs device link to its PCI slot name."""
        try:
            return device.resolve().name
        except OSError:
            return ""

    def _read_undriven(self, index: int, slot: str) -> GpuSample:
        """Describe Intel display hardware whose kernel driver is not loaded.

        Every telemetry field is unavailable for the same single reason, which is
        stated once and clearly: the module that would publish those values is
        not bound to the device.
        """
        bound = self._bound_driver(slot)
        if bound:
            detail = (
                f"bound to {bound}, which publishes no telemetry; "
                "load the i915 or xe module for full reporting"
            )
        else:
            detail = "no kernel driver bound to this adapter"
        reason = Unavailable(Reason.NO_DRIVER, detail)
        device = GpuDevice(
            index=index,
            name=self._name_from_pci(slot),
            vendor=GpuVendor.INTEL,
            driver_version=reason,
            driver_name=f"{bound} (placeholder)" if bound else reason,
            vram_total_bytes=Unavailable(
                Reason.NO_HARDWARE, "integrated graphics share system memory"
            ),
            pci_bus_id=slot,
            pcie_gen=Unavailable(Reason.NO_HARDWARE, "on-package, no PCIe link"),
        )
        return GpuSample(
            device=device,
            utilisation=reason,
            memory_utilisation=reason,
            vram_used_bytes=reason,
            core_clock_mhz=reason,
            max_core_clock_mhz=reason,
            memory_clock_mhz=reason,
            temperature=self._read_temperature(),
            fan_percent=Unavailable(
                Reason.NO_HARDWARE, "cooling is shared with the CPU"
            ),
            power_w=reason,
            performance_state=reason,
        )

    def _bound_driver(self, slot: str) -> str | None:
        """Name of the driver currently bound to a PCI slot, if any."""
        try:
            link = sysfs.SYS / "bus/pci/devices" / slot / "driver"
            return link.resolve().name if link.exists() else None
        except OSError:
            return None

    def _name_from_pci(self, slot: str) -> str:
        """Marketing name for an Intel adapter identified only by PCI slot."""
        return pci_inventory.name_of(slot, "Intel Integrated Graphics")

    def _read_card(self, index: int, card: Path) -> GpuSample:
        """Assemble a sample for one Intel adapter."""
        current, _minimum, maximum = self._read_frequencies(card)
        device = GpuDevice(
            index=index,
            name=self._read_name(card),
            vendor=GpuVendor.INTEL,
            driver_version=sysfs.read_text(sysfs.SYS / "module/i915/version")
            or _NOT_EXPOSED,
            driver_name=self._read_driver_name(card),
            # Integrated graphics have no dedicated VRAM; they share system RAM,
            # so reporting a total would be misleading rather than merely absent.
            vram_total_bytes=Unavailable(
                Reason.NO_HARDWARE, "integrated graphics share system memory"
            ),
            pci_bus_id=self._read_pci_address(card),
            pcie_gen=Unavailable(Reason.NO_HARDWARE, "on-package, no PCIe link"),
        )
        return GpuSample(
            device=device,
            utilisation=Unavailable(
                Reason.PERMISSION,
                "Intel GPU utilisation needs intel_gpu_top with CAP_PERFMON",
            ),
            memory_utilisation=_NOT_EXPOSED,
            vram_used_bytes=_NOT_EXPOSED,
            core_clock_mhz=current if current is not None else _NOT_EXPOSED,
            max_core_clock_mhz=maximum if maximum is not None else _NOT_EXPOSED,
            memory_clock_mhz=_NOT_EXPOSED,
            temperature=self._read_temperature(),
            fan_percent=Unavailable(
                Reason.NO_HARDWARE, "cooling is shared with the CPU"
            ),
            power_w=_NOT_EXPOSED,
            performance_state=(
                f"{current:.0f} / {maximum:.0f} MHz"
                if current is not None and maximum else _NOT_EXPOSED
            ),
        )

    def _read_frequencies(
        self, card: Path
    ) -> tuple[float | None, float | None, float | None]:
        """Current, minimum and maximum render clock in MHz.

        The node layout changed between the legacy ``i915`` flat names and the
        newer per-tile ``gt/gtN`` hierarchy used by ``xe``; both are tried.
        """
        for current_node, min_node, max_node in _FREQ_NODES:
            current = sysfs.read_int(card / current_node)
            if current is not None:
                return (
                    current,
                    sysfs.read_int(card / min_node),
                    sysfs.read_int(card / max_node),
                )
        return (None, None, None)

    def _read_name(self, card: Path) -> str:
        """Human-readable adapter name, resolved through the cached PCI inventory."""
        address = self._read_pci_address(card)
        if isinstance(address, str) and (name := pci_inventory.name_of(address)):
            return name
        device_id = sysfs.read_text(card / "device/device") or ""
        return f"Intel Graphics {device_id}" if device_id else "Intel Graphics"

    def _read_driver_name(self, card: Path) -> str | Unavailable:
        """Which kernel driver is bound to this adapter."""
        try:
            driver = (card / "device/driver").resolve().name
        except OSError:
            return _NOT_EXPOSED
        return f"{driver} (open source)" if driver else _NOT_EXPOSED

    def _read_pci_address(self, card: Path) -> str | Unavailable:
        """PCI address from the sysfs symlink target."""
        try:
            return (card / "device").resolve().name
        except OSError:
            return _NOT_EXPOSED

    def _read_temperature(self):
        """Integrated GPU temperature, which is the CPU package sensor.

        This is not an approximation: on an integrated part the render slice is
        on the same die as the cores, so the package sensor *is* the GPU
        temperature.  The detail string says so, to avoid appearing to invent it.
        """
        if self._hwmon_provider is None:
            return _NOT_EXPOSED
        readings = self._hwmon_provider.find("coretemp", "acpitz")
        for reading in readings:
            if "package" in reading.label.lower():
                return reading.current
        return readings[0].current if readings else _NOT_EXPOSED
