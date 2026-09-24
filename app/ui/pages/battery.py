"""Battery page: charge, health, power flow and hardware identity."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

from app.core.units import duration, percent, temperature, text, volts, watts
from app.models.base import is_number
from app.models.sensors import BatterySnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter, RingGauge
from app.ui.widgets.stat import Badge, Divider, EmptyState, KeyValueTable, StatRow


class BatteryPage(Page):
    """Battery charge and, more importantly, battery health."""

    domain = "battery"
    title = "Battery"
    icon = "▭"
    section = "System"
    description = "Charge, health, wear and power flow"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("battery")
        self._empty = EmptyState(
            "No battery detected",
            "This machine reports no power supply of type Battery. Desktop "
            "systems and most servers will show this.",
            theme=self.theme, icon="⊘",
        )
        self.add(self._empty)

        self._main = Card("Battery", theme=self.theme, accent=accent)
        self._status_badge = Badge("", theme=self.theme)
        self._main.add_action(self._status_badge)

        row = QHBoxLayout()
        row.setSpacing(self.metrics.space_5)
        # Battery gauges are "reserve" metrics: a high reading is good.
        self._charge_ring = RingGauge(
            theme=self.theme, label="charge", sense="reserve"
        )
        self._health_ring = RingGauge(
            theme=self.theme, label="health", sense="reserve"
        )
        row.addWidget(self._charge_ring)
        row.addWidget(self._health_ring)

        self._stats = StatRow(theme=self.theme, columns=2)
        for key, label in (
            ("status", "Status"), ("rate", "Power flow"),
            ("remaining", "Time remaining"), ("voltage", "Voltage"),
            ("cycles", "Charge cycles"), ("profile", "Power profile"),
        ):
            self._stats.add(key, label)
        row.addWidget(self._stats, 1)
        self._main.add_layout(row)

        self._main.add(Divider())
        self._wear_bar = BarMeter(theme=self.theme, label="Capacity lost to wear")
        self._main.add(self._wear_bar)
        self.add(self._main)

        grid = self.add_grid(2)
        window = self.context.config.settings.charts.history_seconds

        charge_card = Card("Charge history", theme=self.theme, accent=accent)
        self._charge_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 100),
            y_label="%", height=160,
        )
        self._charge_chart.add_series("battery.percent", accent)
        charge_card.add(self._charge_chart)
        grid.addWidget(charge_card, 0, 0)

        power_card = Card("Power draw history", theme=self.theme, accent=accent)
        self._power_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=None,
            y_label="W", height=160,
        )
        self._power_chart.add_series("battery.power", self.palette_tokens.series[1])
        power_card.add(self._power_chart)
        grid.addWidget(power_card, 0, 1)

        detail = Card(
            "Capacity and hardware", theme=self.theme,
            subtitle="Health is present full-charge capacity divided by the "
                     "factory design capacity",
            accent=accent,
        )
        self._detail = KeyValueTable(theme=self.theme, columns=2, label_width=155)
        for key, label in (
            ("energy_now", "Current charge"), ("energy_full", "Full charge capacity"),
            ("energy_design", "Design capacity"), ("health", "Health"),
            ("wear", "Wear"), ("capacity_level", "Capacity level"),
            ("voltage_now", "Current voltage"), ("voltage_design", "Design voltage"),
            ("current", "Current"), ("temperature", "Temperature"),
            ("technology", "Technology"), ("manufacturer", "Manufacturer"),
            ("model", "Model"), ("serial", "Serial number"),
        ):
            self._detail.add_row(key, label)
        detail.add(self._detail)
        self._detail_card = detail
        self.add(detail)
        self.add_stretch()
        self._set_present(False)

    def _set_present(self, present: bool) -> None:
        """Show either the battery cards or the empty state."""
        self._empty.setVisible(not present)
        self._main.setVisible(present)
        self._detail_card.setVisible(present)

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a battery snapshot."""
        if not isinstance(snapshot, BatterySnapshot):
            return
        history = self.context.history
        self._charge_chart.update_series(
            "battery.percent", history.series("battery.percent")
        )
        self._power_chart.update_series(
            "battery.power", history.series("battery.power")
        )
        if not self.is_visible_page:
            return

        self._set_present(snapshot.present)
        if not snapshot.present:
            if snapshot.note:
                self._empty.set_message("No battery detected", snapshot.note)
            return

        palette = self.palette_tokens
        self._charge_ring.set_value(snapshot.percent)
        health = snapshot.health_percent
        self._health_ring.set_value(health)

        status = text(snapshot.status)
        self._status_badge.set_state(
            status,
            palette.success if snapshot.charging
            else palette.info if status.lower() == "full"
            else palette.warning,
        )
        self._stats["status"].set_value(status)
        self._stats["rate"].set_value(
            f"{watts(snapshot.power_now_w)}"
        )
        self._stats["rate"].set_caption(snapshot.rate_label)
        self._stats["remaining"].set_value(duration(snapshot.seconds_remaining))
        self._stats.set("voltage", snapshot.voltage_now_v, volts)
        self._stats.set("cycles", snapshot.cycle_count, str)
        self._stats.set("profile", snapshot.power_profile, text)

        wear = snapshot.wear_percent
        if is_number(wear):
            self._wear_bar.setVisible(True)
            self._wear_bar.set_value(
                wear, f"{percent(wear)} lost · {percent(health)} of design remains"
            )
        else:
            self._wear_bar.setVisible(False)

        self._detail.update_many({
            "energy_now": _watt_hours(snapshot.energy_now_wh),
            "energy_full": _watt_hours(snapshot.energy_full_wh),
            "energy_design": _watt_hours(snapshot.energy_design_wh),
            "health": percent(health),
            "wear": percent(wear),
            "capacity_level": snapshot.capacity_level,
            "voltage_now": volts(snapshot.voltage_now_v),
            "voltage_design": volts(snapshot.voltage_design_v),
            "current": (
                f"{snapshot.current_now_a:.3f} A"
                if is_number(snapshot.current_now_a)
                else snapshot.current_now_a
            ),
            "temperature": temperature(snapshot.temperature),
            "technology": snapshot.technology,
            "manufacturer": snapshot.manufacturer,
            "model": snapshot.model,
            "serial": snapshot.serial,
        })

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()


def _watt_hours(value: object) -> str:
    """Format an energy figure in watt-hours."""
    return f"{value:.2f} Wh" if is_number(value) else "—"
