"""Log viewer: journal queries with filtering, search, bookmarks and export."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
)

from app.core.paths import export_dir
from app.core.units import text, timestamp
from app.models.service import LogEntry, LogPriority
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import Badge, EmptyState, KeyValueTable
from app.ui.widgets.table import Column, DataTable

#: Time windows offered in the filter bar, as systemd time expressions.
_WINDOWS = (
    ("Last 15 minutes", "-15min"),
    ("Last hour", "-1h"),
    ("Last 6 hours", "-6h"),
    ("Last 24 hours", "-1d"),
    ("Last 7 days", "-7d"),
    ("This boot", None),
)

_PRIORITIES = (
    ("All messages", None),
    ("Notice and above", LogPriority.NOTICE),
    ("Warnings and above", LogPriority.WARNING),
    ("Errors and above", LogPriority.ERROR),
    ("Critical only", LogPriority.CRITICAL),
)


class LogsPage(Page):
    """A journal viewer.

    Queries run on a worker thread: ``journalctl`` on a machine with months of
    logs can take seconds, and blocking the GUI for that would be unacceptable.
    """

    domain = ""
    title = "Logs"
    icon = "▤"
    section = "System"
    description = "systemd journal and kernel messages"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("logs")
        self._entries: list[LogEntry] = []
        self._bookmarks: list[LogEntry] = []
        self._loading = False

        card = Card("Journal", theme=self.theme, accent=accent)
        self._status_badge = Badge("Ready", theme=self.theme)
        card.add_action(self._status_badge)

        controls = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Search message text…")
        self._search.returnPressed.connect(self.reload)
        controls.addWidget(self._search, 1)

        self._window_box = QComboBox()
        self._window_box.addItems([label for label, _ in _WINDOWS])
        self._window_box.setCurrentIndex(1)
        controls.addWidget(self._window_box)

        self._priority_box = QComboBox()
        self._priority_box.addItems([label for label, _ in _PRIORITIES])
        self._priority_box.setCurrentIndex(2)
        controls.addWidget(self._priority_box)

        self._unit_filter = QLineEdit()
        self._unit_filter.setPlaceholderText("Unit (optional)")
        self._unit_filter.setMaximumWidth(210)
        self._unit_filter.returnPressed.connect(self.reload)
        controls.addWidget(self._unit_filter)

        self._kernel_only = QPushButton("Kernel only")
        self._kernel_only.setCheckable(True)
        controls.addWidget(self._kernel_only)

        reload_button = QPushButton("Load")
        reload_button.setProperty("variant", "primary")
        reload_button.clicked.connect(self.reload)
        controls.addWidget(reload_button)

        export_button = QPushButton("Export…")
        export_button.clicked.connect(self._export)
        controls.addWidget(export_button)
        card.add_layout(controls)

        for box in (self._window_box, self._priority_box):
            box.currentIndexChanged.connect(self.reload)
        self._kernel_only.toggled.connect(self.reload)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setChildrenCollapsible(False)

        palette = self.palette_tokens
        priority_colours = {
            LogPriority.EMERGENCY: palette.danger,
            LogPriority.ALERT: palette.danger,
            LogPriority.CRITICAL: palette.danger,
            LogPriority.ERROR: palette.danger,
            LogPriority.WARNING: palette.warning,
            LogPriority.NOTICE: palette.info,
        }
        columns: list[Column] = [
            Column("time", "Time", lambda e: e.timestamp,
                   display=lambda v: timestamp(v), width=160, mono=True),
            Column("priority", "Level", lambda e: int(e.priority),
                   display=lambda v: LogPriority(v).label, width=90,
                   colour=lambda e: priority_colours.get(e.priority)),
            Column("identifier", "Source", lambda e: text(e.identifier),
                   width=160),
            Column("unit", "Unit", lambda e: text(e.unit), width=190, mono=True),
            Column("message", "Message", lambda e: e.message, width=0,
                   tooltip=lambda e: e.message[:1000]),
        ]
        self._table = DataTable(columns, theme=self.theme, stretch_column=4)
        self._table.setSortingEnabled(True)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        self._search.textChanged.connect(self._table.proxy.set_search)
        splitter.addWidget(self._table)

        self._detail_card = Card("Entry detail", theme=self.theme, accent=accent)
        self._detail = KeyValueTable(theme=self.theme, label_width=120)
        for key, label in (
            ("time", "Timestamp"), ("priority", "Priority"),
            ("unit", "Unit"), ("identifier", "Source"),
            ("pid", "Process ID"), ("host", "Hostname"),
        ):
            self._detail.add_row(key, label)
        self._detail_card.add(self._detail)
        from PySide6.QtWidgets import QPlainTextEdit

        self._message = QPlainTextEdit()
        self._message.setReadOnly(True)
        self._message.setMaximumHeight(120)
        self._detail_card.add(self._message)
        splitter.addWidget(self._detail_card)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        card.add(splitter)
        self._empty = EmptyState(
            "No entries loaded",
            "Choose a time window and press Load.",
            theme=self.theme, icon="▤",
        )
        card.add(self._empty)
        self.content_layout().addWidget(card, 1)

    # ------------------------------------------------------------------ loading

    def reload(self) -> None:
        """Run a journal query on a worker thread."""
        reader = _reader(self.context)
        if not reader.available:
            self._empty.set_message(
                "The journal is not available", str(reader.unavailable_reason())
            )
            self._empty.setVisible(True)
            self._status_badge.set_state(
                "Unavailable", self.palette_tokens.text_subtle
            )
            return
        if self._loading:
            return
        self._loading = True
        self._status_badge.set_state("Loading…", self.palette_tokens.info)

        _label, since = _WINDOWS[self._window_box.currentIndex()]
        _plabel, priority = _PRIORITIES[self._priority_box.currentIndex()]
        unit = self._unit_filter.text().strip() or None

        task = _JournalTask(
            reader,
            lines=2000,
            priority=priority,
            unit=unit,
            since=since,
            kernel_only=self._kernel_only.isChecked(),
        )
        task.signals.finished.connect(self._on_loaded)
        QThreadPool.globalInstance().start(task)

    def _on_loaded(self, entries: list, note: str) -> None:
        """Display query results."""
        self._loading = False
        self._entries = entries
        self._table.set_rows(entries)
        self._empty.setVisible(not entries)
        if not entries:
            self._empty.set_message("No entries matched", note or "Try a wider window.")
        palette = self.palette_tokens
        problems = sum(1 for entry in entries if entry.is_problem)
        self._status_badge.set_state(
            f"{len(entries):,} entries · {problems} warnings or worse",
            palette.warning if problems else palette.success,
        )
        self.status_message.emit(f"Loaded {len(entries):,} journal entries")

    def _on_selection(self) -> None:
        """Show the selected entry's metadata."""
        entry = self._table.selected_object()
        if entry is None:
            return
        self._detail.update_many({
            "time": timestamp(entry.timestamp),
            "priority": entry.priority.label,
            "unit": entry.unit,
            "identifier": entry.identifier,
            "pid": entry.pid,
            "host": entry.hostname,
        })
        self._message.setPlainText(entry.message)

    # ------------------------------------------------------------------ actions

    def _show_context_menu(self, position) -> None:
        """Context menu for a log entry."""
        entry = self._table.selected_object()
        if entry is None:
            return
        menu = QMenu(self)
        copy = QAction("Copy message", self)
        copy.triggered.connect(lambda: self._copy(entry.message))
        menu.addAction(copy)

        bookmark = QAction("Bookmark this entry", self)
        bookmark.triggered.connect(lambda: self._bookmark(entry))
        menu.addAction(bookmark)

        if isinstance(entry.unit, str):
            filter_unit = QAction(f"Filter to {entry.unit}", self)
            filter_unit.triggered.connect(
                lambda: self.filter_to_unit(str(entry.unit))
            )
            menu.addAction(filter_unit)
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _copy(self, value: str) -> None:
        """Copy text to the clipboard."""
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(value)
        self.status_message.emit("Copied to clipboard")

    def _bookmark(self, entry: LogEntry) -> None:
        """Remember an entry for the session."""
        self._bookmarks.append(entry)
        self.status_message.emit(
            f"Bookmarked ({len(self._bookmarks)} this session)"
        )

    def _export(self) -> None:
        """Write the currently loaded entries to a text file."""
        if not self._entries:
            self.status_message.emit("Nothing to export — load entries first")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export journal entries",
            str(export_dir() / "journal-export.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                for entry in self._entries:
                    handle.write(
                        f"{timestamp(entry.timestamp)} "
                        f"[{entry.priority.label:8s}] "
                        f"{text(entry.identifier)}: {entry.message}\n"
                    )
        except OSError as exc:
            self.status_message.emit(f"Export failed: {exc}")
            return
        self.status_message.emit(f"Exported {len(self._entries):,} entries")

    def filter_to_unit(self, unit: str) -> None:
        """Load entries for one unit; called from the services page."""
        self._unit_filter.setText(unit)
        self._priority_box.setCurrentIndex(0)
        self.reload()

    def on_shown(self) -> None:
        """Load on first entry, so the page is never blank."""
        super().on_shown()
        if not self._entries and not self._loading:
            self.reload()


def _reader(context):
    """Build a journal reader bound to the application's capabilities."""
    from app.collectors.logs import JournalReader

    if not hasattr(context, "_journal_reader"):
        object.__setattr__(
            context, "_journal_reader", JournalReader(context.capabilities)
        )
    return context._journal_reader


class _JournalSignals(QObject):
    """Signals for :class:`_JournalTask`."""

    finished = Signal(list, str)


class _JournalTask(QRunnable):
    """Runs one journal query off the GUI thread."""

    def __init__(self, reader, **query) -> None:
        super().__init__()
        self._reader = reader
        self._query = query
        self.signals = _JournalSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        """Execute the query and emit the results."""
        try:
            entries, note = self._reader.query(**self._query)
        except Exception as exc:
            entries, note = [], f"Journal query failed: {exc}"
        self.signals.finished.emit(entries, note)
