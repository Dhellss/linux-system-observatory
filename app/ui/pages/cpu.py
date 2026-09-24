"""Processor page: load, topology, frequency, thermals and time breakdown."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.core.units import (
    duration,
    frequency,
    percent,
    temperature,
    text,
    watts,
)
from app.models.base import is_available, is_number
from app.models.cpu import CpuSnapshot
from app.models.process import ProcessSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter, CompositionBar, CoreHeatmap, RingGauge
from app.ui.widgets.stat import Divider, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable


class CpuPage(Page):
    """Everything about the processor."""

    domain = "cpu"
    extra_domains = ("processes",)
    title = "Processor"
    icon = "▣"
    section = "Monitor"
    description = "Load, frequency, topology and thermals"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("cpu")
        self._build_summary(accent)
        self._build_history(accent)
        self._build_cores(accent)
        self._build_breakdown(accent)
        self._build_details(accent)
        self._build_top_processes(accent)
        self.add_stretch()

    # ------------------------------------------------------------------ sections

    def _build_summary(self, accent: str) -> None:
        """Ring gauges plus the headline statistics."""
        card = Card("Overview", theme=self.theme, accent=accent)
        row = QHBoxLayout()
        row.setSpacing(self.metrics.space_5)

        self._usage_ring = RingGauge(theme=self.theme, label="utilisation")
        self._temp_ring = RingGauge(theme=self.theme, label="temperature")
        # Clock speed carries no good/bad judgement, so it stays accent-coloured
        # instead of turning amber simply for being high.
        self._freq_ring = RingGauge(
            theme=self.theme, label="of max clock", sense="neutral"
        )
        for ring in (self._usage_ring, self._temp_ring, self._freq_ring):
            row.addWidget(ring)

        self._stats = StatRow(theme=self.theme, columns=2)
        self._stats.add("frequency", "Current clock")
        self._stats.add("governor", "Governor")
        self._stats.add("load", "Load average")
        self._stats.add("power", "Package power")
        self._stats.add("processes", "Runnable / blocked")
        self._stats.add("switches", "Context switches")
        row.addWidget(self._stats, 1)
        card.add_layout(row)
        self.add(card)

    def _build_history(self, accent: str) -> None:
        """Utilisation and temperature over time."""
        grid = self.add_grid(2)
        window = self.context.config.settings.charts.history_seconds

        usage_card = Card("Utilisation history", theme=self.theme, accent=accent)
        self._usage_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 100),
            y_label="%", height=170,
        )
        self._usage_chart.add_series("cpu.usage", accent)
        usage_card.add(self._usage_chart)
        grid.addWidget(usage_card, 0, 0)

        thermal_card = Card("Thermal and power", theme=self.theme, accent=accent)
        self._thermal_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 110),
            y_label="°C", height=170,
        )
        self._thermal_chart.add_series(
            "cpu.temperature", self.palette_tokens.warning
        )
        thermal_card.add(self._thermal_chart)
        self._thermal_threshold_drawn = False
        grid.addWidget(thermal_card, 0, 1)

    def _build_cores(self, accent: str) -> None:
        """Per-core heatmap and frequency bars."""
        card = Card(
            "Per-core activity", theme=self.theme,
            subtitle="Hover a cell for detail", accent=accent,
        )
        self._heatmap = CoreHeatmap(theme=self.theme)
        card.add(self._heatmap)
        card.add(Divider())

        self._core_bars_host = QWidget()
        self._core_bars_layout = QVBoxLayout(self._core_bars_host)
        self._core_bars_layout.setContentsMargins(0, 0, 0, 0)
        self._core_bars_layout.setSpacing(self.metrics.space_1)
        self._core_bars: list[BarMeter] = []
        card.add(self._core_bars_host)
        self.add(card)

    def _build_breakdown(self, accent: str) -> None:
        """Where CPU time is actually being spent."""
        card = Card(
            "CPU time breakdown", theme=self.theme,
            subtitle="Share of the last sampling interval", accent=accent,
        )
        self._breakdown = CompositionBar(theme=self.theme, height=22)
        card.add(self._breakdown)
        self._times = KeyValueTable(theme=self.theme, columns=3, label_width=78)
        for key, label in (
            ("user", "User"), ("system", "System"), ("nice", "Nice"),
            ("iowait", "I/O wait"), ("irq", "IRQ"), ("softirq", "SoftIRQ"),
            ("steal", "Steal"), ("guest", "Guest"), ("idle", "Idle"),
        ):
            self._times.add_row(key, label)
        card.add(self._times)
        self.add(card)

    def _build_details(self, accent: str) -> None:
        """Static topology and scaling configuration."""
        grid = self.add_grid(2)

        identity = Card("Processor identity", theme=self.theme, accent=accent)
        self._identity = KeyValueTable(theme=self.theme, label_width=130)
        for key, label in (
            ("model", "Model"), ("vendor", "Vendor"),
            ("architecture", "Architecture"), ("sockets", "Sockets"),
            ("physical", "Physical cores"), ("logical", "Logical cores"),
            ("smt", "Threads per core"), ("family", "Family / stepping"),
            ("microcode", "Microcode"), ("bogomips", "BogoMIPS"),
            ("numa", "NUMA nodes"), ("virt", "Virtualisation"),
            ("byte_order", "Byte order"),
        ):
            self._identity.add_row(key, label)
        identity.add(self._identity)
        grid.addWidget(identity, 0, 0)

        scaling = Card("Frequency scaling and cache", theme=self.theme, accent=accent)
        self._scaling = KeyValueTable(theme=self.theme, label_width=130)
        for key, label in (
            ("driver", "Scaling driver"), ("governor", "Active governor"),
            ("available", "Available governors"), ("turbo", "Turbo / boost"),
            ("min", "Minimum clock"), ("max", "Maximum clock"),
            ("temp_high", "High threshold"), ("temp_crit", "Critical threshold"),
            ("pressure", "CPU pressure (10 s)"),
        ):
            self._scaling.add_row(key, label)
        scaling.add(self._scaling)
        scaling.add(Divider())
        self._cache = KeyValueTable(theme=self.theme, columns=2, label_width=130)
        scaling.add(self._cache)
        self._cache_built = False
        grid.addWidget(scaling, 0, 1)

    def _build_top_processes(self, accent: str) -> None:
        """The heaviest CPU consumers, so the page answers "why is it busy?"."""
        card = Card(
            "Top CPU consumers", theme=self.theme,
            subtitle="Updated with the process table", accent=accent,
        )
        columns: list[Column] = [
            Column("pid", "PID", lambda p: p.pid, width=70, mono=True),
            Column("name", "Process", lambda p: p.name, width=0),
            Column(
                "cpu", "CPU", lambda p: p.cpu_percent,
                display=lambda v: percent(v), width=80, mono=True,
                colour=lambda p: self.palette_tokens.load_colour(p.cpu_percent * 4),
            ),
            Column(
                "time", "CPU time", lambda p: p.cpu_time or 0,
                display=lambda v: duration(v, compact=True), width=90, mono=True,
            ),
            Column(
                "threads", "Threads", lambda p: p.num_threads,
                width=80, mono=True,
            ),
        ]
        self._top_table = DataTable(columns, theme=self.theme, stretch_column=1)
        self._top_table.setMaximumHeight(int(self.metrics.row_height * 9))
        self._top_table.setSortingEnabled(False)
        card.add(self._top_table)
        self.add(card)

    # ------------------------------------------------------------------- updates

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a new CPU or process snapshot."""
        if domain == "cpu" and isinstance(snapshot, CpuSnapshot):
            self._apply_cpu(snapshot)
        elif (
            domain == "processes"
            and isinstance(snapshot, ProcessSnapshot)
            and self.is_visible_page
        ):
            self._top_table.set_rows(snapshot.top_cpu(8))

    def _apply_cpu(self, snapshot: CpuSnapshot) -> None:
        """Refresh every widget from a CPU snapshot.

        Chart series are updated even when the page is hidden, because the
        history buffers they read are shared and must stay continuous; only the
        expensive per-widget repaints are skipped.
        """
        history = self.context.history
        self._usage_chart.update_series(
            "cpu.usage", history.series("cpu.usage")
        )
        self._thermal_chart.update_series(
            "cpu.temperature", history.series("cpu.temperature")
        )
        if not self.is_visible_page:
            return

        palette = self.palette_tokens
        self._usage_ring.set_value(snapshot.usage)
        if is_number(snapshot.temperature):
            value = float(snapshot.temperature)
            critical = (
                float(snapshot.temperature_critical)
                if is_number(snapshot.temperature_critical) else 100.0
            )
            self._temp_ring.set_value(
                min(100.0, 100.0 * value / critical), temperature(value)
            )
            self._temp_ring.set_colour(self._thermal_colour(value, critical))
            if not self._thermal_threshold_drawn and critical:
                self._thermal_chart.add_threshold(
                    critical, palette.danger, f"{critical:.0f} °C"
                )
                self._thermal_threshold_drawn = True
        else:
            self._temp_ring.set_value(snapshot.temperature)

        has_clocks = (
            is_available(snapshot.frequency_mhz)
            and is_available(snapshot.frequency_max_mhz)
        )
        if has_clocks:
            current = float(snapshot.frequency_mhz)   # type: ignore[arg-type]
            maximum = float(snapshot.frequency_max_mhz)  # type: ignore[arg-type]
            self._freq_ring.set_value(
                100.0 * current / maximum if maximum else 0.0, frequency(current)
            )
        else:
            self._freq_ring.set_value(snapshot.frequency_mhz)

        one, five, fifteen = snapshot.load_average
        self._stats.set("frequency", snapshot.frequency_mhz, frequency)
        self._stats.set("governor", snapshot.governor, text)
        self._stats["load"].set_value(f"{one:.2f}  {five:.2f}  {fifteen:.2f}")
        self._stats.set("power", snapshot.package_power_w, watts)
        self._stats["processes"].set_value(
            f"{text(snapshot.processes_running)} / {text(snapshot.processes_blocked)}"
        )
        self._stats.set("switches", snapshot.context_switches_per_s,
                        lambda v: f"{v / 1000:.1f}K/s" if v >= 1000 else f"{v:.0f}/s")

        self._update_cores(snapshot)
        self._update_breakdown(snapshot)
        self._update_details(snapshot)

    def _update_cores(self, snapshot: CpuSnapshot) -> None:
        """Refresh the heatmap and per-core bars."""
        usages = [core.usage for core in snapshot.cores]
        tooltips = [
            f"CPU {core.index} — {percent(core.usage)}\n"
            f"Clock: {frequency(core.frequency_mhz)}\n"
            f"Temperature: {temperature(core.temperature)}\n"
            f"Package {text(core.package)}, core {text(core.core_id)}"
            for core in snapshot.cores
        ]
        self._heatmap.set_values(usages, tooltips)

        # Build the bar list once, then update in place.  Rebuilding on every
        # sample would allocate a widget per core per second.
        while len(self._core_bars) < len(snapshot.cores):
            bar = BarMeter(theme=self.theme, height=5)
            self._core_bars.append(bar)
            self._core_bars_layout.addWidget(bar)
        for index, core in enumerate(snapshot.cores):
            bar = self._core_bars[index]
            bar.set_label(f"CPU {core.index}")
            bar.set_value(
                core.usage,
                f"{percent(core.usage)}   {frequency(core.frequency_mhz)}",
            )
        for extra in self._core_bars[len(snapshot.cores):]:
            extra.setVisible(False)

    def _update_breakdown(self, snapshot: CpuSnapshot) -> None:
        """Refresh the CPU time composition."""
        palette = self.palette_tokens
        pairs = snapshot.times.as_pairs()
        colours = {
            "User": palette.series[0], "System": palette.series[4],
            "Nice": palette.series[3], "I/O wait": palette.series[1],
            "IRQ": palette.series[5], "SoftIRQ": palette.series[6],
            "Steal": palette.danger, "Guest": palette.series[2],
            "Idle": palette.surface_sunken,
        }
        self._breakdown.set_segments(
            [(label, value, colours.get(label, palette.accent))
             for label, value in pairs if value > 0.05]
        )
        times = snapshot.times
        self._times.update_many({
            "user": percent(times.user), "system": percent(times.system),
            "nice": percent(times.nice), "iowait": percent(times.iowait),
            "irq": percent(times.irq), "softirq": percent(times.softirq),
            "steal": percent(times.steal), "guest": percent(times.guest),
            "idle": percent(times.idle),
        })

    def _update_details(self, snapshot: CpuSnapshot) -> None:
        """Refresh the static topology and scaling tables."""
        topology = snapshot.topology
        self._identity.update_many({
            "model": topology.model,
            "vendor": topology.vendor,
            "architecture": topology.architecture,
            "sockets": topology.sockets,
            "physical": topology.physical_cores,
            "logical": topology.logical_cores,
            "smt": (
                f"{topology.threads_per_core} (SMT enabled)"
                if topology.smt_enabled else topology.threads_per_core
            ),
            "family": f"{text(topology.family)} / {text(topology.stepping)}",
            "microcode": topology.microcode,
            "bogomips": (
                f"{topology.bogomips:.2f}"
                if is_number(topology.bogomips) else topology.bogomips
            ),
            "numa": topology.numa_nodes,
            "virt": topology.virtualisation,
            "byte_order": topology.byte_order,
        })
        self._scaling.update_many({
            "driver": snapshot.driver,
            "governor": snapshot.governor,
            "available": ", ".join(snapshot.available_governors) or "—",
            "turbo": snapshot.turbo_enabled,
            "min": frequency(snapshot.frequency_min_mhz),
            "max": frequency(snapshot.frequency_max_mhz),
            "temp_high": temperature(snapshot.temperature_high),
            "temp_crit": temperature(snapshot.temperature_critical),
            "pressure": percent(snapshot.pressure_some),
        })
        if not self._cache_built and topology.cache:
            for level, size in sorted(topology.cache.items()):
                self._cache.add_row(level, f"Cache {level}", size)
            self._cache_built = True

    def _thermal_colour(self, value: float, critical: float) -> str:
        """Colour a temperature against the hardware's own critical limit."""
        palette = self.palette_tokens
        if value >= critical * 0.95:
            return palette.danger
        if value >= critical * 0.85:
            return palette.warning
        return palette.success

    def on_shown(self) -> None:
        """Refresh immediately on entry rather than waiting for the next tick."""
        super().on_shown()
        self.refresh()
