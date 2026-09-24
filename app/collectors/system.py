"""System inventory collector: OS, firmware and hardware devices.

Almost everything here is static, so the collector runs on a five-minute cadence
and caches the genuinely immutable parts (firmware, memory modules, PCI devices)
for the life of the process.  Only uptime, logged-in users and USB devices are
re-read, because those are the three that actually change.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import shlex
import sys
import time

import psutil

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.system import (
    Device,
    DisplayInfo,
    FirmwareInfo,
    MemoryModule,
    OsInfo,
    SystemSnapshot,
)
from app.utils import sysfs
from app.utils.pci import inventory as pci_inventory
from app.utils.shell import has_tool, run

_DMI = sysfs.SYS / "class/dmi/id"

#: DMI chassis type codes, from the SMBIOS specification (table 3.4.1).
_CHASSIS_TYPES = {
    "1": "Other", "2": "Unknown", "3": "Desktop", "4": "Low-profile desktop",
    "5": "Pizza box", "6": "Mini tower", "7": "Tower", "8": "Portable",
    "9": "Laptop", "10": "Notebook", "11": "Handheld", "12": "Docking station",
    "13": "All-in-one", "14": "Subnotebook", "15": "Space-saving",
    "16": "Lunch box", "17": "Main server chassis", "18": "Expansion chassis",
    "23": "Rack mount chassis", "28": "Blade", "30": "Tablet",
    "31": "Convertible", "32": "Detachable",
}

#: Package managers probed to report the installed package count.
_PACKAGE_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("pacman", ["pacman", "-Qq"]),
    ("dpkg-query", ["dpkg-query", "-f", ".\n", "-W"]),
    ("rpm", ["rpm", "-qa"]),
    ("apk", ["apk", "info"]),
    ("xbps-query", ["xbps-query", "-l"]),
)


class SystemCollector(Collector[SystemSnapshot]):
    """Builds the hardware and software inventory."""

    domain = "system"
    title = "System information"
    mostly_static = True

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._firmware: FirmwareInfo | None = None
        self._modules: tuple[MemoryModule, ...] | None = None
        self._pci: tuple[Device, ...] | None = None
        self._package_count: int | Unavailable | None = None

    def _collect(self) -> SystemSnapshot:
        return SystemSnapshot(
            timestamp=time.monotonic(),
            os=self._read_os(),
            firmware=self._read_firmware(),
            memory_modules=self._read_memory_modules(),
            pci_devices=self._read_pci_devices(),
            usb_devices=self._read_usb_devices(),
            audio_devices=self._read_audio_devices(),
            bluetooth_devices=self._read_bluetooth_devices(),
            input_devices=self._read_input_devices(),
            displays=self._read_displays(),
        )

    def _empty(self) -> SystemSnapshot:
        return SystemSnapshot()

    # ----------------------------------------------------------------------- OS

    def _read_os(self) -> OsInfo:
        """Operating system, kernel and session identity."""
        release = self._read_os_release()
        boot_time = psutil.boot_time()
        kernel_version = sysfs.read_text(sysfs.PROC / "sys/kernel/osrelease")
        return OsInfo(
            distribution=release.get("PRETTY_NAME") or release.get("NAME")
            or Unavailable(Reason.NOT_EXPOSED, "/etc/os-release not found"),
            version=release.get("VERSION") or release.get("VERSION_ID"),
            codename=release.get("VERSION_CODENAME"),
            kernel=kernel_version or platform.release(),
            kernel_build=sysfs.read_text(sysfs.PROC / "sys/kernel/version"),
            architecture=platform.machine(),
            hostname=platform.node(),
            uptime_seconds=max(0.0, time.time() - boot_time),
            boot_time=boot_time,
            desktop_environment=self._env_or_unavailable(
                "XDG_CURRENT_DESKTOP", "no desktop environment advertised"
            ),
            window_manager=self._detect_window_manager(),
            session_type=self._env_or_unavailable(
                "XDG_SESSION_TYPE", "session type not set"
            ),
            shell=self._detect_shell(),
            libc=self._detect_libc(),
            init_system=self._detect_init(),
            package_count=self._count_packages(),
            python_version=f"{sys.version_info.major}.{sys.version_info.minor}"
            f".{sys.version_info.micro}",
            logged_in_users=self._read_users(),
            virtualised=self._detect_virtualisation(),
        )

    @staticmethod
    def _read_os_release() -> dict[str, str]:
        """Parse ``/etc/os-release``, whose values are shell-quoted."""
        data: dict[str, str] = {}
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            for line in sysfs.read_lines(path):
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                try:
                    unquoted = shlex.split(value)
                except ValueError:
                    unquoted = [value]
                data[key.strip()] = unquoted[0] if unquoted else value.strip('"')
            if data:
                break
        return data

    @staticmethod
    def _env_or_unavailable(variable: str, reason: str):
        """Read an environment variable or explain its absence."""
        value = os.environ.get(variable, "").strip()
        return value.replace(":", " / ") if value else Unavailable(
            Reason.NOT_EXPOSED, reason
        )

    @staticmethod
    def _detect_window_manager():
        """Identify the compositor or window manager from running processes.

        There is no standard way to ask "which WM is running", so the process
        table is searched for known compositor names.  This is inherently a
        heuristic, and the absence of a match is reported honestly.
        """
        known = {
            "kwin_wayland": "KWin (Wayland)", "kwin_x11": "KWin (X11)",
            "gnome-shell": "GNOME Shell", "mutter": "Mutter",
            "sway": "Sway", "hyprland": "Hyprland", "river": "River",
            "wayfire": "Wayfire", "weston": "Weston", "labwc": "labwc",
            "niri": "Niri", "xfwm4": "Xfwm", "openbox": "Openbox",
            "i3": "i3", "bspwm": "bspwm", "awesome": "awesome",
            "marco": "Marco", "muffin": "Muffin", "cosmic-comp": "COSMIC",
        }
        try:
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name in known:
                    return known[name]
        except (psutil.Error, OSError):
            pass
        return Unavailable(Reason.NOT_EXPOSED, "no known compositor process found")

    @staticmethod
    def _detect_shell():
        """The user's login shell."""
        shell = os.environ.get("SHELL", "")
        if not shell:
            return Unavailable(Reason.NOT_EXPOSED, "SHELL not set")
        name = shell.rsplit("/", 1)[-1]
        result = run([shell, "--version"], timeout=2.0)
        if result.ok and result.stdout and (
            match := re.search(r"(\d+\.\d+[\.\d]*)", result.stdout)
        ):
            return f"{name} {match.group(1)}"
        return name

    @staticmethod
    def _detect_libc():
        """The C library implementation and version."""
        try:
            name, version = platform.libc_ver()
        except (OSError, ValueError):
            name, version = "", ""
        if name:
            return f"{name} {version}".strip()
        if has_tool("ldd"):
            result = run(["ldd", "--version"], timeout=2.0)
            blob = result.stdout or result.stderr
            if "musl" in blob.lower():
                return "musl"
            if match := re.search(r"(\d+\.\d+)", blob):
                return f"glibc {match.group(1)}"
        return Unavailable(Reason.NOT_EXPOSED, "C library version not determinable")

    @staticmethod
    def _detect_init():
        """The init system, read from PID 1's own name."""
        try:
            return psutil.Process(1).name()
        except (psutil.Error, OSError):
            return Unavailable(Reason.PERMISSION, "PID 1 not readable")

    def _count_packages(self):
        """Installed package count, cached because it is slow and stable."""
        if self._package_count is not None:
            return self._package_count
        for tool, argv in _PACKAGE_COMMANDS:
            if not has_tool(tool):
                continue
            result = run(argv, timeout=20.0)
            if result.ok:
                self._package_count = len(
                    [ln for ln in result.stdout.splitlines() if ln.strip()]
                )
                return self._package_count
        self._package_count = Unavailable(
            Reason.NO_TOOL, "no recognised package manager found"
        )
        return self._package_count

    @staticmethod
    def _read_users() -> tuple[str, ...]:
        """Distinct logged-in usernames."""
        try:
            names = {u.name for u in psutil.users()}
        except (psutil.Error, OSError):
            try:
                names = {getpass.getuser()}
            except OSError:
                names = set()
        return tuple(sorted(names))

    @staticmethod
    def _detect_virtualisation():
        """Hypervisor identity, or bare metal."""
        result = run(["systemd-detect-virt"], timeout=2.0)
        value = result.stdout.strip()
        if value:
            return "None (bare metal)" if value == "none" else value
        return Unavailable(Reason.NO_TOOL, "systemd-detect-virt not available")

    # ----------------------------------------------------------------- firmware

    def _read_firmware(self) -> FirmwareInfo:
        """Motherboard and firmware identity, cached after the first read."""
        if self._firmware is not None:
            return self._firmware
        if not self.capabilities.dmi:
            self._firmware = FirmwareInfo()
            return self._firmware

        chassis_code = sysfs.read_text(_DMI / "chassis_type") or ""
        self._firmware = FirmwareInfo(
            board_vendor=self._dmi("board_vendor"),
            board_name=self._dmi("board_name"),
            board_version=self._dmi("board_version"),
            board_serial=self._dmi("board_serial", privileged=True),
            bios_vendor=self._dmi("bios_vendor"),
            bios_version=self._dmi("bios_version"),
            bios_date=self._dmi("bios_date"),
            firmware_type=(
                "UEFI" if sysfs.exists("/sys/firmware/efi") else "Legacy BIOS"
            ),
            secure_boot=self._read_secure_boot(),
            chassis_type=_CHASSIS_TYPES.get(chassis_code, chassis_code or None),
            system_vendor=self._dmi("sys_vendor"),
            product_name=self._dmi("product_name"),
            product_version=self._dmi("product_version"),
        )
        return self._firmware

    @staticmethod
    def _dmi(field: str, *, privileged: bool = False):
        """Read one DMI field, distinguishing absence from denial."""
        value = sysfs.read_text(_DMI / field)
        if value:
            return value
        if privileged:
            return Unavailable(
                Reason.PERMISSION, "serial numbers are readable only by root"
            )
        return Unavailable(Reason.NOT_EXPOSED, "not populated by firmware")

    @staticmethod
    def _read_secure_boot():
        """UEFI Secure Boot state, from the EFI variable.

        The variable's value is a five-byte blob: four bytes of attributes
        followed by the boolean, so the *last* byte is the one that matters.
        """
        for node in sysfs.glob("firmware/efi/efivars/SecureBoot-*"):
            try:
                raw = node.read_bytes()
            except OSError:
                continue
            if len(raw) >= 5:
                return raw[4] == 1
        if not sysfs.exists("/sys/firmware/efi"):
            return Unavailable(Reason.UNSUPPORTED, "not a UEFI system")
        return Unavailable(Reason.PERMISSION, "EFI variable not readable")

    # ------------------------------------------------------------ memory modules

    def _read_memory_modules(self) -> tuple[MemoryModule, ...]:
        """Physical DIMMs from ``dmidecode``, which requires root.

        There is no unprivileged path to DIMM data: the DMI type 17 tables are
        exposed only through ``/dev/mem`` or the root-only raw DMI file.  Rather
        than fabricate an entry from the total RAM size, an empty tuple is
        returned and the UI explains why.
        """
        if self._modules is not None:
            return self._modules
        if not has_tool("dmidecode") or not self.capabilities.root:
            self._modules = ()
            return self._modules

        result = run(["dmidecode", "-t", "17"], timeout=10.0)
        if not result.ok:
            self._modules = ()
            return self._modules

        modules: list[MemoryModule] = []
        block: dict[str, str] = {}
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Memory Device"):
                if block:
                    module = self._parse_module(block)
                    if module is not None:
                        modules.append(module)
                block = {}
            elif ":" in stripped:
                key, _, value = stripped.partition(":")
                block[key.strip()] = value.strip()
        if block:
            module = self._parse_module(block)
            if module is not None:
                modules.append(module)
        self._modules = tuple(modules)
        return self._modules

    @staticmethod
    def _parse_module(block: dict[str, str]) -> MemoryModule | None:
        """Turn one dmidecode Memory Device block into a model."""
        size_raw = block.get("Size", "")
        if not size_raw or "No Module" in size_raw:
            return None  # an empty slot
        size_bytes = None
        if match := re.match(r"(\d+)\s*(MB|GB|TB)", size_raw):
            multiplier = {"MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            size_bytes = int(match.group(1)) * multiplier[match.group(2)]

        def speed(key: str) -> int | None:
            if match := re.match(r"(\d+)", block.get(key, "")):
                return int(match.group(1))
            return None

        return MemoryModule(
            locator=block.get("Locator", "Unknown slot"),
            size_bytes=size_bytes,
            speed_mts=speed("Speed"),
            configured_speed_mts=speed("Configured Memory Speed"),
            manufacturer=block.get("Manufacturer"),
            part_number=block.get("Part Number"),
            kind=block.get("Type"),
            form_factor=block.get("Form Factor"),
            rank=int(block["Rank"]) if block.get("Rank", "").isdigit() else None,
        )

    # ------------------------------------------------------------------ devices

    def _read_pci_devices(self) -> tuple[Device, ...]:
        """PCI devices from the shared inventory, cached."""
        if self._pci is not None:
            return self._pci
        self._pci = tuple(
            Device(
                identifier=device.slot,
                name=device.name,
                category=device.device_class,
                vendor=device.vendor,
                driver=device.driver or Unavailable(
                    Reason.NO_DRIVER, "no kernel driver bound"
                ),
                detail=f"rev {device.revision}" if device.revision else None,
                bus="PCI",
            )
            for device in pci_inventory.devices()
        )
        return self._pci

    def _read_usb_devices(self) -> tuple[Device, ...]:
        """USB devices, re-read each cycle because they are hot-pluggable."""
        if not self.capabilities.lsusb:
            return ()
        result = run(["lsusb"], timeout=5.0)
        if not result.ok:
            return ()
        devices: list[Device] = []
        for line in result.lines:
            # Format: "Bus 001 Device 003: ID 1234:5678 Vendor Product"
            match = re.match(
                r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)",
                line,
            )
            if not match:
                continue
            bus, device_num, vendor_id, product_id, name = match.groups()
            # Root hubs are an implementation detail, not a device the user owns.
            if "root hub" in name.lower():
                continue
            devices.append(
                Device(
                    identifier=f"{vendor_id}:{product_id}",
                    name=name.strip() or f"Unnamed device {vendor_id}:{product_id}",
                    category="USB device",
                    detail=f"Bus {bus} Device {device_num}",
                    bus="USB",
                )
            )
        return tuple(devices)

    def _read_audio_devices(self) -> tuple[Device, ...]:
        """Sound cards from ``/proc/asound``."""
        devices: list[Device] = []
        for line in sysfs.read_lines(sysfs.PROC / "asound/cards"):
            # Format: " 0 [PCH  ]: HDA-Intel - HDA Intel PCH"
            if match := re.match(r"\s*(\d+)\s*\[([^\]]+)\]\s*:\s*(.+)", line):
                index, short, description = match.groups()
                devices.append(
                    Device(
                        identifier=f"card{index}",
                        name=description.strip(),
                        category="Sound card",
                        detail=short.strip(),
                        bus="ALSA",
                    )
                )
        if not devices:
            for card in sysfs.glob("class/sound/card*"):
                if name := sysfs.read_text(card / "id"):
                    devices.append(
                        Device(
                            identifier=card.name,
                            name=name,
                            category="Sound card",
                            bus="ALSA",
                        )
                    )
        return tuple(devices)

    def _read_bluetooth_devices(self) -> tuple[Device, ...]:
        """Bluetooth adapters and connected peripherals, read from sysfs only.

        ``bluetoothctl`` is deliberately **not** used.  It is an interactive REPL
        that, invoked non-interactively without a powered controller, blocks
        waiting for input until killed -- a four-second stall of a worker thread
        was measured during development.  sysfs answers the same question
        instantly: connected remotes appear as ``hciX:XX:XX:XX:XX:XX`` nodes
        beside their adapter.
        """
        devices: list[Device] = []
        for node in sysfs.glob("class/bluetooth/*"):
            address = sysfs.read_text(node / "address")
            if ":" in node.name:
                # A connected remote device; its name is published by the kernel.
                devices.append(
                    Device(
                        identifier=address or node.name,
                        name=sysfs.read_text(node / "name") or node.name,
                        category="Connected device",
                        detail=address,
                        bus="Bluetooth",
                    )
                )
                continue
            powered = sysfs.read_int(node / "rfkill0/soft")
            state = "blocked" if powered == 1 else "available"
            devices.append(
                Device(
                    identifier=node.name,
                    name=f"Bluetooth adapter {node.name}",
                    category="Adapter",
                    detail=f"{address} ({state})" if address else state,
                    bus="Bluetooth",
                )
            )
        return tuple(devices)

    def _read_input_devices(self) -> tuple[Device, ...]:
        """Input devices from ``/proc/bus/input/devices``."""
        devices: list[Device] = []
        name = phys = handlers = None
        for line in sysfs.read_lines(sysfs.PROC / "bus/input/devices"):
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"')
            elif line.startswith("P: Phys="):
                phys = line.split("=", 1)[1]
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].strip()
                if name:
                    devices.append(
                        Device(
                            identifier=phys or name,
                            name=name,
                            category=self._input_category(handlers),
                            detail=handlers,
                            bus="Input",
                        )
                    )
                name = phys = handlers = None
        return tuple(devices)

    @staticmethod
    def _input_category(handlers: str) -> str:
        """Classify an input device from its kernel handler list."""
        lowered = handlers.lower()
        if "mouse" in lowered:
            return "Pointing device"
        if "kbd" in lowered:
            return "Keyboard"
        if "js" in lowered:
            return "Game controller"
        return "Input device"

    def _read_displays(self) -> tuple[DisplayInfo, ...]:
        """Connected outputs from the DRM connector nodes.

        The EDID blob holds the manufacturer and model, but decoding it properly
        needs a parser well beyond this collector's remit.  The connector's
        reported modes give resolution and refresh rate, which is what the page
        actually shows.
        """
        displays: list[DisplayInfo] = []
        for connector in sysfs.glob("class/drm/card*-*"):
            status = sysfs.read_text(connector / "status")
            if status is None:
                continue
            connected = status == "connected"
            modes = sysfs.read_lines(connector / "modes")
            resolution = modes[0] if modes else None
            # Strip the card prefix: "card1-DP-1" reads better as "DP-1".
            label = (
                connector.name.split("-", 1)[1]
                if "-" in connector.name else connector.name
            )
            displays.append(
                DisplayInfo(
                    connector=label,
                    connected=connected,
                    resolution=resolution or Unavailable(
                        Reason.NOT_EXPOSED,
                        "no mode reported" if connected else "not connected",
                    ),
                    refresh_hz=self._read_refresh(),
                    physical_size_mm=self._read_physical_size(connector),
                )
            )
        # Connected outputs first, then alphabetically within each group.
        return tuple(sorted(displays, key=lambda d: (not d.connected, d.connector)))

    @staticmethod
    def _read_refresh():
        """Refresh rate, which the DRM sysfs interface does not publish.

        The active mode's timing lives in the compositor, not in sysfs; the
        ``modes`` file lists available resolutions without refresh rates.  This is
        reported as unavailable rather than guessed from the first mode, which
        would often be wrong on a variable-refresh display.
        """
        return Unavailable(
            Reason.NOT_EXPOSED, "refresh rate requires a compositor query"
        )

    @staticmethod
    def _read_physical_size(connector):
        """Panel dimensions in millimetres, if the connector reports them."""
        width = sysfs.read_int(connector / "edid_width_mm")
        height = sysfs.read_int(connector / "edid_height_mm")
        if width and height:
            return (int(width), int(height))
        return None
