"""Graphics page: one section per adapter, plus the session graphics stack."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from app.core.units import bytes_, frequency, percent, temperature, text, volts, watts
from app.models.base import is_available
from app.models.gpu import GpuSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter, RingGauge
from app.ui.widgets.stat import Badge, Divider, EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable


class GpuPage(Page):
    """Per-adapter telemetry for NVIDIA, AMD and Intel graphics."""

    domain = "gpu"
    title = "Graphics"
    icon = "◈"
    section = "Monitor"
    description = "GPU utilisation, VRAM, clocks and driver state"

    def build_ui(self) -> None:
        """Assemble the page; adapter sections are created on first snapshot."""
        self._accent = self.context.colour("gpu")
        self._adapter_host = QWidget(self)
        self._adapter_layout = QVBoxLayout(self._adapter_host)
        self._adapter_layout.setContentsMargins(0, 0, 0, 0)
        self._adapter_layout.setSpacing(self.metrics.space_4)
        self.add(self._adapter_host)

        self._empty = EmptyState(
            "No graphics adapter detected",
            "Checking for NVIDIA, AMD and Intel interfaces…",
            theme=self.theme, icon="▢",
        )
        self.add(self._empty)

        self._build_stack()
        self.add_stretch()
        self._sections: dict[int, _AdapterSection] = {}

    def _build_stack(self) -> None:
        """The session-wide graphics stack, shared by all adapters."""
        card = Card(
            "Graphics stack", theme=self.theme,
            subtitle="Session-wide, not per adapter", accent=self._accent,
        )
        self._stack = KeyValueTable(theme=self.theme, columns=2, label_width=140)
        for key, label in (
            ("session", "Session type"), ("compositor", "Desktop / compositor"),
            ("server", "Display server"), ("renderer", "OpenGL renderer"),
            ("gl_version", "OpenGL version"), ("gl_vendor", "OpenGL vendor"),
            ("glsl", "GLSL version"), ("vulkan", "Vulkan API"),
            ("vulkan_devices", "Vulkan devices"),
        ):
            self._stack.add_row(key, label)
        card.add(self._stack)
        self.add(card)

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a GPU snapshot, creating adapter sections as needed."""
        if not isinstance(snapshot, GpuSnapshot):
            return
        history = self.context.history
        for metric in ("gpu.usage", "gpu.temperature", "gpu.vram_percent"):
            for section in self._sections.values():
                section.update_chart(metric, history.series(metric))
        if not self.is_visible_page:
            return

        self._empty.setVisible(not snapshot.gpus)
        if not snapshot.gpus and snapshot.note:
            self._empty.set_message("No graphics adapter detected", snapshot.note)

        for sample in snapshot.gpus:
            index = sample.device.index
            existing = self._sections.get(index)
            if existing is None:
                existing = _AdapterSection(self, sample.device, self._accent)
                self._sections[index] = existing
                self._adapter_layout.addWidget(existing.card)
            existing.update(sample)

        stack = snapshot.stack
        self._stack.update_many({
            "session": stack.session_type,
            "compositor": stack.compositor,
            "server": stack.display_server_version,
            "renderer": stack.opengl_renderer,
            "gl_version": stack.opengl_version,
            "gl_vendor": stack.opengl_vendor,
            "glsl": stack.glsl_version,
            "vulkan": stack.vulkan_api_version,
            "vulkan_devices": ", ".join(stack.vulkan_devices) or "—",
        })

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()


class _AdapterSection:
    """The card and widgets for one graphics adapter.

    Extracted into its own class so a multi-GPU machine gets identical, fully
    independent sections without the page duplicating widget-building code.
    """

    def __init__(self, page: GpuPage, device, accent: str) -> None:
        self._page = page
        theme = page.theme
        self.card = Card(
            device.name, theme=theme,
            subtitle=f"{device.vendor.value} · {text(device.driver_name)}",
            accent=accent,
        )
        self._driverless = not is_available(device.driver_version)

        row = QHBoxLayout()
        row.setSpacing(theme.metrics.space_5)
        self._usage_ring = RingGauge(theme=theme, label="utilisation")
        self._vram_ring = RingGauge(theme=theme, label="VRAM")
        self._temp_ring = RingGauge(theme=theme, label="temperature")
        for ring in (self._usage_ring, self._vram_ring, self._temp_ring):
            row.addWidget(ring)

        self._stats = StatRow(theme=theme, columns=2)
        for key, label in (
            ("core_clock", "Core clock"), ("memory_clock", "Memory clock"),
            ("power", "Power draw"), ("fan", "Fan"),
            ("pstate", "Performance state"), ("encoder", "Encoder / decoder"),
        ):
            self._stats.add(key, label)
        row.addWidget(self._stats, 1)
        self.card.add_layout(row)

        self._throttle = Badge("", theme=theme, colour=theme.palette.warning)
        self._throttle.setVisible(False)
        self.card.add_action(self._throttle)

        self.card.add(Divider())
        self._vram_bar = BarMeter(theme=theme, label="VRAM")
        self.card.add(self._vram_bar)

        window = page.context.config.settings.charts.history_seconds
        self._chart = LiveChart(
            theme=theme, window_seconds=window, y_range=(0, 100),
            y_label="%", height=150,
        )
        self._chart.add_series("gpu.usage", accent)
        self._chart.add_series("gpu.vram_percent", theme.palette.series[3])
        self._chart.add_series("gpu.temperature", theme.palette.warning)
        self.card.add(self._chart)

        self.card.add(Divider())
        self._details = KeyValueTable(theme=theme, columns=2, label_width=130)
        for key, label in (
            ("driver", "Driver version"), ("vram", "Total VRAM"),
            ("bus", "PCI address"), ("link", "PCIe link"),
            ("vbios", "VBIOS"), ("compute", "Compute capability"),
            ("uuid", "UUID"), ("limit", "Power limit"),
            ("voltage", "Core voltage"), ("hotspot", "Hotspot temperature"),
        ):
            self._details.add_row(key, label)
        self.card.add(self._details)

        self._process_table = DataTable(
            [
                Column("pid", "PID", lambda p: p.pid, width=70, mono=True),
                Column("name", "Process", lambda p: p.name, width=0),
                Column(
                    "vram", "GPU memory", lambda p: p.used_vram_bytes or 0,
                    display=bytes_, width=110, mono=True,
                ),
                Column("kind", "Type", lambda p: p.kind.title(), width=90),
            ],
            theme=theme, stretch_column=1,
        )
        self._process_table.setMaximumHeight(int(theme.metrics.row_height * 7))
        self.card.add(self._process_table)
        self._no_processes = EmptyState(
            "No processes are using this adapter", "", theme=theme, icon="○"
        )
        self.card.add(self._no_processes)

        self._apply_static(device)

    def _apply_static(self, device) -> None:
        """Fill the immutable identity fields."""
        link = "—"
        if is_available(device.pcie_gen) and is_available(device.pcie_width):
            link = f"Gen {device.pcie_gen} {device.pcie_width}"
        elif is_available(device.pcie_gen):
            link = f"Gen {device.pcie_gen}"
        self._details.update_many({
            "driver": device.driver_version,
            "vram": bytes_(device.vram_total_bytes),
            "bus": device.pci_bus_id,
            "link": link,
            "vbios": device.vbios_version,
            "compute": device.compute_capability,
            "uuid": device.uuid,
            "limit": watts(device.max_power_w),
        })

    def update_chart(self, metric: str, series) -> None:
        """Push history into this adapter's chart.

        Only the primary adapter contributes to the shared ``gpu.*`` metrics, so
        a second adapter's chart intentionally stays empty rather than plotting
        another card's data.
        """
        if self._driverless:
            return
        self._chart.update_series(metric, series)

    def update(self, sample) -> None:
        """Refresh live values from a sample."""
        theme = self._page.theme
        self._usage_ring.set_value(sample.utilisation)
        self._vram_ring.set_value(sample.vram_percent)
        if is_available(sample.temperature):
            value = float(sample.temperature)
            self._temp_ring.set_value(min(100.0, value), temperature(value))
        else:
            self._temp_ring.set_value(sample.temperature)

        self._stats.set("core_clock", sample.core_clock_mhz, frequency)
        self._stats.set("memory_clock", sample.memory_clock_mhz, frequency)
        self._stats["power"].set_value(
            f"{watts(sample.power_w)} / {watts(sample.power_limit_w)}"
            if is_available(sample.power_w) else "—"
        )
        self._stats.set("fan", sample.fan_percent, percent)
        self._stats.set("pstate", sample.performance_state, text)
        self._stats["encoder"].set_value(
            f"{percent(sample.encoder_utilisation)} / "
            f"{percent(sample.decoder_utilisation)}"
        )

        used, total = sample.vram_used_bytes, sample.device.vram_total_bytes
        if isinstance(used, int) and isinstance(total, int) and total:
            self._vram_bar.set_value(
                100.0 * used / total, f"{bytes_(used)} / {bytes_(total)}"
            )
        else:
            self._vram_bar.set_value(sample.vram_percent)

        self._details.update_many({
            "voltage": volts(sample.voltage_v),
            "hotspot": temperature(sample.hotspot_temperature),
        })

        if sample.throttle_reasons:
            self._throttle.set_state(
                "Throttling: " + ", ".join(sample.throttle_reasons),
                theme.palette.warning,
            )
            self._throttle.setVisible(True)
        else:
            self._throttle.setVisible(False)

        self._process_table.set_rows(sample.processes)
        self._process_table.setVisible(bool(sample.processes))
        self._no_processes.setVisible(not sample.processes)
