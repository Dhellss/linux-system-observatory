"""Storage page: devices, filesystems, throughput and SMART health."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.core.units import bytes_, count, iops, percent, rate, temperature, text
from app.models.base import is_number
from app.models.storage import SmartVerdict, StorageSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter
from app.ui.widgets.stat import Badge, Divider, EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable


class StoragePage(Page):
    """Block devices, mounted filesystems and drive health."""

    domain = "storage"
    title = "Storage"
    icon = "▥"
    section = "Monitor"
    description = "Drives, filesystems, throughput and SMART health"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("storage")
        self._accent = accent
        self._build_summary(accent)
        self._build_filesystems(accent)
        self._build_devices(accent)
        self.add_stretch()
        self._device_sections: dict[str, _DeviceSection] = {}

    def _build_summary(self, accent: str) -> None:
        """Aggregate throughput and the activity chart."""
        card = Card("Activity", theme=self.theme, accent=accent)
        self._stats = StatRow(theme=self.theme, columns=4)
        self._stats.add("read", "Read throughput")
        self._stats.add("write", "Write throughput")
        self._stats.add("read_iops", "Read IOPS")
        self._stats.add("write_iops", "Write IOPS")
        self._stats.add("busy", "Busiest device")
        self._stats.add("queue", "Queue depth")
        self._stats.add("capacity", "Total capacity")
        self._stats.add("devices", "Devices")
        card.add(self._stats)

        window = self.context.config.settings.charts.history_seconds
        self._chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=None,
            y_label="B/s", height=165,
        )
        self._chart.add_series("storage.read_rate", self.palette_tokens.series[0])
        self._chart.add_series("storage.write_rate", self.palette_tokens.series[1])
        card.add(self._chart)
        self.add(card)

    def _build_filesystems(self, accent: str) -> None:
        """Mounted filesystem capacity."""
        card = Card(
            "Mounted filesystems", theme=self.theme,
            subtitle="Network mounts are skipped to avoid blocking on an "
                     "unresponsive server",
            accent=accent,
        )
        columns: list[Column] = [
            Column("mount", "Mount point",
                   lambda p: str(p.mountpoint), width=170, mono=True),
            Column("device", "Device", lambda p: p.device, width=0, mono=True),
            Column("fs", "Filesystem", lambda p: text(p.filesystem), width=100),
            Column("size", "Size", lambda p: p.total_bytes or 0,
                   display=bytes_, width=110, mono=True),
            Column("used", "Used", lambda p: p.used_bytes or 0,
                   display=bytes_, width=110, mono=True),
            Column("free", "Free", lambda p: p.free_bytes or 0,
                   display=bytes_, width=110, mono=True),
            Column(
                "percent", "Usage", lambda p: p.percent or 0,
                display=lambda v: percent(v), width=80, mono=True,
                colour=lambda p: self.palette_tokens.load_colour(p.percent or 0),
            ),
        ]
        self._fs_table = DataTable(columns, theme=self.theme, stretch_column=1)
        self._fs_table.setMinimumHeight(int(self.metrics.row_height * 5))
        self._fs_table.setMaximumHeight(int(self.metrics.row_height * 9))
        card.add(self._fs_table)

        self._fs_bars_host = QWidget()
        self._fs_bars_layout = QVBoxLayout(self._fs_bars_host)
        self._fs_bars_layout.setContentsMargins(0, 0, 0, 0)
        self._fs_bars_layout.setSpacing(self.metrics.space_1)
        self._fs_bars: dict[str, BarMeter] = {}
        card.add(Divider())
        card.add(self._fs_bars_host)
        self.add(card)

    def _build_devices(self, accent: str) -> None:
        """Host for per-device sections."""
        self.add_section("Physical devices")
        self._device_host = QWidget(self)
        self._device_layout = QVBoxLayout(self._device_host)
        self._device_layout.setContentsMargins(0, 0, 0, 0)
        self._device_layout.setSpacing(self.metrics.space_4)
        self.add(self._device_host)

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a storage snapshot."""
        if not isinstance(snapshot, StorageSnapshot):
            return
        history = self.context.history
        for metric in ("storage.read_rate", "storage.write_rate"):
            self._chart.update_series(metric, history.series(metric))
        if not self.is_visible_page:
            return

        io = snapshot.total_io
        self._stats["read"].set_value(rate(io.read_bytes_per_s))
        self._stats["write"].set_value(rate(io.write_bytes_per_s))
        self._stats["read_iops"].set_value(iops(io.read_iops))
        self._stats["write_iops"].set_value(iops(io.write_iops))
        busiest = max(
            snapshot.disks,
            key=lambda d: d.io.total_bytes_per_s,
            default=None,
        )
        self._stats["busy"].set_value(
            f"{busiest.name} ({percent(busiest.io.utilisation_percent)})"
            if busiest else "—"
        )
        self._stats.set("queue", io.queue_length, lambda v: f"{v:.2f}")
        self._stats["capacity"].set_value(bytes_(snapshot.total_capacity_bytes))
        self._stats["devices"].set_value(str(len(snapshot.disks)))

        self._fs_table.set_rows(snapshot.filesystems)
        self._update_filesystem_bars(snapshot)

        for disk in snapshot.disks:
            section = self._device_sections.get(disk.name)
            if section is None:
                section = _DeviceSection(self, disk, self._accent)
                self._device_sections[disk.name] = section
                self._device_layout.addWidget(section.card)
            section.update(disk)

    def _update_filesystem_bars(self, snapshot: StorageSnapshot) -> None:
        """Refresh the capacity bars beneath the filesystem table."""
        seen: set[str] = set()
        for filesystem in snapshot.filesystems:
            if not is_number(filesystem.percent):
                continue
            mount = str(filesystem.mountpoint)
            seen.add(mount)
            bar = self._fs_bars.get(mount)
            if bar is None:
                bar = BarMeter(theme=self.theme, label=mount)
                self._fs_bars[mount] = bar
                self._fs_bars_layout.addWidget(bar)
            bar.setVisible(True)
            bar.set_value(
                filesystem.percent,
                f"{bytes_(filesystem.used_bytes)} of "
                f"{bytes_(filesystem.total_bytes)} · "
                f"{bytes_(filesystem.free_bytes)} free",
            )
        for mount, bar in self._fs_bars.items():
            if mount not in seen:
                bar.setVisible(False)

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()


class _DeviceSection:
    """Card and widgets for one physical drive."""

    def __init__(self, page: StoragePage, disk, accent: str) -> None:
        theme = page.theme
        self._page = page
        self.card = Card(
            f"{disk.path}  ·  {text(disk.model)}", theme=theme,
            subtitle=f"{disk.kind.value} · {bytes_(disk.size_bytes)} · "
                     f"{text(disk.transport)}",
            accent=accent,
        )
        self._health_badge = Badge("Unknown", theme=theme,
                                   colour=theme.palette.text_muted)
        self.card.add_action(self._health_badge)

        self._stats = StatRow(theme=theme, columns=4)
        for key, label in (
            ("read", "Read"), ("write", "Write"),
            ("read_iops", "Read IOPS"), ("write_iops", "Write IOPS"),
            ("util", "Device busy"), ("queue", "Queue depth"),
            ("read_lat", "Read latency"), ("write_lat", "Write latency"),
        ):
            self._stats.add(key, label)
        self.card.add(self._stats)
        self.card.add(Divider())

        row = QHBoxLayout()
        row.setSpacing(theme.metrics.space_6)
        self._info = KeyValueTable(theme=theme, label_width=125)
        for key, label in (
            ("model", "Model"), ("serial", "Serial"),
            ("firmware", "Firmware"), ("transport", "Transport"),
            ("rotational", "Media"), ("scheduler", "I/O scheduler"),
            ("blocks", "Block size (log/phys)"), ("removable", "Removable"),
        ):
            self._info.add_row(key, label)
        row.addWidget(self._info, 1)

        self._health = KeyValueTable(theme=theme, label_width=145)
        for key, label in (
            ("verdict", "SMART verdict"), ("temperature", "Temperature"),
            ("hours", "Power-on hours"), ("cycles", "Power cycles"),
            ("wear", "Endurance used"), ("life", "Estimated life left"),
            ("spare", "Available spare"), ("written", "Data written"),
            ("read_total", "Data read"), ("reallocated", "Reallocated sectors"),
            ("pending", "Pending sectors"), ("media_errors", "Media errors"),
            ("unsafe", "Unsafe shutdowns"),
        ):
            self._health.add_row(key, label)
        row.addWidget(self._health, 1)
        self.card.add_layout(row)

        self._health_note = EmptyState(
            "SMART data unavailable", "", theme=theme, icon="⊘"
        )
        self._health_note.setVisible(False)
        self.card.add(self._health_note)

        self._wear_bar = BarMeter(theme=theme, label="Endurance consumed")
        self._wear_bar.setVisible(False)
        self.card.add(self._wear_bar)

        self._partitions = DataTable(
            [
                Column("name", "Partition", lambda p: p.device, width=150, mono=True),
                Column("size", "Size", lambda p: p.size_bytes,
                       display=bytes_, width=95, mono=True),
                Column("fs", "Filesystem", lambda p: text(p.filesystem), width=95),
                Column("mount", "Mounted at",
                       lambda p: text(p.mountpoint), width=0, mono=True),
                Column("used", "Used", lambda p: p.percent or 0,
                       display=lambda v: percent(v) if v else "—",
                       width=80, mono=True,
                       colour=lambda p: theme.palette.load_colour(p.percent or 0)),
            ],
            theme=theme, stretch_column=3,
        )
        self._partitions.setMaximumHeight(int(theme.metrics.row_height * 6))
        self.card.add(self._partitions)
        self._apply_static(disk)

    def _apply_static(self, disk) -> None:
        """Fill immutable device attributes."""
        media = "—"
        if disk.rotational is True:
            media = "Rotational"
        elif disk.rotational is False:
            media = "Solid state"
        self._info.update_many({
            "model": disk.model, "serial": disk.serial,
            "firmware": disk.firmware, "transport": disk.transport,
            "rotational": media, "scheduler": disk.scheduler,
            "blocks": (
                f"{text(disk.logical_block_size)} / "
                f"{text(disk.physical_block_size)} bytes"
            ),
            "removable": disk.removable,
        })

    def update(self, disk) -> None:
        """Refresh live I/O and health values."""
        theme = self._page.theme
        io = disk.io
        self._stats["read"].set_value(rate(io.read_bytes_per_s))
        self._stats["write"].set_value(rate(io.write_bytes_per_s))
        self._stats["read_iops"].set_value(iops(io.read_iops))
        self._stats["write_iops"].set_value(iops(io.write_iops))
        self._stats.set("util", io.utilisation_percent, percent)
        self._stats.set("queue", io.queue_length, lambda v: f"{v:.2f}")
        self._stats.set("read_lat", io.read_latency_ms,
                        lambda v: f"{v:.2f} ms")
        self._stats.set("write_lat", io.write_latency_ms,
                        lambda v: f"{v:.2f} ms")

        health = disk.health
        colours = {
            SmartVerdict.PASSED: theme.palette.success,
            SmartVerdict.WARNING: theme.palette.warning,
            SmartVerdict.FAILING: theme.palette.danger,
            SmartVerdict.UNKNOWN: theme.palette.text_muted,
        }
        self._health_badge.set_state(
            health.verdict.value, colours[health.verdict]
        )
        self._health.update_many({
            "verdict": health.verdict.value,
            "temperature": temperature(health.temperature),
            "hours": count(health.power_on_hours),
            "cycles": count(health.power_cycles),
            "wear": percent(health.wear_percent),
            "life": percent(health.estimated_life_left),
            "spare": percent(health.spare_percent),
            "written": bytes_(health.data_written_bytes),
            "read_total": bytes_(health.data_read_bytes),
            "reallocated": count(health.reallocated_sectors),
            "pending": count(health.pending_sectors),
            "media_errors": count(health.media_errors),
            "unsafe": count(health.unsafe_shutdowns),
        })
        if health.note:
            self._health_note.set_message("SMART data unavailable", health.note)
            self._health_note.setVisible(True)
        else:
            self._health_note.setVisible(False)

        if is_number(health.wear_percent):
            self._wear_bar.setVisible(True)
            self._wear_bar.set_value(
                health.wear_percent,
                f"{percent(health.estimated_life_left)} remaining",
            )
        else:
            self._wear_bar.setVisible(False)

        self._partitions.set_rows(disk.partitions)
        self._partitions.setVisible(bool(disk.partitions))
