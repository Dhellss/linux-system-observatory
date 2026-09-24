"""Services page: systemd units, boot timing and unit control."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.collectors.services import ServiceCollector
from app.core.units import bytes_, text, timestamp
from app.models.base import is_number
from app.models.service import ServiceSnapshot, UnitState
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.gauge import CompositionBar
from app.ui.widgets.stat import Badge, Divider, EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable

#: Unit-state filters offered in the toolbar.
_FILTERS = (
    ("All units", lambda u: True),
    ("Active", lambda u: u.state is UnitState.ACTIVE),
    ("Failed", lambda u: u.state is UnitState.FAILED),
    ("Inactive", lambda u: u.state is UnitState.INACTIVE),
    ("Enabled", lambda u: str(u.enablement) == "enabled"),
    ("Disabled", lambda u: str(u.enablement) == "disabled"),
    ("Services only", lambda u: u.unit_type == "service"),
    ("Timers", lambda u: u.unit_type == "timer"),
)


class ServicesPage(Page):
    """systemd unit management."""

    domain = "services"
    title = "Services"
    icon = "⚙"
    section = "System"
    description = "systemd units, dependencies and boot timing"
    # systemd enumeration costs ~650 ms, so it is suspended while the page is
    # closed.  The dashboard's services widget re-enables it on demand.
    suspend_when_hidden = True

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("services")
        self._snapshot: ServiceSnapshot | None = None
        self._build_summary(accent)
        self._build_body(accent)

    def _build_summary(self, accent: str) -> None:
        """Unit counts, boot timing and the filter toolbar."""
        card = Card("systemd", theme=self.theme, accent=accent)
        self._unavailable = EmptyState(
            "systemd is not available", "", theme=self.theme, icon="⊘"
        )
        self._unavailable.setVisible(False)
        card.add(self._unavailable)

        self._stats = StatRow(theme=self.theme, columns=5)
        for key, label in (
            ("loaded", "Loaded units"), ("active", "Active"),
            ("failed", "Failed"), ("boot", "Boot time"),
            ("userspace", "Userspace"),
        ):
            self._stats.add(key, label, size="small")
        card.add(self._stats)

        card.add(Divider())
        self._boot_bar = CompositionBar(theme=self.theme, height=20)
        card.add(self._boot_bar)

        controls = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Filter units by name or description…")
        controls.addWidget(self._search, 1)
        self._filter_box = QComboBox()
        self._filter_box.addItems([name for name, _ in _FILTERS])
        self._filter_box.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(self._filter_box)
        card.add_layout(controls)
        self.add(card)

    def _build_body(self, accent: str) -> None:
        """Unit table alongside the detail panel."""
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        palette = self.palette_tokens
        state_colours = {
            UnitState.ACTIVE: palette.success,
            UnitState.FAILED: palette.danger,
            UnitState.ACTIVATING: palette.info,
            UnitState.DEACTIVATING: palette.warning,
        }
        columns: list[Column] = [
            Column("name", "Unit", lambda u: u.name, width=270, mono=True),
            Column("state", "State", lambda u: u.state.value, width=100,
                   colour=lambda u: state_colours.get(u.state)),
            Column("sub", "Sub-state", lambda u: u.sub_state, width=100),
            Column("enabled", "Startup", lambda u: text(u.enablement), width=100),
            Column("pid", "Main PID",
                   lambda u: u.main_pid if isinstance(u.main_pid, int) else 0,
                   display=lambda v: str(v) if v else "—", width=86, mono=True),
            Column("memory", "Memory",
                   lambda u: u.memory_bytes if isinstance(u.memory_bytes, int) else -1,
                   display=lambda v: bytes_(v) if v >= 0 else "—",
                   width=92, mono=True),
            Column("tasks", "Tasks",
                   lambda u: u.tasks if isinstance(u.tasks, int) else -1,
                   display=lambda v: str(v) if v >= 0 else "—",
                   width=70, mono=True),
            Column("description", "Description", lambda u: u.description, width=0),
        ]
        self._table = DataTable(columns, theme=self.theme, stretch_column=7)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.selectionModel().selectionChanged.connect(self._on_selection)
        self._search.textChanged.connect(self._table.proxy.set_search)
        splitter.addWidget(self._table)

        self._detail = _UnitDetail(self)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([740, 440])
        self.content_layout().addWidget(splitter, 1)

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a systemd snapshot."""
        if not isinstance(snapshot, ServiceSnapshot):
            return
        self._snapshot = snapshot
        if not self.is_visible_page:
            return

        if snapshot.note and not snapshot.units:
            self._unavailable.set_message("systemd is not available", snapshot.note)
            self._unavailable.setVisible(True)
            return
        self._unavailable.setVisible(False)

        palette = self.palette_tokens
        failed = snapshot.failed
        self._stats["loaded"].set_value(str(len(snapshot.units)))
        self._stats["active"].set_value(str(snapshot.active_count))
        self._stats["failed"].set_value(
            str(len(failed)),
            colour=palette.danger if failed else palette.success,
        )
        boot = snapshot.boot
        self._stats["boot"].set_value(
            f"{boot.total_s:.1f} s" if is_number(boot.total_s) else "—"
        )
        self._stats["userspace"].set_value(
            f"{boot.userspace_s:.1f} s"
            if is_number(boot.userspace_s) else "—"
        )
        self._boot_bar.set_segments([
            (label, value, colour)
            for label, value, colour in (
                ("Firmware", boot.firmware_s, palette.series[5]),
                ("Boot loader", boot.loader_s, palette.series[1]),
                ("Kernel", boot.kernel_s, palette.series[0]),
                ("initrd", boot.initrd_s, palette.series[3]),
                ("Userspace", boot.userspace_s, palette.series[2]),
            )
            if is_number(value) and value > 0
        ])
        self._table.set_rows(snapshot.units)
        self._detail.refresh()

    def _apply_filter(self) -> None:
        """Apply the selected unit-state filter."""
        self._table.proxy.set_predicate(_FILTERS[self._filter_box.currentIndex()][1])

    def _on_selection(self) -> None:
        """Show details for the selected unit."""
        unit = self._table.selected_object()
        self._detail.set_unit(unit)

    # ------------------------------------------------------------------ actions

    def _show_context_menu(self, position) -> None:
        """Build the unit action menu."""
        unit = self._table.selected_object()
        if unit is None:
            return
        menu = QMenu(self)
        menu.addAction(unit.name).setEnabled(False)
        menu.addSeparator()
        for action, label in (
            ("start", "Start"), ("stop", "Stop"), ("restart", "Restart"),
            ("reload", "Reload"),
        ):
            entry = QAction(label, self)
            entry.triggered.connect(
                lambda _checked=False, a=action, u=unit: self._confirm(u, a)
            )
            menu.addAction(entry)
        menu.addSeparator()
        for action, label in (("enable", "Enable at boot"),
                              ("disable", "Disable at boot")):
            entry = QAction(label, self)
            entry.triggered.connect(
                lambda _checked=False, a=action, u=unit: self._confirm(u, a)
            )
            menu.addAction(entry)
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _confirm(self, unit, action: str) -> None:
        """Confirm a privileged unit action before performing it.

        Unit actions change system state and usually need administrator rights,
        so the user is always told exactly what will happen and that an
        authentication prompt may follow.
        """
        descriptions = {
            "start": "start", "stop": "stop", "restart": "restart",
            "reload": "reload the configuration of",
            "enable": "enable at boot", "disable": "disable at boot",
        }
        root = self.context.capabilities.root
        box = QMessageBox(self)
        box.setWindowTitle(f"{action.title()} unit?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"{action.title()} <b>{unit.name}</b>?"
        )
        box.setInformativeText(
            f"This will {descriptions.get(action, action)} the unit.\n"
            f"{unit.description}\n\n"
            + ("Running as root; the action will be applied immediately."
               if root else
               "Your desktop will ask for administrator authentication.")
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        collector = self.context.monitor.collector_of("services", ServiceCollector)
        if collector is None:
            return
        self.status_message.emit(f"Running {action} on {unit.name}…")
        task = _UnitActionTask(collector, unit.name, action)
        task.signals.finished.connect(self._on_action_finished)
        QThreadPool.globalInstance().start(task)

    def _on_action_finished(self, ok: bool, message: str) -> None:
        """Report the outcome of a unit action."""
        self.status_message.emit(message)
        if not ok:
            QMessageBox.warning(self, "Unit action failed", message)
        self.refresh()

    def on_shown(self) -> None:
        """Resume the systemd collector and refresh."""
        super().on_shown()
        self.context.monitor.set_enabled("services", True)
        self.refresh()

    def on_hidden(self) -> None:
        """Suspend the expensive systemd collector while the page is closed."""
        super().on_hidden()
        self.context.monitor.set_enabled("services", False)


class _ActionSignals(QObject):
    """Signals for :class:`_UnitActionTask`."""

    finished = Signal(bool, str)


class _UnitActionTask(QRunnable):
    """Runs a systemd unit action off the GUI thread.

    ``pkexec`` blocks until the user answers the authentication dialog, which may
    be many seconds -- unacceptable on the GUI thread.
    """

    def __init__(self, collector, unit: str, action: str) -> None:
        super().__init__()
        self._collector = collector
        self._unit = unit
        self._action = action
        self.signals = _ActionSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        """Perform the action and report the result."""
        try:
            ok, message = self._collector.control(self._unit, self._action)
        except Exception as exc:
            ok, message = False, f"Action failed: {exc}"
        self.signals.finished.emit(ok, message)


class _UnitDetail(QWidget):
    """Detail panel for a selected unit."""

    def __init__(self, page: ServicesPage) -> None:
        super().__init__(page)
        self._page = page
        self._unit = None
        theme = page.theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = Card("Unit detail", theme=theme,
                          accent=page.context.colour("services"))
        layout.addWidget(self._card)

        self._empty = EmptyState(
            "No unit selected",
            "Select a unit to see its state, resource usage and dependencies.",
            theme=theme, icon="▸",
        )
        self._card.add(self._empty)

        self._badge = Badge("", theme=theme)
        self._card.add_action(self._badge)

        self._table = KeyValueTable(theme=theme, label_width=140)
        for key, label in (
            ("name", "Unit"), ("description", "Description"),
            ("state", "Active state"), ("sub", "Sub-state"),
            ("load", "Load state"), ("enabled", "Startup"),
            ("pid", "Main PID"), ("memory", "Memory"),
            ("cpu", "CPU time"), ("tasks", "Tasks"),
            ("restarts", "Restart count"), ("since", "Active since"),
            ("result", "Last result"), ("path", "Unit file"),
        ):
            self._table.add_row(key, label)
        self._table.setVisible(False)
        self._card.add(self._table)

        self._dependencies = KeyValueTable(theme=theme, label_width=140)
        self._dependencies.add_row("requires", "Depends on")
        self._dependencies.add_row("required_by", "Required by")
        self._dependencies.setVisible(False)
        self._card.add(Divider())
        self._card.add(self._dependencies)

        self._logs_button = QPushButton("Show recent log entries")
        self._logs_button.setVisible(False)
        self._logs_button.clicked.connect(self._open_logs)
        self._card.add(self._logs_button)

    def set_unit(self, unit) -> None:
        """Change the displayed unit."""
        self._unit = unit
        visible = unit is not None
        self._empty.setVisible(not visible)
        self._table.setVisible(visible)
        self._dependencies.setVisible(visible)
        self._logs_button.setVisible(visible)
        self.refresh()
        if visible:
            self._load_dependencies(unit.name)

    def refresh(self) -> None:
        """Re-render the current unit's values."""
        unit = self._unit
        if unit is None:
            return
        theme = self._page.theme
        colours = {
            UnitState.ACTIVE: theme.palette.success,
            UnitState.FAILED: theme.palette.danger,
            UnitState.INACTIVE: theme.palette.text_muted,
        }
        self._badge.set_state(
            unit.state.value, colours.get(unit.state, theme.palette.info)
        )
        self._table.update_many({
            "name": unit.name,
            "description": unit.description,
            "state": unit.state.value,
            "sub": unit.sub_state,
            "load": unit.load_state,
            "enabled": unit.enablement,
            "pid": unit.main_pid,
            "memory": bytes_(unit.memory_bytes),
            "cpu": (
                f"{unit.cpu_seconds:.2f} s"
                if is_number(unit.cpu_seconds) else unit.cpu_seconds
            ),
            "tasks": unit.tasks,
            "restarts": unit.restart_count,
            "since": timestamp(unit.since),
            "result": unit.result,
            "path": unit.fragment_path,
        })

    def _load_dependencies(self, name: str) -> None:
        """Fetch a unit's dependency lists."""
        collector = self._page.context.monitor.collector_of(
            "services", ServiceCollector
        )
        if collector is None:
            return
        forward, reverse = collector.dependencies(name)
        self._dependencies.update_many({
            "requires": ", ".join(forward[:12]) or "—",
            "required_by": ", ".join(reverse[:12]) or "—",
        })

    def _open_logs(self) -> None:
        """Ask the main window to open the log viewer filtered to this unit."""
        if self._unit is not None:
            self._page.status_message.emit(
                f"Opening log viewer for {self._unit.name}"
            )
            window = self._page.window()
            navigate = getattr(window, "open_logs_for_unit", None)
            if callable(navigate):
                navigate(self._unit.name)
