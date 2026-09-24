"""Memory page: RAM composition, swap, zram, huge pages and pressure."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

from app.core.units import bytes_, percent
from app.models.memory import MemorySnapshot, PressureStall
from app.models.process import ProcessSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.gauge import BarMeter, CompositionBar, RingGauge
from app.ui.widgets.stat import Divider, EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable


class MemoryPage(Page):
    """Everything about system memory."""

    domain = "memory"
    extra_domains = ("processes",)
    title = "Memory"
    icon = "▤"
    section = "Monitor"
    description = "RAM, swap, compression and pressure"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("memory")
        self._build_summary(accent)
        self._build_history(accent)
        self._build_detail(accent)
        self._build_compression(accent)
        self._build_top_processes(accent)
        self.add_stretch()

    def _build_summary(self, accent: str) -> None:
        """Composition bar, ring gauges and headline figures."""
        card = Card("Overview", theme=self.theme, accent=accent)
        row = QHBoxLayout()
        row.setSpacing(self.metrics.space_5)
        self._ram_ring = RingGauge(theme=self.theme, label="RAM used")
        self._swap_ring = RingGauge(theme=self.theme, label="swap used")
        row.addWidget(self._ram_ring)
        row.addWidget(self._swap_ring)

        self._stats = StatRow(theme=self.theme, columns=2)
        self._stats.add("total", "Total")
        self._stats.add("available", "Available")
        self._stats.add("cache", "Cache and buffers")
        self._stats.add("shared", "Shared")
        self._stats.add("swap", "Swap used")
        self._stats.add("pressure", "Pressure (10 s)")
        row.addWidget(self._stats, 1)
        card.add_layout(row)

        card.add(Divider())
        self._composition = CompositionBar(theme=self.theme, height=24)
        card.add(self._composition)
        self.add(card)

    def _build_history(self, accent: str) -> None:
        """Memory and swap over time."""
        grid = self.add_grid(2)
        window = self.context.config.settings.charts.history_seconds

        usage = Card("Memory history", theme=self.theme, accent=accent)
        self._usage_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 100),
            y_label="%", height=165,
        )
        self._usage_chart.add_series("memory.percent", accent)
        self._usage_chart.add_series("memory.swap_percent",
                                     self.palette_tokens.series[1])
        usage.add(self._usage_chart)
        grid.addWidget(usage, 0, 0)

        pressure = Card(
            "Memory pressure", theme=self.theme,
            subtitle="Share of time tasks stalled waiting for memory",
            accent=accent,
        )
        self._pressure_chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=(0, 100),
            y_label="%", height=165,
        )
        self._pressure_chart.add_series(
            "memory.pressure", self.palette_tokens.danger
        )
        pressure.add(self._pressure_chart)
        grid.addWidget(pressure, 0, 1)

    def _build_detail(self, accent: str) -> None:
        """Kernel memory accounting and swap areas."""
        grid = self.add_grid(2)

        kernel = Card("Kernel memory accounting", theme=self.theme, accent=accent)
        self._kernel = KeyValueTable(theme=self.theme, columns=2, label_width=120)
        for key, label in (
            ("total", "Total"), ("free", "Free"),
            ("available", "Available"), ("used", "Used"),
            ("cached", "Cached"), ("buffers", "Buffers"),
            ("shared", "Shared (tmpfs)"), ("anonymous", "Anonymous"),
            ("mapped", "Mapped"), ("slab", "Slab"),
            ("slab_reclaim", "Slab reclaimable"), ("page_tables", "Page tables"),
            ("kernel_stack", "Kernel stacks"), ("dirty", "Dirty"),
            ("writeback", "Writeback"), ("committed", "Committed"),
            ("commit_limit", "Commit limit"), ("thp", "Transparent huge pages"),
        ):
            self._kernel.add_row(key, label)
        kernel.add(self._kernel)
        grid.addWidget(kernel, 0, 0)

        swap = Card("Swap areas", theme=self.theme, accent=accent)
        self._swap_body = swap.body()
        self._swap_bars: dict[str, BarMeter] = {}
        self._swap_empty = EmptyState(
            "No swap configured",
            "Without swap the kernel cannot page out idle memory and will "
            "invoke the OOM killer sooner under pressure.",
            theme=self.theme, icon="⇄",
        )
        swap.add(self._swap_empty)
        swap.add(Divider())
        self._pressure_table = KeyValueTable(theme=self.theme, label_width=150)
        for key, label in (
            ("mem10", "Memory stall (10 s)"), ("mem60", "Memory stall (1 min)"),
            ("mem300", "Memory stall (5 min)"), ("io10", "I/O stall (10 s)"),
            ("cpu10", "CPU stall (10 s)"),
        ):
            self._pressure_table.add_row(key, label)
        swap.add(self._pressure_table)
        self._swap_card = swap
        grid.addWidget(swap, 0, 1)

    def _build_compression(self, accent: str) -> None:
        """Zram devices and huge page pools."""
        grid = self.add_grid(2)

        zram = Card(
            "Compressed memory (zram)", theme=self.theme,
            subtitle="Compression ratio is the RAM actually saved", accent=accent,
        )
        self._zram_table = KeyValueTable(theme=self.theme, label_width=150)
        zram.add(self._zram_table)
        self._zram_empty = EmptyState(
            "No zram devices",
            "zram provides compressed swap in RAM, which is usually faster "
            "than swapping to disk.",
            theme=self.theme, icon="⊕",
        )
        zram.add(self._zram_empty)
        self._zram_card = zram
        self._zram_built = False
        grid.addWidget(zram, 0, 0)

        huge = Card("Huge pages", theme=self.theme, accent=accent)
        self._huge_table = KeyValueTable(theme=self.theme, label_width=150)
        huge.add(self._huge_table)
        self._huge_empty = EmptyState(
            "No huge page pools configured",
            "Huge pages reduce TLB pressure for databases and virtual machines.",
            theme=self.theme, icon="▦",
        )
        huge.add(self._huge_empty)
        self._huge_built = False
        grid.addWidget(huge, 0, 1)

    def _build_top_processes(self, accent: str) -> None:
        """The heaviest memory consumers."""
        card = Card("Top memory consumers", theme=self.theme, accent=accent)
        columns: list[Column] = [
            Column("pid", "PID", lambda p: p.pid, width=70, mono=True),
            Column("name", "Process", lambda p: p.name, width=0),
            Column(
                "rss", "Resident", lambda p: p.memory_rss_bytes,
                display=bytes_, width=100, mono=True,
            ),
            Column(
                "share", "Share", lambda p: p.memory_percent,
                display=lambda v: percent(v), width=80, mono=True,
            ),
            Column(
                "vms", "Virtual", lambda p: p.memory_vms_bytes,
                display=bytes_, width=100, mono=True,
            ),
        ]
        self._top_table = DataTable(columns, theme=self.theme, stretch_column=1)
        self._top_table.setMaximumHeight(int(self.metrics.row_height * 9))
        self._top_table.setSortingEnabled(False)
        card.add(self._top_table)
        self.add(card)

    # ------------------------------------------------------------------- updates

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a memory or process snapshot."""
        if domain == "memory" and isinstance(snapshot, MemorySnapshot):
            self._apply(snapshot)
        elif (
            domain == "processes"
            and isinstance(snapshot, ProcessSnapshot)
            and self.is_visible_page
        ):
            self._top_table.set_rows(snapshot.top_memory(8))

    def _apply(self, snapshot: MemorySnapshot) -> None:
        """Refresh every widget from a memory snapshot."""
        history = self.context.history
        for metric, chart in (
            ("memory.percent", self._usage_chart),
            ("memory.swap_percent", self._usage_chart),
            ("memory.pressure", self._pressure_chart),
        ):
            chart.update_series(metric, history.series(metric))
        if not self.is_visible_page:
            return

        palette = self.palette_tokens
        self._ram_ring.set_value(snapshot.percent)
        self._swap_ring.set_value(
            snapshot.swap_percent if snapshot.swap_total_bytes else None
        )
        self._stats["total"].set_value(bytes_(snapshot.total_bytes))
        self._stats["available"].set_value(bytes_(snapshot.available_bytes))
        self._stats["cache"].set_value(
            bytes_(snapshot.cached_bytes + snapshot.buffers_bytes)
        )
        self._stats["shared"].set_value(bytes_(snapshot.shared_bytes))
        self._stats["swap"].set_value(
            f"{bytes_(snapshot.swap_used_bytes)} / {bytes_(snapshot.swap_total_bytes)}"
            if snapshot.swap_total_bytes else "Not configured"
        )
        pressure = snapshot.pressure_memory
        stall = pressure if isinstance(pressure, PressureStall) else None
        self._stats["pressure"].set_value(
            percent(stall.avg10) if stall else "—",
            colour=palette.danger if stall and stall.avg10 >= 10 else None,
        )

        self._composition.set_segments([
            ("Applications", snapshot.composition[0][1], palette.series[3]),
            ("Cache", snapshot.composition[1][1], palette.series[2]),
            ("Buffers", snapshot.composition[2][1], palette.series[1]),
            ("Free", snapshot.composition[3][1], palette.surface_sunken),
        ] if len(snapshot.composition) >= 4 else [])

        self._kernel.update_many({
            "total": bytes_(snapshot.total_bytes),
            "free": bytes_(snapshot.free_bytes),
            "available": bytes_(snapshot.available_bytes),
            "used": f"{bytes_(snapshot.used_bytes)} ({percent(snapshot.percent)})",
            "cached": bytes_(snapshot.cached_bytes),
            "buffers": bytes_(snapshot.buffers_bytes),
            "shared": bytes_(snapshot.shared_bytes),
            "anonymous": bytes_(snapshot.anonymous_bytes),
            "mapped": bytes_(snapshot.mapped_bytes),
            "slab": bytes_(snapshot.slab_bytes),
            "slab_reclaim": bytes_(snapshot.slab_reclaimable_bytes),
            "page_tables": bytes_(snapshot.page_tables_bytes),
            "kernel_stack": bytes_(snapshot.kernel_stack_bytes),
            "dirty": bytes_(snapshot.dirty_bytes),
            "writeback": bytes_(snapshot.writeback_bytes),
            "committed": bytes_(snapshot.committed_bytes),
            "commit_limit": bytes_(snapshot.commit_limit_bytes),
            "thp": snapshot.transparent_hugepages,
        })

        self._update_swap(snapshot)
        self._update_pressure(snapshot)
        self._update_zram(snapshot)
        self._update_hugepages(snapshot)

    def _update_swap(self, snapshot: MemorySnapshot) -> None:
        """Refresh the per-area swap bars."""
        self._swap_empty.setVisible(not snapshot.swap_devices)
        seen: set[str] = set()
        for device in snapshot.swap_devices:
            seen.add(device.path)
            bar = self._swap_bars.get(device.path)
            if bar is None:
                bar = BarMeter(theme=self.theme, label=device.path)
                self._swap_bars[device.path] = bar
                # Insert above the divider and pressure table.
                self._swap_body.insertWidget(1, bar)
            bar.setVisible(True)
            bar.set_label(f"{device.path}  ({device.kind}, priority {device.priority})")
            bar.set_value(
                device.percent,
                f"{bytes_(device.used_bytes)} / {bytes_(device.size_bytes)}",
            )
        for path, bar in self._swap_bars.items():
            if path not in seen:
                bar.setVisible(False)

    def _update_pressure(self, snapshot: MemorySnapshot) -> None:
        """Refresh the PSI table."""
        def stalls(value: object, attribute: str) -> object:
            """Format one PSI average, preserving an absent reading."""
            if not isinstance(value, PressureStall):
                return value
            return percent(getattr(value, attribute))

        self._pressure_table.update_many({
            "mem10": stalls(snapshot.pressure_memory, "avg10"),
            "mem60": stalls(snapshot.pressure_memory, "avg60"),
            "mem300": stalls(snapshot.pressure_memory, "avg300"),
            "io10": stalls(snapshot.pressure_io, "avg10"),
            "cpu10": stalls(snapshot.pressure_cpu, "avg10"),
        })

    def _update_zram(self, snapshot: MemorySnapshot) -> None:
        """Refresh the zram summary."""
        devices = snapshot.zram_devices
        self._zram_empty.setVisible(not devices)
        if not devices:
            return
        if not self._zram_built:
            for device in devices:
                prefix = device.name
                for suffix, label in (
                    ("algorithm", "Algorithm"), ("disksize", "Configured size"),
                    ("original", "Uncompressed data"),
                    ("compressed", "Compressed size"),
                    ("total", "Physical RAM used"),
                    ("ratio", "Compression ratio"), ("saved", "RAM saved"),
                ):
                    self._zram_table.add_row(
                        f"{prefix}.{suffix}", f"{prefix} — {label}"
                    )
            self._zram_built = True
        for device in devices:
            prefix = device.name
            self._zram_table.update_many({
                f"{prefix}.algorithm": device.algorithm,
                f"{prefix}.disksize": bytes_(device.disk_size_bytes),
                f"{prefix}.original": bytes_(device.original_bytes),
                f"{prefix}.compressed": bytes_(device.compressed_bytes),
                f"{prefix}.total": bytes_(device.total_memory_bytes),
                f"{prefix}.ratio": (
                    f"{device.compression_ratio:.2f}x"
                    if device.compression_ratio else "—"
                ),
                f"{prefix}.saved": bytes_(device.saved_bytes),
            })

    def _update_hugepages(self, snapshot: MemorySnapshot) -> None:
        """Refresh the huge page pools."""
        pools = [p for p in snapshot.hugepages if p.total or p.free]
        self._huge_empty.setVisible(not pools)
        if not pools:
            return
        if not self._huge_built:
            for pool in pools:
                size = f"{pool.size_kb // 1024} MiB" if pool.size_kb >= 1024 \
                    else f"{pool.size_kb} KiB"
                self._huge_table.add_row(f"{pool.size_kb}", f"{size} pages")
            self._huge_built = True
        for pool in pools:
            self._huge_table.set(
                f"{pool.size_kb}",
                f"{pool.used} used of {pool.total} "
                f"({bytes_(pool.total_bytes)} reserved)",
            )

    def on_shown(self) -> None:
        """Refresh immediately on entry."""
        super().on_shown()
        self.refresh()
