"""Diagnostics page: health checks, findings and exportable reports."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.paths import export_dir
from app.models.diagnostics import CheckResult, DiagnosticReport, Severity
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import Badge, Divider, EmptyState, KeyValueTable, StatRow


class DiagnosticsPage(Page):
    """Runs health checks and presents actionable findings."""

    domain = ""
    title = "Diagnostics"
    icon = "✚"
    section = "Analysis"
    description = "System health checks and recommendations"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("diagnostics")
        self._report: DiagnosticReport | None = None

        card = Card(
            "System health", theme=self.theme,
            subtitle="Checks run against the most recent samples, so this "
                     "costs nothing to open",
            accent=accent,
        )
        self._verdict = Badge("Not yet run", theme=self.theme)
        card.add_action(self._verdict)

        controls = QHBoxLayout()
        run = QPushButton("Run diagnostics")
        run.setProperty("variant", "primary")
        run.clicked.connect(self.run_checks)
        controls.addWidget(run)

        for label, handler in (
            ("Export Markdown…", self._export_markdown),
            ("Export HTML…", self._export_html),
            ("Export JSON…", self._export_json),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            controls.addWidget(button)
        controls.addStretch(1)
        card.add_layout(controls)

        self._summary = StatRow(theme=self.theme, columns=5)
        for key, label in (
            ("checks", "Checks run"), ("healthy", "Healthy"),
            ("notices", "Notices"), ("warnings", "Warnings"),
            ("critical", "Critical"),
        ):
            self._summary.add(key, label, size="small")
        card.add(self._summary)
        self.add(card)

        self._findings_host = QWidget(self)
        self._findings_layout = QVBoxLayout(self._findings_host)
        self._findings_layout.setContentsMargins(0, 0, 0, 0)
        self._findings_layout.setSpacing(self.metrics.space_3)
        self.add(self._findings_host)

        self._empty = EmptyState(
            "Diagnostics have not run yet",
            "Press “Run diagnostics” to check processor load, memory pressure, "
            "thermals, disk capacity, drive health, services and connectivity.",
            theme=self.theme, icon="✚",
        )
        self.add(self._empty)

        self._collector_card = Card(
            "Monitoring performance", theme=self.theme,
            subtitle="What this application itself costs to run", accent=accent,
        )
        self._collector_table = KeyValueTable(theme=self.theme, label_width=170)
        self._collector_card.add(self._collector_table)
        self._collector_rows: set[str] = set()
        self.add(self._collector_card)
        self.add_stretch()

    # --------------------------------------------------------------------- run

    def run_checks(self) -> None:
        """Execute every diagnostic check and render the report."""
        service = self.context.diagnostics
        report = service.run()
        self._report = report
        self._render(report)
        self.status_message.emit(
            f"Diagnostics complete: {report.worst.label} "
            f"({len(report.problems)} findings)"
        )

    def _render(self, report: DiagnosticReport) -> None:
        """Rebuild the findings list from a report."""
        palette = self.palette_tokens
        self._empty.setVisible(False)
        self._verdict.set_state(
            report.worst.label, palette.status(int(report.worst))
        )
        counts = report.counts
        self._summary["checks"].set_value(str(len(report.results)))
        self._summary["healthy"].set_value(str(counts[Severity.OK]))
        self._summary["notices"].set_value(str(counts[Severity.INFO]))
        self._summary["warnings"].set_value(
            str(counts[Severity.WARNING]),
            colour=palette.warning if counts[Severity.WARNING] else None,
        )
        self._summary["critical"].set_value(
            str(counts[Severity.CRITICAL]),
            colour=palette.danger if counts[Severity.CRITICAL] else None,
        )

        while self._findings_layout.count():
            # takeAt returns None once the layout is empty; count() guards that
            # here, but the null check keeps the teardown safe if the layout is
            # mutated concurrently.
            item = self._findings_layout.takeAt(0)
            if item is None:
                break
            if (widget := item.widget()) is not None:
                widget.deleteLater()

        problems = report.problems
        skipped = [r for r in report.results if r.skipped]
        passed = [r for r in report.results if r.passed]

        if problems:
            self._add_group("Findings", problems)
        else:
            healthy = Card("No problems found", theme=self.theme,
                           accent=palette.success)
            healthy.add(QLabel(
                f"All {len(passed)} applicable checks passed. "
                f"{len(skipped)} checks could not run on this system."
            ))
            self._findings_layout.addWidget(healthy)
        if passed:
            self._add_group("Checks that passed", passed, collapsed=True)
        if skipped:
            self._add_group("Checks that could not run", skipped, collapsed=True)

    def _add_group(
        self, heading: str, results: list[CheckResult], collapsed: bool = False
    ) -> None:
        """Add a titled group of finding cards."""
        label = QLabel(heading)
        label.setStyleSheet(
            f"color: {self.palette_tokens.text_muted}; "
            f"font-size: {self.metrics.font_caption}pt; font-weight: 700; "
            "text-transform: uppercase; letter-spacing: 0.5px;"
        )
        self._findings_layout.addWidget(label)
        for result in results:
            self._findings_layout.addWidget(
                self._finding_card(result, compact=collapsed)
            )

    def _finding_card(self, result: CheckResult, compact: bool) -> Card:
        """Build one finding card."""
        palette = self.palette_tokens
        colour = (
            palette.text_subtle if result.skipped
            else palette.status(int(result.severity))
        )
        card = Card(
            result.title, theme=self.theme,
            subtitle=result.category, accent=colour,
        )
        badge = Badge(
            "Skipped" if result.skipped else result.severity.label,
            theme=self.theme, colour=colour,
        )
        card.add_action(badge)

        summary = QLabel(result.summary)
        summary.setWordWrap(True)
        card.add(summary)

        if result.advice and not compact:
            advice = QLabel(f"→  {result.advice}")
            advice.setWordWrap(True)
            advice.setStyleSheet(
                f"color: {palette.text_muted}; "
                f"background: {palette.surface_sunken}; "
                f"border-left: 3px solid {colour}; "
                f"border-radius: {self.metrics.radius_sm}px; "
                f"padding: {self.metrics.space_3}px;"
            )
            card.add(advice)

        if result.evidence and not compact:
            table = KeyValueTable(theme=self.theme, columns=2, label_width=170)
            for key, value in result.evidence.items():
                table.add_row(key, key, value)
            card.add(Divider())
            card.add(table)
        return card

    # ------------------------------------------------------------------ exports

    def _snapshots(self) -> dict[str, object]:
        """The snapshot set the diagnostics service last saw."""
        store = self.context.diagnostics.store
        return {
            domain: store.get(domain)
            for domain in (
                "cpu", "memory", "gpu", "storage", "network",
                "processes", "sensors", "battery", "system", "services",
            )
            if store.get(domain) is not None
        }

    def _export_markdown(self) -> None:
        """Write a Markdown report."""
        self._export("Markdown report", "system-report.md",
                     "Markdown (*.md)", "report_to_markdown")

    def _export_html(self) -> None:
        """Write an HTML report."""
        self._export("HTML report", "system-report.html",
                     "HTML (*.html)", "report_to_html")

    def _export_json(self) -> None:
        """Write a JSON snapshot document."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Export JSON snapshot",
            str(export_dir() / "system-snapshot.json"), "JSON (*.json)",
        )
        if not path:
            return
        from pathlib import Path

        _ok, message = self.context.export.snapshots_to_json(
            Path(path), self._snapshots()
        )
        self.status_message.emit(message)

    def _export(self, title: str, default: str, filters: str, method: str) -> None:
        """Shared export flow for the document formats."""
        if self._report is None:
            self.run_checks()
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {title}", str(export_dir() / default), filters
        )
        if not path:
            return
        from pathlib import Path

        exporter = getattr(self.context.export, method)
        _ok, message = exporter(Path(path), self._snapshots(), self._report)
        self.status_message.emit(message)

    # ------------------------------------------------------------------ updates

    def on_shown(self) -> None:
        """Run checks on first entry and refresh collector statistics."""
        super().on_shown()
        if self._report is None:
            self.run_checks()
        self._refresh_collector_stats()

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Keep the collector performance table current."""
        if self.is_visible_page:
            self._refresh_collector_stats()

    def _refresh_collector_stats(self) -> None:
        """Show what each collector costs.

        Holding the application to its own standard: if monitoring is expensive,
        the user should be able to see exactly which collector is responsible.
        """
        monitor = self.context.monitor
        for entry in monitor.statistics():
            domain = str(entry["domain"])
            if domain not in self._collector_rows:
                self._collector_table.add_row(domain, str(entry["title"]))
                self._collector_rows.add(domain)
            state = "enabled" if entry["enabled"] else "suspended"
            self._collector_table.set(
                domain,
                f"{entry['average_ms']:.1f} ms every {entry['interval']:.0f} s "
                f"· {entry['samples']} samples · {state}"
                + (f" · {entry['overruns']} overruns" if entry["overruns"] else "")
                + ("" if entry["healthy"] else " · FAILING"),
            )
        total = monitor.total_cost_ms_per_second
        if "total" not in self._collector_rows:
            self._collector_table.add_row("total", "Total monitoring cost")
            self._collector_rows.add("total")
        self._collector_table.set(
            "total",
            f"{total:.0f} ms of CPU per second ({total / 10:.1f}% of one core)",
        )
