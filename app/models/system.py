"""Static system inventory models: OS, firmware and hardware devices."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


@dataclass(frozen=True, slots=True)
class OsInfo:
    """Operating system and session identity."""

    distribution: Maybe[str] = None
    version: Maybe[str] = None
    codename: Maybe[str] = None
    kernel: Maybe[str] = None
    kernel_build: Maybe[str] = None
    architecture: Maybe[str] = None
    hostname: Maybe[str] = None
    uptime_seconds: Maybe[float] = None
    boot_time: Maybe[float] = None
    desktop_environment: Maybe[str] = None
    window_manager: Maybe[str] = None
    session_type: Maybe[str] = None
    shell: Maybe[str] = None
    libc: Maybe[str] = None
    init_system: Maybe[str] = None
    package_count: Maybe[int] = None
    python_version: Maybe[str] = None
    logged_in_users: tuple[str, ...] = ()
    #: True when running inside a container or virtual machine.
    virtualised: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class FirmwareInfo:
    """Motherboard and firmware identity from the DMI/SMBIOS tables."""

    board_vendor: Maybe[str] = None
    board_name: Maybe[str] = None
    board_version: Maybe[str] = None
    board_serial: Maybe[str] = None
    bios_vendor: Maybe[str] = None
    bios_version: Maybe[str] = None
    bios_date: Maybe[str] = None
    #: "UEFI" or "Legacy BIOS", detected from the presence of /sys/firmware/efi.
    firmware_type: Maybe[str] = None
    secure_boot: Maybe[bool] = None
    chassis_type: Maybe[str] = None
    system_vendor: Maybe[str] = None
    product_name: Maybe[str] = None
    product_version: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class MemoryModule:
    """A physical DIMM, as reported by DMI type 17."""

    locator: str
    size_bytes: Maybe[int] = None
    speed_mts: Maybe[int] = None
    configured_speed_mts: Maybe[int] = None
    manufacturer: Maybe[str] = None
    part_number: Maybe[str] = None
    kind: Maybe[str] = None
    form_factor: Maybe[str] = None
    rank: Maybe[int] = None
    voltage_v: Maybe[float] = None


@dataclass(frozen=True, slots=True)
class Device:
    """A hardware device from any enumeration source (PCI, USB, Bluetooth...)."""

    identifier: str
    name: str
    category: str
    vendor: Maybe[str] = None
    driver: Maybe[str] = None
    detail: Maybe[str] = None
    #: Source bus, so the UI can group by origin.
    bus: str = ""


@dataclass(frozen=True, slots=True)
class DisplayInfo:
    """A connected display output."""

    connector: str
    connected: bool
    resolution: Maybe[str] = None
    refresh_hz: Maybe[float] = None
    physical_size_mm: Maybe[tuple[int, int]] = None
    manufacturer: Maybe[str] = None
    model: Maybe[str] = None
    scale: Maybe[float] = None
    primary: bool = False

    @property
    def diagonal_inches(self) -> Maybe[float]:
        """Screen diagonal derived from the physical dimensions."""
        size = self.physical_size_mm
        if not isinstance(size, tuple):
            return None
        width, height = size
        if not width or not height:
            return None
        return float(((width**2 + height**2) ** 0.5) / 25.4)


@dataclass(frozen=True, slots=True)
class SystemSnapshot(Snapshot):
    """The complete hardware and software inventory.

    Sampled rarely (every few minutes) because almost nothing here changes: the
    exceptions are uptime, logged-in users and hot-plugged USB devices.
    """

    os: OsInfo = field(default_factory=OsInfo)
    firmware: FirmwareInfo = field(default_factory=FirmwareInfo)
    memory_modules: tuple[MemoryModule, ...] = ()
    pci_devices: tuple[Device, ...] = ()
    usb_devices: tuple[Device, ...] = ()
    audio_devices: tuple[Device, ...] = ()
    bluetooth_devices: tuple[Device, ...] = ()
    input_devices: tuple[Device, ...] = ()
    displays: tuple[DisplayInfo, ...] = ()

    @property
    def device_groups(self) -> list[tuple[str, tuple[Device, ...]]]:
        """Device collections in display order, for the inventory view."""
        return [
            ("PCI devices", self.pci_devices),
            ("USB devices", self.usb_devices),
            ("Audio devices", self.audio_devices),
            ("Bluetooth", self.bluetooth_devices),
            ("Input devices", self.input_devices),
        ]
