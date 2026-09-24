"""History page: stored metrics, session recording and data export."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from app.core.paths import export_dir
from app.core.units import bytes_, duration, timestamp
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.stat import Badge, EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable

#: Time windows offered for stored-history queries.
_WINDOWS = (("Last hour", 1.0), ("Last 6 hours", 6.0), ("Last 24 hours", 24.0),
            ("Last 3 days", 72.0), ("Last 7 days", 168.0),
            ("Last 14 days", 336.0))


class HistoryPage(Page):
    """Browse recorded metrics and export them."""

    domain = ""
    title = "History"
    icon = "◔"
    section = "Analysis"
    description = "Recorded metrics, sessions and export"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("history")
        self._build_storage(accent)
        self._build_browser(accent)
        self._build_sessions(accent)
        self.add_stretch()

    def _build_storage(self, accent: str) -> None:
        """Database status and export controls."""
        card = Card("Recorded history", theme=self.theme, accent=accent)
        self._status_badge = Badge("", theme=self.theme)
        card.add_action(self._status_badge)

        self._stats = StatRow(theme=self.theme, columns=5)
        for key, label in (
            ("samples", "Stored samples"), ("metrics", "Distinct metrics"),
            ("size", "Database size"), ("oldest", "Oldest sample"),
            ("span", "Coverage"),
        ):
            self._stats.add(key, label, size="small")
        card.add(self._stats)

        controls = QHBoxLayout()
        for label, handler in (
            ("Export CSV…", self._export_csv),
            ("Export live buffers…", self._export_live),
            ("Export JSON snapshot…", self._export_json),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            controls.addWidget(button)
        controls.addStretch(1)
        card.add_layout(controls)

        self._path_label = QLabel()
        self._path_label.setStyleSheet(
            f"color: {self.palette_tokens.text_subtle}; "
            f"font-family: {self.metrics.mono_family};"
        )
        self._path_label.setWordWrap(True)
        card.add(self._path_label)
        self.add(card)

    def _build_browser(self, accent: str) -> None:
        """Metric picker and the stored-history chart."""
        card = Card(
            "Metric browser", theme=self.theme,
            subtitle="Select metrics to plot from the recorded database",
            accent=accent,
        )
        controls = QHBoxLayout()
        self._window_box = QComboBox()
        self._window_box.addItems([label for label, _ in _WINDOWS])
        self._window_box.setCurrentIndex(2)
        self._window_box.currentIndexChanged.connect(self._plot)
        controls.addWidget(QLabel("Window"))
        controls.addWidget(self._window_box)
        controls.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._reload)
        controls.addWidget(refresh)
        card.add_layout(controls)

        body = QHBoxLayout()
        self._metric_list = QListWidget()
        self._metric_list.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection
        )
        self._metric_list.setMaximumWidth(280)
        self._metric_list.itemSelectionChanged.connect(self._plot)
        body.addWidget(self._metric_list)

        chart_side = QVBoxLayout()
        self._chart = LiveChart(
            theme=self.theme, window_seconds=3600, y_range=None,
            y_label="", height=280, fill=False,
        )
        chart_side.addWidget(self._chart)
        self._summary = KeyValueTable(theme=self.theme, columns=2, label_width=170)
        chart_side.addWidget(self._summary)
        body.addLayout(chart_side, 1)
        card.add_layout(body)

        self._browser_empty = EmptyState(
            "No recorded history yet",
            "History recording writes samples to disk every few seconds. "
            "Leave the application running, or enable recording in Settings.",
            theme=self.theme, icon="◔",
        )
        card.add(self._browser_empty)
        self.add(card)

    def _build_sessions(self, accent: str) -> None:
        """Named recording sessions."""
        card = Card(
            "Recording sessions", theme=self.theme,
            subtitle="Mark a period to compare before-and-after behaviour",
            accent=accent,
        )
        controls = QHBoxLayout()
        self._session_button = QPushButton("Start session…")
        self._session_button.setProperty("variant", "primary")
        self._session_button.clicked.connect(self._toggle_session)
        controls.addWidget(self._session_button)
        controls.addStretch(1)
        card.add_layout(controls)

        self._sessions_table = DataTable(
            [
                Column("name", "Session", lambda s: s["name"], width=240),
                Column("started", "Started", lambda s: s["started"],
                       display=lambda v: timestamp(v), width=170, mono=True),
                Column("ended", "Ended", lambda s: s["ended"] or 0,
                       display=lambda v: timestamp(v) if v else "In progress",
                       width=170, mono=True),
                Column("duration", "Duration",
                       lambda s: (s["ended"] or time.time()) - s["started"],
                       display=lambda v: duration(v), width=120, mono=True),
                Column("note", "Note", lambda s: s["note"], width=0),
            ],
            theme=self.theme, stretch_column=4,
        )
        self._sessions_table.setMaximumHeight(int(self.metrics.row_height * 7))
        card.add(self._sessions_table)
        self.add(card)

    # ------------------------------------------------------------------ loading

    def _reload(self) -> None:
        """Refresh storage statistics and the metric list."""
        history = self.context.history
        report = history.storage_report()
        palette = self.palette_tokens

        if report["degraded"]:
            self._status_badge.set_state("Unavailable", palette.danger)
        elif report["persisting"]:
            self._status_badge.set_state("Recording", palette.success)
        else:
            self._status_badge.set_state("Recording disabled", palette.warning)

        self._stats["samples"].set_value(f"{report['samples']:,}")
        self._stats["metrics"].set_value(str(report["metrics"]))
        self._stats["size"].set_value(bytes_(report["size_bytes"]))
        self._stats["oldest"].set_value(
            timestamp(report["oldest"]) if report["oldest"] else "—"
        )
        oldest, newest = report["oldest"], report["newest"]
        self._stats["span"].set_value(
            duration(newest - oldest) if oldest and newest else "—"
        )
        self._path_label.setText(f"Database: {report['path']}")

        selected = {
            item.text() for item in self._metric_list.selectedItems()
        }
        self._metric_list.clear()
        registry = self.context.metrics
        names = sorted(
            set(history.tracked_metrics())
            | {d.name for d in registry.all()}
        )
        for name in names:
            item = QListWidgetItem(name)
            definition = registry.get(name)
            if definition is not None:
                item.setToolTip(f"{definition.label} ({definition.unit})")
            self._metric_list.addItem(item)
            if name in selected:
                item.setSelected(True)
        if not selected and names:
            # Default to CPU usage so the chart is never blank on first visit.
            matches = self._metric_list.findItems(
                "cpu.usage", Qt.MatchFlag.MatchExactly
            )
            if matches:
                matches[0].setSelected(True)

        self._sessions_table.set_rows(history.sessions())
        self._session_button.setText(
            "Stop session" if history.recording
            else "Start session…"
        )
        self._browser_empty.setVisible(report["samples"] == 0)
        self._plot()

    def _plot(self) -> None:
        """Draw the selected metrics over the chosen window."""
        history = self.context.history
        hours = _WINDOWS[self._window_box.currentIndex()][1]
        selected = [item.text() for item in self._metric_list.selectedItems()]
        self._chart.clear_series()
        palette = self.palette_tokens
        now = time.time()
        plotted = 0

        for index, metric in enumerate(selected[:6]):
            points = history.stored_series(metric, hours=hours)
            if not points:
                # Fall back to the in-memory buffer so a metric still charts
                # when persistence is off.
                times, values = history.series(metric).snapshot()
                if not times:
                    continue
                self._chart.set_data(metric, times, values)
            else:
                # Stored timestamps are absolute; the chart's axis is relative.
                times = [point[0] - now for point in points]
                values = [point[1] for point in points]
                self._chart.set_data(metric, times, values)
            self._chart.set_series_colour(
                metric, palette.series[index % len(palette.series)]
            )
            plotted += 1

        self._chart.set_window(int(hours * 3600))
        self._update_summary(selected[:6], hours)
        self._browser_empty.setVisible(plotted == 0)

    def _update_summary(self, metrics: list[str], hours: float) -> None:
        """Show min/mean/max for each plotted metric."""
        history = self.context.history
        for metric in metrics:
            stats = history.stored_summary(metric, hours=hours)
            if not stats:
                series = history.series(metric)
                live = series.stats()
                if live is None:
                    continue
                stats = {
                    "minimum": live.minimum, "maximum": live.maximum,
                    "mean": live.mean, "samples": live.samples,
                }
            if metric not in getattr(self, "_summary_rows", set()):
                self._summary.add_row(metric, metric)
                self._summary_rows = getattr(self, "_summary_rows", set()) | {metric}
            self._summary.set(
                metric,
                f"min {stats['minimum']:.1f} · mean {stats['mean']:.1f} · "
                f"max {stats['maximum']:.1f} · {int(stats['samples']):,} samples",
            )

    # ------------------------------------------------------------------ actions

    def _toggle_session(self) -> None:
        """Start or stop a named recording session."""
        history = self.context.history
        if history.recording:
            history.stop_session()
            self.status_message.emit("Recording session ended")
        else:
            name, accepted = QInputDialog.getText(
                self, "Start recording session",
                "Name this session (for example “before tuning”):",
            )
            if not accepted or not name.strip():
                return
            if not history.start_session(name.strip()):
                self.status_message.emit(
                    "Could not start a session — history recording is disabled"
                )
                return
            self.status_message.emit(f"Recording session “{name}” started")
        self._reload()

    def _export_csv(self) -> None:
        """Export stored history for the selected metrics."""
        selected = [item.text() for item in self._metric_list.selectedItems()]
        if not selected:
            self.status_message.emit("Select one or more metrics to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export metric history",
            str(export_dir() / "metrics.csv"), "CSV files (*.csv)",
        )
        if not path:
            return
        from pathlib import Path

        hours = _WINDOWS[self._window_box.currentIndex()][1]
        _ok, message = self.context.export.metrics_to_csv(
            Path(path), selected, hours=hours
        )
        self.status_message.emit(message)

    def _export_live(self) -> None:
        """Export the in-memory chart buffers."""
        selected = [item.text() for item in self._metric_list.selectedItems()]
        if not selected:
            self.status_message.emit("Select one or more metrics to export")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export live buffers",
            str(export_dir() / "live-metrics.csv"), "CSV files (*.csv)",
        )
        if not path:
            return
        from pathlib import Path

        _ok, message = self.context.export.live_series_to_csv(
            Path(path), selected
        )
        self.status_message.emit(message)

    def _export_json(self) -> None:
        """Export the current snapshot set as JSON."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export JSON snapshot",
            str(export_dir() / "system-snapshot.json"), "JSON (*.json)",
        )
        if not path:
            return
        from pathlib import Path

        store = self.context.diagnostics.store
        snapshots = {
            domain: store.get(domain)
            for domain in ("cpu", "memory", "gpu", "storage", "network",
                           "processes", "sensors", "battery", "system", "services")
            if store.get(domain) is not None
        }
        _ok, message = self.context.export.snapshots_to_json(
            Path(path), snapshots
        )
        self.status_message.emit(message)

    def on_shown(self) -> None:
        """Reload on entry."""
        super().on_shown()
        self._reload()
