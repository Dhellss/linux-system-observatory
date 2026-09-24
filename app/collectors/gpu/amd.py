"""AMD GPU back-end, driven by the ``amdgpu`` sysfs and hwmon interfaces.

Unlike NVIDIA, AMD needs no helper binary: the open-source kernel driver exposes
everything through ``/sys/class/drm/cardN/device``.  That makes this the
best-behaved of the three back-ends -- no subprocess, no parsing of
human-readable output, and no permission problems for the common metrics.

Unit conversions are the main hazard.  hwmon publishes microwatts, microvolts and
millidegrees; ``gpu_busy_percent`` is a plain percentage; ``mem_info_*`` are
bytes.  Each is converted at the boundary so the model layer sees consistent SI.
"""

from __future__ import annotations

from pathlib import Path

from app.collectors.gpu.base import GpuBackend
from app.models.base import Reason, Unavailable
from app.models.gpu import GpuDevice, GpuSample, GpuVendor
from app.utils import sysfs

#: Vendor ID published by every AMD/ATI display adapter.
_AMD_VENDOR_ID = "0x1002"

_NOT_EXPOSED = Unavailable(Reason.NOT_EXPOSED, "not published by amdgpu")


class AmdBackend(GpuBackend):
    """Reads AMD adapters straight from sysfs."""

    vendor_name = "AMD"

    def available(self) -> bool:
        """True when at least one amdgpu-driven card is present."""
        return bool(self._cards())

    def sample(self) -> list[GpuSample]:
        """Read every AMD adapter."""
        return [self._read_card(index, card)
                for index, card in enumerate(self._cards())]

    # ------------------------------------------------------------------ discovery

    def _cards(self) -> list[Path]:
        """Locate AMD display adapters by PCI vendor ID.

        Filtering on the vendor ID rather than the driver name also catches
        cards bound to ``radeon`` on older hardware.
        """
        cards: list[Path] = []
        for card in sysfs.glob("class/drm/card[0-9]*"):
            device = card / "device"
            if sysfs.read_text(device / "vendor") == _AMD_VENDOR_ID:
                cards.append(device)
        return cards

    def _hwmon(self, device: Path) -> Path | None:
        """The hwmon directory belonging to this adapter, if any."""
        candidates = sysfs.glob("hwmon*", root=device / "hwmon")
        return candidates[0] if candidates else None

    # -------------------------------------------------------------------- reading

    def _read_card(self, index: int, device: Path) -> GpuSample:
        """Assemble a sample for one adapter."""
        hwmon = self._hwmon(device)
        vram_total = sysfs.read_int(device / "mem_info_vram_total")
        vram_used = sysfs.read_int(device / "mem_info_vram_used")

        gpu_device = GpuDevice(
            index=index,
            name=self._read_name(device),
            vendor=GpuVendor.AMD,
            driver_version=self._read_driver_version(),
            driver_name="amdgpu (open source)",
            vram_total_bytes=int(vram_total) if vram_total else _NOT_EXPOSED,
            pci_bus_id=self._read_pci_address(device),
            max_power_w=self._micro(hwmon, "power1_cap"),
            pcie_gen=sysfs.read_text(device / "current_link_speed") or _NOT_EXPOSED,
            pcie_width=(
                f"x{sysfs.read_text(device / 'current_link_width')}"
                if sysfs.read_text(device / "current_link_width") else _NOT_EXPOSED
            ),
        )

        busy = sysfs.read_int(device / "gpu_busy_percent")
        mem_busy = sysfs.read_int(device / "mem_busy_percent")
        return GpuSample(
            device=gpu_device,
            utilisation=busy if busy is not None else _NOT_EXPOSED,
            memory_utilisation=mem_busy if mem_busy is not None else _NOT_EXPOSED,
            vram_used_bytes=int(vram_used) if vram_used is not None else _NOT_EXPOSED,
            vram_free_bytes=(
                int(vram_total - vram_used)
                if vram_total is not None and vram_used is not None else _NOT_EXPOSED
            ),
            core_clock_mhz=self._milli(hwmon, "freq1_input", divisor=1_000_000),
            memory_clock_mhz=self._milli(hwmon, "freq2_input", divisor=1_000_000),
            temperature=self._milli(hwmon, "temp1_input"),
            hotspot_temperature=self._milli(hwmon, "temp2_input"),
            memory_temperature=self._milli(hwmon, "temp3_input"),
            fan_rpm=self._plain(hwmon, "fan1_input"),
            fan_percent=self._fan_percent(hwmon),
            power_w=self._micro(hwmon, "power1_average") or self._micro(
                hwmon, "power1_input"
            ) or _NOT_EXPOSED,
            power_limit_w=self._micro(hwmon, "power1_cap"),
            voltage_v=self._milli(hwmon, "in0_input", divisor=1000),
            performance_state=sysfs.read_text(
                device / "power_dpm_force_performance_level")
            or _NOT_EXPOSED,
        )

    def _read_name(self, device: Path) -> str:
        """Best available marketing name for the adapter."""
        for node in ("product_name", "device"):
            if value := sysfs.read_text(device / node):
                if node == "product_name":
                    return value
                return f"AMD GPU {value}"
        return "AMD GPU"

    def _read_driver_version(self) -> str | Unavailable:
        """Amdgpu module version from the DRM subsystem."""
        value = sysfs.read_text(sysfs.SYS / "module/amdgpu/version")
        return value or _NOT_EXPOSED

    def _read_pci_address(self, device: Path) -> str | Unavailable:
        """PCI address, taken from the sysfs symlink target."""
        try:
            return device.resolve().name
        except OSError:
            return _NOT_EXPOSED

    # ------------------------------------------------------------ unit helpers

    @staticmethod
    def _plain(hwmon: Path | None, node: str):
        """Read an unscaled hwmon value."""
        if hwmon is None:
            return _NOT_EXPOSED
        value = sysfs.read_int(hwmon / node)
        return value if value is not None else _NOT_EXPOSED

    @staticmethod
    def _milli(hwmon: Path | None, node: str, divisor: float = 1000.0):
        """Read a milli-unit hwmon value (millidegrees, millivolts)."""
        if hwmon is None:
            return _NOT_EXPOSED
        value = sysfs.read_int(hwmon / node, scale=divisor)
        return value if value is not None else _NOT_EXPOSED

    @staticmethod
    def _micro(hwmon: Path | None, node: str):
        """Read a micro-unit hwmon value (microwatts) as its base unit."""
        if hwmon is None:
            return None
        return sysfs.read_int(hwmon / node, scale=1_000_000.0)

    def _fan_percent(self, hwmon: Path | None):
        """Derive fan duty cycle as a percentage of its PWM range."""
        if hwmon is None:
            return _NOT_EXPOSED
        pwm = sysfs.read_int(hwmon / "pwm1")
        if pwm is None:
            return _NOT_EXPOSED
        # PWM is an 8-bit duty cycle; convert to the percentage users expect.
        return min(100.0, 100.0 * pwm / 255.0)
