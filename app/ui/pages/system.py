"""System information page: OS, firmware and the hardware inventory."""

from __future__ import annotations

from PySide6.QtWidgets import QLineEdit

from app.core.units import bytes_, count, duration, text, timestamp
from app.models.system import SystemSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import EmptyState, KeyValueTable
from app.ui.widgets.table import Column, DataTable


class SystemPage(Page):
    """A complete hardware and software inventory."""

    domain = "system"
    title = "System"
    icon = "▣"
    section = "System"
    description = "Operating system, firmware and hardware inventory"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("system")
        grid = self.add_grid(2)

        software = Card("Operating system", theme=self.theme, accent=accent)
        self._os = KeyValueTable(theme=self.theme, label_width=150)
        for key, label in (
            ("distribution", "Distribution"), ("version", "Version"),
            ("kernel", "Kernel"), ("kernel_build", "Kernel build"),
            ("architecture", "Architecture"), ("hostname", "Hostname"),
            ("uptime", "Uptime"), ("boot_time", "Last boot"),
            ("desktop", "Desktop environment"), ("wm", "Window manager"),
            ("session", "Session type"), ("shell", "Shell"),
            ("libc", "C library"), ("init", "Init system"),
            ("packages", "Installed packages"), ("python", "Python runtime"),
            ("users", "Logged-in users"), ("virtualisation", "Virtualisation"),
        ):
            self._os.add_row(key, label)
        software.add(self._os)
        grid.addWidget(software, 0, 0)

        firmware = Card("Firmware and chassis", theme=self.theme, accent=accent)
        self._firmware = KeyValueTable(theme=self.theme, label_width=150)
        for key, label in (
            ("system_vendor", "System vendor"), ("product", "Product"),
            ("product_version", "Product version"),
            ("board_vendor", "Motherboard vendor"), ("board", "Motherboard"),
            ("board_version", "Board revision"), ("board_serial", "Board serial"),
            ("bios_vendor", "Firmware vendor"), ("bios_version", "Firmware version"),
            ("bios_date", "Firmware date"), ("firmware_type", "Firmware type"),
            ("secure_boot", "Secure Boot"), ("chassis", "Chassis type"),
        ):
            self._firmware.add_row(key, label)
        firmware.add(self._firmware)
        grid.addWidget(firmware, 0, 1)

        memory = Card(
            "Memory modules", theme=self.theme,
            subtitle="Physical DIMM data comes from DMI table 17, which the "
                     "kernel exposes only to root",
            accent=accent,
        )
        self._modules = DataTable(
            [
                Column("slot", "Slot", lambda m: m.locator, width=130, mono=True),
                Column("size", "Size", lambda m: m.size_bytes or 0,
                       display=bytes_, width=90, mono=True),
                Column("type", "Type", lambda m: text(m.kind), width=90),
                Column("speed", "Speed", lambda m: m.speed_mts or 0,
                       display=lambda v: f"{v} MT/s" if v else "—",
                       width=100, mono=True),
                Column("configured", "Configured",
                       lambda m: m.configured_speed_mts or 0,
                       display=lambda v: f"{v} MT/s" if v else "—",
                       width=110, mono=True),
                Column("manufacturer", "Manufacturer",
                       lambda m: text(m.manufacturer), width=140),
                Column("part", "Part number", lambda m: text(m.part_number),
                       width=0, mono=True),
            ],
            theme=self.theme, stretch_column=6,
        )
        self._modules.setMaximumHeight(int(self.metrics.row_height * 7))
        memory.add(self._modules)
        self._modules_empty = EmptyState(
            "DIMM details require root",
            "There is no unprivileged path to the DMI memory tables. Run the "
            "application with sudo, or use `sudo dmidecode -t 17`, to see "
            "per-module details.",
            theme=self.theme, icon="⊘",
        )
        memory.add(self._modules_empty)
        self.add(memory)

        displays = Card("Displays", theme=self.theme, accent=accent)
        self._displays = DataTable(
            [
                Column("connector", "Connector", lambda d: d.connector,
                       width=140, mono=True),
                Column("status", "Status",
                       lambda d: "Connected" if d.connected else "Disconnected",
                       width=120,
                       colour=lambda d: (
                           self.palette_tokens.success if d.connected
                           else self.palette_tokens.text_subtle
                       )),
                Column("resolution", "Preferred mode",
                       lambda d: text(d.resolution), width=150, mono=True),
                Column("size", "Physical size",
                       lambda d: (
                           f"{d.physical_size_mm[0]} x {d.physical_size_mm[1]} mm"
                           if d.physical_size_mm else "—"
                       ), width=160),
                Column("diagonal", "Diagonal",
                       lambda d: d.diagonal_inches or 0,
                       display=lambda v: f'{v:.1f}"' if v else "—",
                       width=0, mono=True),
            ],
            theme=self.theme, stretch_column=4,
        )
        self._displays.setMaximumHeight(int(self.metrics.row_height * 6))
        displays.add(self._displays)
        self.add(displays)

        inventory = Card("Hardware inventory", theme=self.theme, accent=accent)
        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText(
            "Search devices by name, vendor, driver or bus…"
        )
        inventory.add(self._search)
        self._devices = DataTable(
            [
                Column("bus", "Bus", lambda d: d.bus, width=100),
                Column("category", "Category", lambda d: d.category, width=190),
                Column("name", "Device", lambda d: d.name, width=0),
                Column("vendor", "Vendor", lambda d: text(d.vendor), width=190),
                Column("driver", "Driver", lambda d: text(d.driver), width=130,
                       colour=lambda d: (
                           None if isinstance(d.driver, str)
                           else self.palette_tokens.text_subtle
                       )),
                Column("identifier", "Identifier", lambda d: d.identifier,
                       width=150, mono=True),
            ],
            theme=self.theme, stretch_column=2,
        )
        self._devices.setMinimumHeight(int(self.metrics.row_height * 14))
        self._search.textChanged.connect(self._devices.proxy.set_search)
        inventory.add(self._devices)
        self.add(inventory)
        self.add_stretch()

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a system inventory snapshot."""
        if not isinstance(snapshot, SystemSnapshot) or not self.is_visible_page:
            return
        os_info = snapshot.os
        self._os.update_many({
            "distribution": os_info.distribution,
            "version": os_info.version,
            "kernel": os_info.kernel,
            "kernel_build": os_info.kernel_build,
            "architecture": os_info.architecture,
            "hostname": os_info.hostname,
            "uptime": duration(os_info.uptime_seconds),
            "boot_time": timestamp(os_info.boot_time),
            "desktop": os_info.desktop_environment,
            "wm": os_info.window_manager,
            "session": os_info.session_type,
            "shell": os_info.shell,
            "libc": os_info.libc,
            "init": os_info.init_system,
            "packages": count(os_info.package_count),
            "python": os_info.python_version,
            "users": ", ".join(os_info.logged_in_users) or "—",
            "virtualisation": os_info.virtualised,
        })

        firmware = snapshot.firmware
        self._firmware.update_many({
            "system_vendor": firmware.system_vendor,
            "product": firmware.product_name,
            "product_version": firmware.product_version,
            "board_vendor": firmware.board_vendor,
            "board": firmware.board_name,
            "board_version": firmware.board_version,
            "board_serial": firmware.board_serial,
            "bios_vendor": firmware.bios_vendor,
            "bios_version": firmware.bios_version,
            "bios_date": firmware.bios_date,
            "firmware_type": firmware.firmware_type,
            "secure_boot": firmware.secure_boot,
            "chassis": firmware.chassis_type,
        })

        self._modules.set_rows(snapshot.memory_modules)
        has_modules = bool(snapshot.memory_modules)
        self._modules.setVisible(has_modules)
        self._modules_empty.setVisible(not has_modules)

        self._displays.set_rows(snapshot.displays)

        devices = [
            device
            for _heading, group in snapshot.device_groups
            for device in group
        ]
        self._devices.set_rows(devices)

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()
