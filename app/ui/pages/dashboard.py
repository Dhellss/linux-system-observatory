"""Customisable dashboard.

Widget model
------------
The dashboard is a grid of independent widgets, each declaring a fixed column
span.  Rather than implementing free-form drag positioning -- which is fiddly to
get right and awkward to persist -- widgets are *ordered*, and the grid reflows
them into the available columns.  Reordering and resizing are therefore simple,
reliable operations on a list, and the layout adapts to window width for free.

The chosen set, layout and column count persist in the settings document.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.units import (
    bytes_,
    duration,
    percent,
    rate,
    temperature,
    text,
    timestamp,
)
from app.models.base import is_number
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import Sparkline
from app.ui.widgets.gauge import BarMeter, RingGauge
from app.ui.widgets.stat import Divider, EmptyState, KeyValueTable, StatRow


@dataclass(frozen=True)
class WidgetSpec:
    """Declares one available dashboard widget."""

    key: str
    title: str
    #: Domains whose snapshots this widget consumes.
    domains: tuple[str, ...]
    #: Columns the widget occupies in the grid.
    span: int
    #: Builds the widget body; returns an update callable.
    build: Callable[[DashboardPage, Card], Callable[[str, object], None]]
    description: str = ""


class DashboardPage(Page):
    """An at-a-glance overview assembled from user-chosen widgets."""

    domain = ""
    extra_domains = (
        "cpu", "memory", "gpu", "storage", "network",
        "processes", "sensors", "battery", "system", "services",
    )
    title = "Dashboard"
    icon = "◉"
    section = "Overview"
    description = "Customisable system overview"

    #: Widgets enabled on first run, chosen to fill two rows without scrolling.
    DEFAULT_LAYOUT = (
        "cpu", "memory", "gpu", "storage",
        "network", "thermals", "battery", "uptime",
        "processes", "load", "clock", "notes",
    )

    def build_ui(self) -> None:
        """Assemble the dashboard from the persisted layout."""
        self._specs = {spec.key: spec for spec in _WIDGET_SPECS}
        self._updaters: dict[str, Callable[[str, Any], None]] = {}
        self._cards: dict[str, Card] = {}
        self._latest: dict[str, object] = {}

        header = QHBoxLayout()
        heading = QLabel("System overview", self)
        heading.setProperty("role", "heading")
        header.addWidget(heading)
        header.addStretch(1)

        self._columns_box = QComboBox(self)
        self._columns_box.addItems(["2 columns", "3 columns", "4 columns"])
        stored_columns = int(
            self.context.config.get("ui_state.dashboard_columns", 4)
        )
        self._columns_box.setCurrentIndex(max(0, min(2, stored_columns - 2)))
        self._columns_box.currentIndexChanged.connect(self._on_columns_changed)
        header.addWidget(self._columns_box)

        customise = QPushButton("Customise…", self)
        customise.clicked.connect(self._open_customise)
        header.addWidget(customise)
        self.add_layout(header)

        self._grid_host = QWidget(self)
        self._grid_layout = QVBoxLayout(self._grid_host)
        self._grid_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_layout.setSpacing(self.metrics.space_4)
        self.add(self._grid_host)
        self.add_stretch()

        self._rebuild()

    # ---------------------------------------------------------------- layout

    @property
    def _columns(self) -> int:
        """Configured grid column count."""
        return int(self._columns_box.currentIndex()) + 2

    def _layout_keys(self) -> list[str]:
        """The ordered list of enabled widget keys."""
        stored = self.context.config.get("ui_state.dashboard_widgets")
        if isinstance(stored, list) and stored:
            return [key for key in stored if key in self._specs]
        return [key for key in self.DEFAULT_LAYOUT if key in self._specs]

    def _save_layout(self, keys: list[str]) -> None:
        """Persist the widget order."""
        self.context.config.set("ui_state.dashboard_widgets", keys)

    def _on_columns_changed(self) -> None:
        """Persist and apply a new column count."""
        self.context.config.set("ui_state.dashboard_columns", self._columns)
        self._rebuild()

    def _rebuild(self) -> None:
        """Tear down and re-create the widget grid.

        Rebuilding wholesale is acceptable because it happens only on an explicit
        layout change, never on a data update.
        """
        while self._grid_layout.count():
            # takeAt returns None once the layout is empty; count() guards that
            # here, but the null check keeps the teardown safe if the layout is
            # mutated concurrently.
            item = self._grid_layout.takeAt(0)
            if item is None:
                break
            if (widget := item.widget()) is not None:
                widget.deleteLater()
        self._updaters.clear()
        self._cards.clear()

        from PySide6.QtWidgets import QGridLayout

        columns = self._columns
        grid_host = QWidget(self._grid_host)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(self.metrics.space_4)
        for column in range(columns):
            grid.setColumnStretch(column, 1)

        row = position = 0
        for key in self._layout_keys():
            spec = self._specs[key]
            span = min(spec.span, columns)
            # Wrap to the next row when the widget will not fit in what remains.
            if position + span > columns:
                row += 1
                position = 0
            card = Card(
                spec.title, theme=self.theme,
                accent=self.context.colour(
                    spec.domains[0] if spec.domains else "dashboard"
                ),
            )
            self._updaters[key] = spec.build(self, card)
            self._cards[key] = card
            grid.addWidget(card, row, position, 1, span)
            position += span
            if position >= columns:
                row += 1
                position = 0
        self._grid_layout.addWidget(grid_host)

        # Re-apply the most recent snapshots so a rebuilt widget is populated
        # immediately rather than staying blank until the next poll.
        for domain, snapshot in self._latest.items():
            self._dispatch(domain, snapshot)

    def _open_customise(self) -> None:
        """Show the widget chooser."""
        dialog = _CustomiseDialog(
            self._specs, self._layout_keys(), theme=self.theme, parent=self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_layout(dialog.selected_keys())
            self._rebuild()

    # ---------------------------------------------------------------- updates

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Fan a snapshot out to every widget that consumes that domain."""
        self._latest[domain] = snapshot
        if self.is_visible_page:
            self._dispatch(domain, snapshot)

    def _dispatch(self, domain: str, snapshot: Any) -> None:
        """Invoke the updater of each widget interested in ``domain``."""
        for key, updater in self._updaters.items():
            if domain in self._specs[key].domains:
                updater(domain, snapshot)

    def on_shown(self) -> None:
        """Repaint from cached snapshots on entry."""
        super().on_shown()
        for domain, snapshot in self._latest.items():
            self._dispatch(domain, snapshot)


# --------------------------------------------------------------------- widgets
# Each builder returns an updater closure.  Keeping the widget's state inside the
# closure avoids a class per widget while still isolating them from each other.


def _build_cpu(page: DashboardPage, card: Card):
    """Ring gauge, sparkline and headline CPU figures."""
    ring = RingGauge(theme=page.theme, size=98, label="usage")
    stats = StatRow(theme=page.theme, columns=1)
    stats.add("clock", "Clock", size="small")
    stats.add("temp", "Temperature", size="small")
    row = QHBoxLayout()
    row.addWidget(ring)
    row.addWidget(stats, 1)
    card.add_layout(row)
    spark = Sparkline(theme=page.theme, colour=page.context.colour("cpu"))
    card.add(spark)

    def update(domain: str, snapshot: Any) -> None:
        ring.set_value(snapshot.usage)
        stats.set("clock", snapshot.frequency_mhz, _frequency)
        stats.set("temp", snapshot.temperature, temperature)
        spark.set_values(page.context.history.series("cpu.usage").values())

    return update


def _build_memory(page: DashboardPage, card: Card):
    """Ring gauge plus RAM and swap bars."""
    ring = RingGauge(theme=page.theme, size=98, label="RAM used")
    stats = StatRow(theme=page.theme, columns=1)
    stats.add("used", "Used", size="small")
    stats.add("avail", "Available", size="small")
    row = QHBoxLayout()
    row.addWidget(ring)
    row.addWidget(stats, 1)
    card.add_layout(row)
    swap = BarMeter(theme=page.theme, label="Swap")
    card.add(swap)

    def update(domain: str, snapshot: Any) -> None:
        ring.set_value(snapshot.percent)
        stats["used"].set_value(bytes_(snapshot.used_bytes))
        stats["avail"].set_value(bytes_(snapshot.available_bytes))
        if snapshot.swap_total_bytes:
            swap.set_value(
                snapshot.swap_percent,
                f"{bytes_(snapshot.swap_used_bytes)} / "
                f"{bytes_(snapshot.swap_total_bytes)}",
            )
        else:
            swap.set_value(0, "Not configured")

    return update


def _build_gpu(page: DashboardPage, card: Card):
    """Primary adapter utilisation and VRAM."""
    ring = RingGauge(theme=page.theme, size=98, label="GPU")
    stats = StatRow(theme=page.theme, columns=1)
    stats.add("name", "Adapter", size="small")
    stats.add("temp", "Temperature", size="small")
    row = QHBoxLayout()
    row.addWidget(ring)
    row.addWidget(stats, 1)
    card.add_layout(row)
    vram = BarMeter(theme=page.theme, label="VRAM")
    card.add(vram)
    empty = EmptyState("No graphics adapter", "", theme=page.theme, icon="▢")
    empty.setVisible(False)
    card.add(empty)

    def update(domain: str, snapshot: Any) -> None:
        primary = snapshot.primary
        empty.setVisible(primary is None)
        if primary is None:
            ring.set_value(None)
            return
        ring.set_value(primary.utilisation)
        name = primary.device.name
        stats["name"].set_value(name[:22] + ("…" if len(name) > 22 else ""))
        stats.set("temp", primary.temperature, temperature)
        used = primary.vram_used_bytes
        total = primary.device.vram_total_bytes
        if isinstance(used, int) and isinstance(total, int) and total:
            vram.set_value(100.0 * used / total,
                           f"{bytes_(used)} / {bytes_(total)}")
        else:
            vram.set_value(primary.vram_percent)

    return update


def _build_storage(page: DashboardPage, card: Card):
    """Filesystem capacity bars and current throughput."""
    stats = StatRow(theme=page.theme, columns=2)
    stats.add("read", "Read", size="small")
    stats.add("write", "Write", size="small")
    card.add(stats)
    card.add(Divider())
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(page.metrics.space_1)
    card.add(host)
    bars: dict[str, BarMeter] = {}

    def update(domain: str, snapshot: Any) -> None:
        stats["read"].set_value(rate(snapshot.total_io.read_bytes_per_s))
        stats["write"].set_value(rate(snapshot.total_io.write_bytes_per_s))
        for filesystem in snapshot.filesystems[:4]:
            mount = str(filesystem.mountpoint)
            bar = bars.get(mount)
            if bar is None:
                bar = BarMeter(theme=page.theme, label=mount)
                bars[mount] = bar
                layout.addWidget(bar)
            bar.set_value(
                filesystem.percent,
                f"{bytes_(filesystem.free_bytes)} free",
            )

    return update


def _build_network(page: DashboardPage, card: Card):
    """Throughput figures with up/down sparklines."""
    stats = StatRow(theme=page.theme, columns=2)
    stats.add("down", "Download", size="small")
    stats.add("up", "Upload", size="small")
    card.add(stats)
    down = Sparkline(theme=page.theme, colour=page.palette_tokens.series[2],
                     y_range=None, height=28)
    up = Sparkline(theme=page.theme, colour=page.palette_tokens.series[1],
                   y_range=None, height=28)
    card.add(down)
    card.add(up)
    info = KeyValueTable(theme=page.theme, label_width=70)
    info.add_row("iface", "Interface")
    info.add_row("address", "Address")
    card.add(info)

    def update(domain: str, snapshot: Any) -> None:
        stats["down"].set_value(rate(snapshot.totals.download_bytes_per_s))
        stats["up"].set_value(rate(snapshot.totals.upload_bytes_per_s))
        down.set_values(page.context.history.series("network.download").values())
        up.set_values(page.context.history.series("network.upload").values())
        default = snapshot.default_interface
        info.set("iface", default.name if default else "—")
        info.set("address", (default.ipv4 or "—") if default else "—")

    return update


def _build_thermals(page: DashboardPage, card: Card):
    """The hottest sensors on the system."""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(page.metrics.space_1)
    card.add(host)
    bars: list[BarMeter] = []
    empty = EmptyState("No sensors detected", "", theme=page.theme, icon="◈")
    empty.setVisible(False)
    card.add(empty)

    def update(domain: str, snapshot: Any) -> None:
        hottest = snapshot.temperatures()[:5]
        empty.setVisible(not hottest)
        while len(bars) < len(hottest):
            bar = BarMeter(theme=page.theme)
            bars.append(bar)
            layout.addWidget(bar)
        for index, sensor in enumerate(hottest):
            bar = bars[index]
            bar.setVisible(True)
            bar.set_label(f"{sensor.chip} · {sensor.label}"[:34])
            limit = sensor.critical if is_number(sensor.critical) else 100.0
            value = sensor.value if is_number(sensor.value) else 0.0
            bar.set_value(min(100.0, 100.0 * value / limit), temperature(value))
        for extra in bars[len(hottest):]:
            extra.setVisible(False)

    return update


def _build_battery(page: DashboardPage, card: Card):
    """Charge, health and power flow."""
    ring = RingGauge(theme=page.theme, size=98, label="charge", sense="reserve")
    stats = StatRow(theme=page.theme, columns=1)
    stats.add("status", "Status", size="small")
    stats.add("health", "Health", size="small")
    row = QHBoxLayout()
    row.addWidget(ring)
    row.addWidget(stats, 1)
    card.add_layout(row)
    detail = KeyValueTable(theme=page.theme, label_width=80)
    detail.add_row("rate", "Power")
    detail.add_row("remaining", "Remaining")
    card.add(detail)
    empty = EmptyState("No battery present", "", theme=page.theme, icon="▭")
    empty.setVisible(False)
    card.add(empty)

    def update(domain: str, snapshot: Any) -> None:
        empty.setVisible(not snapshot.present)
        if not snapshot.present:
            ring.set_value(None)
            return
        ring.set_value(snapshot.percent)
        stats["status"].set_value(text(snapshot.status))
        stats.set("health", snapshot.health_percent, percent)
        detail.set("rate", f"{_watts(snapshot.power_now_w)} ({snapshot.rate_label})")
        detail.set("remaining", duration(snapshot.seconds_remaining))

    return update


def _build_uptime(page: DashboardPage, card: Card):
    """Host identity and uptime."""
    table = KeyValueTable(theme=page.theme, label_width=86)
    for key, label in (
        ("host", "Hostname"), ("distro", "Distribution"),
        ("kernel", "Kernel"), ("uptime", "Uptime"), ("boot", "Booted"),
    ):
        table.add_row(key, label)
    card.add(table)

    def update(domain: str, snapshot: Any) -> None:
        table.update_many({
            "host": snapshot.os.hostname,
            "distro": snapshot.os.distribution,
            "kernel": snapshot.os.kernel,
            "uptime": duration(snapshot.os.uptime_seconds),
            "boot": timestamp(snapshot.os.boot_time),
        })

    return update


def _build_processes(page: DashboardPage, card: Card):
    """Process counts and the top consumer."""
    stats = StatRow(theme=page.theme, columns=2)
    stats.add("total", "Processes", size="small")
    stats.add("threads", "Threads", size="small")
    stats.add("running", "Runnable", size="small")
    stats.add("zombie", "Zombies", size="small")
    card.add(stats)
    card.add(Divider())
    top = KeyValueTable(theme=page.theme, label_width=70)
    top.add_row("cpu", "Top CPU")
    top.add_row("memory", "Top memory")
    card.add(top)

    def update(domain: str, snapshot: Any) -> None:
        stats["total"].set_value(str(snapshot.total))
        stats["threads"].set_value(str(snapshot.threads))
        stats["running"].set_value(str(snapshot.running))
        stats["zombie"].set_value(
            str(snapshot.zombie),
            colour=page.palette_tokens.warning if snapshot.zombie else None,
        )
        cpu_top = snapshot.top_cpu(1)
        mem_top = snapshot.top_memory(1)
        top.set("cpu", f"{cpu_top[0].name} ({percent(cpu_top[0].cpu_percent)})"
                if cpu_top else "—")
        top.set("memory", f"{mem_top[0].name} ({bytes_(mem_top[0].memory_rss_bytes)})"
                if mem_top else "—")

    return update


def _build_load(page: DashboardPage, card: Card):
    """Load averages against the core count."""
    stats = StatRow(theme=page.theme, columns=3)
    stats.add("one", "1 min", size="small")
    stats.add("five", "5 min", size="small")
    stats.add("fifteen", "15 min", size="small")
    card.add(stats)
    meter = BarMeter(theme=page.theme, label="Load per core")
    card.add(meter)

    def update(domain: str, snapshot: Any) -> None:
        one, five, fifteen = snapshot.load_average
        stats["one"].set_value(f"{one:.2f}")
        stats["five"].set_value(f"{five:.2f}")
        stats["fifteen"].set_value(f"{fifteen:.2f}")
        cores = max(1, snapshot.topology.logical_cores)
        ratio = 100.0 * one / cores
        meter.set_value(min(100.0, ratio), f"{one / cores:.2f}x of {cores} cores")

    return update


def _build_clock(page: DashboardPage, card: Card):
    """A live clock.

    Driven by its own one-second timer rather than by a snapshot, because the
    clock must tick even when monitoring is paused.
    """
    label = QLabel("--:--:--")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet(
        f"font-family: {page.metrics.mono_family}; "
        f"font-size: {page.metrics.font_display}pt; font-weight: 600; "
        f"color: {page.palette_tokens.text};"
    )
    card.add(label)
    date_label = QLabel("")
    date_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    date_label.setStyleSheet(f"color: {page.palette_tokens.text_muted};")
    card.add(date_label)

    def tick() -> None:
        now = time.localtime()
        label.setText(time.strftime("%H:%M:%S", now))
        date_label.setText(time.strftime("%A, %d %B %Y", now))

    timer = QTimer(card)
    timer.setInterval(1000)
    timer.timeout.connect(tick)
    timer.start()
    tick()

    def update(domain: str, snapshot: Any) -> None:
        """The clock ignores snapshots; it is driven by its own timer."""

    return update


def _build_notes(page: DashboardPage, card: Card):
    """A persistent scratchpad, saved to the settings document."""
    editor = QPlainTextEdit()
    editor.setPlaceholderText("Notes are saved automatically and stay on this machine.")
    editor.setPlainText(str(page.context.config.get("ui_state.dashboard_notes", "")))
    editor.setMinimumHeight(110)
    card.add(editor)

    # Debounced save: writing on every keystroke would rewrite the settings file
    # dozens of times a second.
    save_timer = QTimer(card)
    save_timer.setSingleShot(True)
    save_timer.setInterval(900)
    save_timer.timeout.connect(
        lambda: page.context.config.set(
            "ui_state.dashboard_notes", editor.toPlainText()
        )
    )
    editor.textChanged.connect(save_timer.start)

    def update(domain: str, snapshot: Any) -> None:
        """Notes hold no live data."""

    return update


def _build_services(page: DashboardPage, card: Card):
    """Systemd unit health."""
    stats = StatRow(theme=page.theme, columns=2)
    stats.add("active", "Active units", size="small")
    stats.add("failed", "Failed units", size="small")
    card.add(stats)
    listing = QLabel("")
    listing.setWordWrap(True)
    listing.setStyleSheet(f"color: {page.palette_tokens.text_muted};")
    card.add(listing)

    def update(domain: str, snapshot: Any) -> None:
        stats["active"].set_value(str(snapshot.active_count))
        failed = snapshot.failed
        stats["failed"].set_value(
            str(len(failed)),
            colour=(
                page.palette_tokens.danger if failed
                else page.palette_tokens.success
            ),
        )
        listing.setText(
            ", ".join(u.name for u in failed[:6]) if failed
            else (snapshot.note or "All units healthy")
        )

    return update


def _build_alerts(page: DashboardPage, card: Card):
    """Currently firing alerts."""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(page.metrics.space_2)
    card.add(host)
    empty = EmptyState("No active alerts", "", theme=page.theme, icon="✓")
    card.add(empty)
    rows: list[QLabel] = []

    def refresh() -> None:
        active = page.context.alerts.active()
        empty.setVisible(not active)
        while len(rows) < len(active):
            label = QLabel()
            label.setWordWrap(True)
            rows.append(label)
            layout.addWidget(label)
        for index, (rule, value) in enumerate(active):
            colour = page.palette_tokens.status(int(rule.severity))
            rows[index].setVisible(True)
            rows[index].setText(
                f"<span style='color:{colour}'>●</span> "
                f"{rule.label or rule.metric} — {value:.1f} "
                f"(threshold {rule.threshold:g})"
            )
        for extra in rows[len(active):]:
            extra.setVisible(False)

    def update(domain: str, snapshot: Any) -> None:
        refresh()

    refresh()
    return update


def _build_weather(page: DashboardPage, card: Card):
    """Weather placeholder.

    Deliberately inert.  The application makes a firm promise never to contact the
    network unprompted, and a weather widget cannot honour that.  The placeholder
    documents the decision rather than pretending the feature is merely missing.
    """
    card.add(EmptyState(
        "Weather is not built in",
        "This application never contacts the network on its own, so there is no "
        "weather provider. The widget exists as an integration point: implement "
        "WeatherProvider in app/services and it will appear here.",
        theme=page.theme, icon="☁",
    ))

    def update(domain: str, snapshot: Any) -> None:
        """No data source by design."""

    return update


def _frequency(value: Any) -> str:
    """Local alias so widget closures read consistently."""
    from app.core.units import frequency

    return frequency(value)


def _watts(value: Any) -> str:
    """Local alias for power formatting."""
    from app.core.units import watts

    return watts(value)


#: Every widget the dashboard can show.
_WIDGET_SPECS: tuple[WidgetSpec, ...] = (
    WidgetSpec("cpu", "Processor", ("cpu",), 1, _build_cpu,
               "Utilisation ring, clock and temperature"),
    WidgetSpec("memory", "Memory", ("memory",), 1, _build_memory,
               "RAM ring with swap usage"),
    WidgetSpec("gpu", "Graphics", ("gpu",), 1, _build_gpu,
               "Primary adapter utilisation and VRAM"),
    WidgetSpec("storage", "Storage", ("storage",), 1, _build_storage,
               "Filesystem capacity and throughput"),
    WidgetSpec("network", "Network", ("network",), 1, _build_network,
               "Throughput with trend sparklines"),
    WidgetSpec("thermals", "Temperatures", ("sensors",), 1, _build_thermals,
               "The five hottest sensors"),
    WidgetSpec("battery", "Battery", ("battery",), 1, _build_battery,
               "Charge, health and power flow"),
    WidgetSpec("uptime", "System", ("system",), 1, _build_uptime,
               "Host identity and uptime"),
    WidgetSpec("processes", "Processes", ("processes",), 1, _build_processes,
               "Counts and top consumers"),
    WidgetSpec("load", "Load average", ("cpu",), 1, _build_load,
               "Load averages relative to core count"),
    WidgetSpec("clock", "Clock", (), 1, _build_clock, "Local date and time"),
    WidgetSpec("notes", "Notes", (), 1, _build_notes,
               "A scratchpad saved with your settings"),
    WidgetSpec("services", "Services", ("services",), 1, _build_services,
               "systemd unit health"),
    WidgetSpec("alerts", "Active alerts", ("cpu", "memory", "storage"), 1,
               _build_alerts, "Currently firing alert rules"),
    WidgetSpec("weather", "Weather", (), 1, _build_weather,
               "Integration point (no network access by design)"),
)


class _CustomiseDialog(QDialog):
    """Lets the user choose and order dashboard widgets."""

    def __init__(
        self, specs: dict[str, WidgetSpec], enabled: list[str], *, theme, parent=None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Customise dashboard")
        self.setMinimumSize(460, 480)
        self._specs = specs

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Tick the widgets to show. Drag to reorder — widgets flow into the "
            "grid in this order."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.palette.text_muted};")
        layout.addWidget(hint)

        self._list = QListWidget(self)
        self._list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        # Enabled widgets first in their saved order, then the rest.
        for key in enabled:
            self._add_item(key, True)
        for key in specs:
            if key not in enabled:
                self._add_item(key, False)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_item(self, key: str, checked: bool) -> None:
        """Add one checkable widget entry."""
        spec = self._specs[key]
        item = QListWidgetItem(f"{spec.title} — {spec.description}")
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setFlags(
            item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        self._list.addItem(item)

    def selected_keys(self) -> list[str]:
        """Checked widget keys, in the user's chosen order."""
        keys: list[str] = []
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.checkState() is Qt.CheckState.Checked:
                keys.append(item.data(Qt.ItemDataRole.UserRole))
        return keys
