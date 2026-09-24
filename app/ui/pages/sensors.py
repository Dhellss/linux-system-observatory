"""Sensors page: every temperature, fan, voltage and power reading."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.core.units import rpm, temperature, volts, watts
from app.models.base import is_number
from app.models.sensors import SensorKind, SensorSnapshot, ThermalStatus
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter
from app.ui.widgets.stat import Badge, EmptyState, KeyValueTable, StatRow

#: Formatter per sensor kind.
_FORMATTERS = {
    SensorKind.TEMPERATURE: temperature,
    SensorKind.FAN: rpm,
    SensorKind.VOLTAGE: volts,
    SensorKind.POWER: watts,
    SensorKind.CURRENT: lambda v: f"{v:.3f} A",
    SensorKind.ENERGY: lambda v: f"{v:.1f} J",
    SensorKind.HUMIDITY: lambda v: f"{v:.1f}%",
}


class SensorsPage(Page):
    """Hardware monitoring chips and their readings."""

    domain = "sensors"
    title = "Sensors"
    icon = "◈"
    section = "System"
    description = "Temperatures, fans, voltages and power"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("sensors")
        self._accent = accent

        card = Card("Thermal overview", theme=self.theme, accent=accent)
        self._status_badge = Badge("Unknown", theme=self.theme)
        card.add_action(self._status_badge)
        self._stats = StatRow(theme=self.theme, columns=4)
        for key, label in (
            ("hottest", "Hottest sensor"), ("value", "Temperature"),
            ("chips", "Sensor chips"), ("readings", "Total readings"),
        ):
            self._stats.add(key, label, size="small")
        card.add(self._stats)

        window = self.context.config.settings.charts.history_seconds
        self._chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 110),
            y_label="°C", height=165,
        )
        self._chart.add_series("sensors.max_temperature", self.palette_tokens.warning)
        self._chart.add_series("cpu.temperature", self.palette_tokens.series[0])
        self._chart.add_series("gpu.temperature", self.palette_tokens.series[2])
        card.add(self._chart)
        self.add(card)

        self._empty = EmptyState(
            "No hardware sensors detected",
            "This machine exposes no hwmon devices. Sensors may require the "
            "lm_sensors package and a run of sensors-detect.",
            theme=self.theme, icon="⊘",
        )
        self._empty.setVisible(False)
        self.add(self._empty)

        self._chip_host = QWidget(self)
        self._chip_layout = QVBoxLayout(self._chip_host)
        self._chip_layout.setContentsMargins(0, 0, 0, 0)
        self._chip_layout.setSpacing(self.metrics.space_4)
        self.add(self._chip_host)
        self.add_stretch()
        self._chips: dict[str, _ChipSection] = {}

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a sensor snapshot."""
        if not isinstance(snapshot, SensorSnapshot):
            return
        history = self.context.history
        for metric in ("sensors.max_temperature", "cpu.temperature",
                       "gpu.temperature"):
            self._chart.update_series(metric, history.series(metric))
        if not self.is_visible_page:
            return

        palette = self.palette_tokens
        self._empty.setVisible(not snapshot.chips)
        if snapshot.note and not snapshot.chips:
            self._empty.set_message("No hardware sensors detected", snapshot.note)

        hottest = snapshot.hottest
        self._stats["hottest"].set_value(
            f"{hottest.chip} · {hottest.label}" if hottest else "—"
        )
        self._stats["value"].set_value(
            temperature(hottest.value) if hottest else "—",
            colour=palette.status(_severity(snapshot.worst_status)),
        )
        self._stats["chips"].set_value(str(len(snapshot.chips)))
        self._stats["readings"].set_value(str(len(snapshot.all_sensors)))

        status = snapshot.worst_status
        self._status_badge.set_state(
            status.value, palette.status(_severity(status))
        )

        for chip in snapshot.chips:
            section = self._chips.get(chip.name)
            if section is None:
                section = _ChipSection(self, chip, self._accent)
                self._chips[chip.name] = section
                self._chip_layout.addWidget(section.card)
            section.update(chip)

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()


def _severity(status: ThermalStatus) -> int:
    """Map a thermal status onto the shared severity scale."""
    return {
        ThermalStatus.NORMAL: 0, ThermalStatus.WARM: 1,
        ThermalStatus.HOT: 2, ThermalStatus.CRITICAL: 3,
    }.get(status, 1)


class _ChipSection:
    """Card for one hwmon chip."""

    def __init__(self, page: SensorsPage, chip, accent: str) -> None:
        theme = page.theme
        self._page = page
        self.card = Card(
            chip.label, theme=theme, subtitle=chip.name, accent=accent
        )
        self._bars: dict[str, BarMeter] = {}
        self._values = KeyValueTable(theme=theme, columns=2, label_width=170)
        self._bar_host = QWidget()
        self._bar_layout = QVBoxLayout(self._bar_host)
        self._bar_layout.setContentsMargins(0, 0, 0, 0)
        self._bar_layout.setSpacing(theme.metrics.space_1)
        self.card.add(self._bar_host)
        self.card.add(self._values)
        self._rows: set[str] = set()

    def update(self, chip) -> None:
        """Refresh every sensor on this chip."""
        theme = self._page.theme
        for sensor in chip.sensors:
            formatter = _FORMATTERS.get(sensor.kind, lambda v: str(v))
            display = (
                formatter(sensor.value)
                if isinstance(sensor.value, (int, float)) else "—"
            )
            if sensor.kind is SensorKind.TEMPERATURE:
                bar = self._bars.get(sensor.key)
                if bar is None:
                    bar = BarMeter(theme=theme, label=sensor.label)
                    self._bars[sensor.key] = bar
                    self._bar_layout.addWidget(bar)
                limit = (
                    sensor.critical if is_number(sensor.critical) else 100.0
                )
                value = sensor.value if is_number(sensor.value) else 0.0
                suffix = ""
                if is_number(sensor.critical):
                    suffix = f"   (limit {temperature(sensor.critical)})"
                bar.set_value(
                    min(100.0, 100.0 * value / limit), f"{display}{suffix}"
                )
                bar.set_colour(theme.palette.status(_severity(sensor.status)))
            else:
                if sensor.key not in self._rows:
                    self._values.add_row(sensor.key, sensor.label)
                    self._rows.add(sensor.key)
                self._values.set(sensor.key, display)
