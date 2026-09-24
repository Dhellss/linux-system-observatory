"""Process manager: list and tree views, filtering, inspection and control.

Destructive actions (terminate, kill) always confirm first and name the process,
because a mis-click in a process manager can lose the user's work.  The
collector additionally refuses to signal PID 1 or the monitor's own process.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.collectors.process import ProcessCollector
from app.core.units import (
    bytes_,
    clock,
    count,
    duration,
    percent,
    rate,
    text,
    timestamp,
)
from app.models.base import is_number
from app.models.process import ProcessInfo, ProcessSnapshot, ProcessState
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.stat import EmptyState, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable

#: Filter presets offered in the toolbar.
_FILTERS = (
    ("All processes", lambda p: True),
    ("User processes", lambda p: not p.is_kernel_thread),
    ("Active (CPU > 0.1%)", lambda p: p.cpu_percent > 0.1),
    ("Heavy memory (> 100 MB)", lambda p: p.memory_rss_bytes > 100 * 1024**2),
    ("Running", lambda p: p.state is ProcessState.RUNNING),
    ("Zombies", lambda p: p.state is ProcessState.ZOMBIE),
)


class ProcessesPage(Page):
    """A full process manager."""

    domain = "processes"
    title = "Processes"
    icon = "▦"
    section = "Monitor"
    description = "Process table, tree view and inspector"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("processes")
        self._accent = accent
        self._snapshot: ProcessSnapshot | None = None
        self._selected_pid: int | None = None
        self._expanded: set[int] = set()

        self._build_summary(accent)
        self._build_body(accent)

    def _build_summary(self, accent: str) -> None:
        """Counts and the filter toolbar."""
        card = Card("Process table", theme=self.theme, accent=accent)
        self._stats = StatRow(theme=self.theme, columns=6)
        for key, label in (
            ("total", "Total"), ("running", "Runnable"),
            ("sleeping", "Sleeping"), ("zombie", "Zombies"),
            ("threads", "Threads"), ("restricted", "Restricted"),
        ):
            self._stats.add(key, label, size="small")
        card.add(self._stats)

        controls = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText(
            "Filter by name, PID, user or command line…"
        )
        self._search.textChanged.connect(self._on_search)
        controls.addWidget(self._search, 1)

        self._filter_box = QComboBox()
        self._filter_box.addItems([name for name, _ in _FILTERS])
        self._filter_box.setCurrentIndex(1)
        self._filter_box.currentIndexChanged.connect(self._apply_filter)
        controls.addWidget(self._filter_box)

        self._view_toggle = QPushButton("Tree view")
        self._view_toggle.setCheckable(True)
        self._view_toggle.toggled.connect(self._on_view_toggled)
        controls.addWidget(self._view_toggle)

        self._normalise = QPushButton("CPU ÷ cores")
        self._normalise.setCheckable(True)
        self._normalise.setChecked(True)
        self._normalise.setToolTip(
            "When enabled, CPU percentages are shares of the whole machine so "
            "the column sums to 100%. When disabled, 100% means one saturated "
            "core, as top(1) reports it."
        )
        self._normalise.toggled.connect(self._on_normalise_toggled)
        controls.addWidget(self._normalise)
        card.add_layout(controls)
        self.add(card)

    def _build_body(self, accent: str) -> None:
        """The split view: table or tree on the left, inspector on the right."""
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._table = self._build_table()
        self._tree = self._build_tree()
        self._tree.setVisible(False)
        left_layout.addWidget(self._table)
        left_layout.addWidget(self._tree)
        splitter.addWidget(left)

        self._inspector = _Inspector(self)
        splitter.addWidget(self._inspector)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 420])
        self.content_layout().addWidget(splitter, 1)
        self._apply_filter()

    def _build_table(self) -> DataTable:
        """The flat, sortable process table."""
        palette = self.palette_tokens

        def cpu_colour(process: ProcessInfo) -> str | None:
            scaled = process.cpu_percent * (12 if self._normalise.isChecked() else 1)
            return palette.load_colour(min(100.0, scaled))

        columns: list[Column] = [
            Column("pid", "PID", lambda p: p.pid, width=72, mono=True),
            Column("name", "Name", lambda p: p.name, width=190,
                   tooltip=lambda p: p.display_command[:400]),
            Column("cpu", "CPU", lambda p: p.cpu_percent,
                   display=lambda v: percent(v, precision=1), width=76,
                   mono=True, colour=cpu_colour),
            Column("memory", "Memory", lambda p: p.memory_rss_bytes,
                   display=bytes_, width=108, mono=True),
            Column("mem_pct", "Mem %", lambda p: p.memory_percent,
                   display=lambda v: percent(v), width=72, mono=True),
            Column("state", "State", lambda p: p.state.value, width=104,
                   colour=lambda p: (
                       palette.warning if p.state is ProcessState.ZOMBIE
                       else palette.success if p.state is ProcessState.RUNNING
                       else None
                   )),
            Column("user", "User", lambda p: text(p.username), width=104),
            Column("threads", "Threads", lambda p: p.num_threads, width=74,
                   mono=True),
            Column("read", "Disk read",
                   lambda p: (
                       p.io_read_bytes_per_s
                       if is_number(p.io_read_bytes_per_s) else -1
                   ),
                   display=lambda v: rate(v) if v >= 0 else "—",
                   width=94, mono=True),
            Column("write", "Disk write",
                   lambda p: (
                       p.io_write_bytes_per_s
                       if is_number(p.io_write_bytes_per_s) else -1
                   ),
                   display=lambda v: rate(v) if v >= 0 else "—",
                   width=94, mono=True),
            Column("gpu", "GPU memory",
                   lambda p: p.gpu_memory_bytes or 0,
                   display=lambda v: bytes_(v) if v else "—",
                   width=100, mono=True),
            Column("runtime", "Runtime", lambda p: p.runtime or 0,
                   display=lambda v: duration(v, compact=True), width=96,
                   mono=True),
            Column("command", "Command", lambda p: p.display_command, width=0,
                   mono=True),
        ]
        table = DataTable(columns, theme=self.theme, stretch_column=12)
        table.sort_by(2, descending=True)
        table.customContextMenuRequested.connect(self._show_context_menu)
        table.selectionModel().selectionChanged.connect(self._on_selection)
        table.doubleClicked.connect(lambda _: self._inspector.focus())
        return table

    def _build_tree(self) -> QTreeWidget:
        """The parent/child hierarchy view."""
        tree = QTreeWidget()
        tree.setColumnCount(6)
        tree.setHeaderLabels(
            ["Process", "PID", "CPU", "Memory", "Threads", "User"]
        )
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._show_context_menu)
        tree.itemSelectionChanged.connect(self._on_tree_selection)
        tree.itemExpanded.connect(
            lambda item: self._expanded.add(item.data(0, Qt.ItemDataRole.UserRole))
        )
        tree.itemCollapsed.connect(
            lambda item: self._expanded.discard(item.data(0, Qt.ItemDataRole.UserRole))
        )
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index, width in ((1, 78), (2, 76), (3, 94), (4, 74), (5, 110)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
            tree.setColumnWidth(index, width)
        return tree

    # ------------------------------------------------------------------ updates

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a process snapshot."""
        if not isinstance(snapshot, ProcessSnapshot):
            return
        self._snapshot = snapshot
        if not self.is_visible_page:
            return
        palette = self.palette_tokens
        self._stats["total"].set_value(str(snapshot.total))
        self._stats["running"].set_value(str(snapshot.running))
        self._stats["sleeping"].set_value(str(snapshot.sleeping))
        self._stats["zombie"].set_value(
            str(snapshot.zombie),
            colour=palette.warning if snapshot.zombie else None,
        )
        self._stats["threads"].set_value(str(snapshot.threads))
        self._stats["restricted"].set_value(str(snapshot.restricted_count))

        if self._view_toggle.isChecked():
            self._refresh_tree(snapshot)
        else:
            self._table.set_rows(snapshot.processes)
        self._inspector.refresh()

    def _refresh_tree(self, snapshot: ProcessSnapshot) -> None:
        """Rebuild the tree, preserving expansion and selection.

        The tree is rebuilt rather than diffed because the hierarchy changes
        shape as processes come and go.  Expansion state is restored from the
        tracked PID set so the view does not collapse under the user.
        """
        self._tree.setUpdatesEnabled(False)
        self._tree.clear()
        selected_item: QTreeWidgetItem | None = None
        predicate = _FILTERS[self._filter_box.currentIndex()][1]
        needle = self._search.text().strip().lower()

        def matches(process: ProcessInfo) -> bool:
            if not predicate(process):
                return False
            if not needle:
                return True
            return (
                needle in process.name.lower()
                or needle in str(process.pid)
                or needle in str(process.username).lower()
                or needle in process.display_command.lower()
            )

        def add(node, parent) -> QTreeWidgetItem | None:
            process = node.process
            # A parent is kept when any descendant matches, so filtering never
            # orphans a matching child from its ancestry.
            children_items = []
            for child in node.children:
                created = add(child, None)
                if created is not None:
                    children_items.append(created)
            if not matches(process) and not children_items:
                return None
            item = QTreeWidgetItem([
                process.name,
                str(process.pid),
                percent(process.cpu_percent),
                bytes_(process.memory_rss_bytes),
                str(process.num_threads),
                text(process.username),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, process.pid)
            item.setToolTip(0, process.display_command[:400])
            for column in (1, 2, 3, 4):
                item.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
            item.addChildren(children_items)
            if parent is None:
                pass
            else:
                parent.addChild(item)
            nonlocal selected_item
            if process.pid == self._selected_pid:
                selected_item = item
            return item

        roots = []
        for node in snapshot.build_tree():
            created = add(node, None)
            if created is not None:
                roots.append(created)
        self._tree.addTopLevelItems(roots)

        # Restore expansion.
        def restore(item: QTreeWidgetItem) -> None:
            pid = item.data(0, Qt.ItemDataRole.UserRole)
            if pid in self._expanded:
                item.setExpanded(True)
            for index in range(item.childCount()):
                restore(item.child(index))

        for index in range(self._tree.topLevelItemCount()):
            restore(self._tree.topLevelItem(index))
        if selected_item is not None:
            self._tree.setCurrentItem(selected_item)
        self._tree.setUpdatesEnabled(True)

    # ------------------------------------------------------------------ filters

    def _on_search(self, needle: str) -> None:
        """Apply the free-text filter to whichever view is active."""
        self._table.proxy.set_search(needle)
        if self._view_toggle.isChecked() and self._snapshot is not None:
            self._refresh_tree(self._snapshot)

    def _apply_filter(self) -> None:
        """Apply the selected preset filter."""
        predicate = _FILTERS[self._filter_box.currentIndex()][1]
        self._table.proxy.set_predicate(predicate)
        if self._view_toggle.isChecked() and self._snapshot is not None:
            self._refresh_tree(self._snapshot)

    def _on_view_toggled(self, tree_mode: bool) -> None:
        """Switch between the flat table and the hierarchy."""
        self._table.setVisible(not tree_mode)
        self._tree.setVisible(tree_mode)
        self._view_toggle.setText("Table view" if tree_mode else "Tree view")
        if tree_mode and self._snapshot is not None:
            self._refresh_tree(self._snapshot)
        elif self._snapshot is not None:
            self._table.set_rows(self._snapshot.processes)

    def _on_normalise_toggled(self, enabled: bool) -> None:
        """Switch the CPU column between whole-machine and per-core scaling."""
        collector = self.context.monitor.collector_of("processes", ProcessCollector)
        if collector is not None:
            collector.normalise_cpu = enabled
        self.status_message.emit(
            "CPU shown as share of all cores" if enabled
            else "CPU shown per core (100% = one saturated core)"
        )

    # ---------------------------------------------------------------- selection

    def _on_selection(self) -> None:
        """Track the selected process in the table view."""
        process = self._table.selected_object()
        self._selected_pid = process.pid if process else None
        self._inspector.set_pid(self._selected_pid)

    def _on_tree_selection(self) -> None:
        """Track the selected process in the tree view."""
        item = self._tree.currentItem()
        self._selected_pid = (
            item.data(0, Qt.ItemDataRole.UserRole) if item else None
        )
        self._inspector.set_pid(self._selected_pid)

    def _current_process(self) -> ProcessInfo | None:
        """The currently selected process, from whichever view is active."""
        if self._snapshot is None or self._selected_pid is None:
            return None
        return self._snapshot.by_pid().get(self._selected_pid)

    # ------------------------------------------------------------------ actions

    def _show_context_menu(self, position) -> None:
        """Build and show the per-process context menu."""
        process = self._current_process()
        if process is None:
            return
        menu = QMenu(self)
        menu.addAction(
            f"Process {process.pid} — {process.name}"
        ).setEnabled(False)
        menu.addSeparator()

        inspect = QAction("Inspect", self)
        inspect.triggered.connect(self._inspector.focus)
        menu.addAction(inspect)

        copy = QAction("Copy command line", self)
        copy.triggered.connect(lambda: self._copy(process.display_command))
        menu.addAction(copy)
        menu.addSeparator()

        priority = QAction("Change priority (nice)…", self)
        priority.triggered.connect(lambda: self._change_priority(process))
        menu.addAction(priority)

        affinity = QAction("Set CPU affinity…", self)
        affinity.triggered.connect(lambda: self._change_affinity(process))
        menu.addAction(affinity)
        menu.addSeparator()

        if process.state is ProcessState.STOPPED:
            resume = QAction("Resume", self)
            resume.triggered.connect(lambda: self._act(process, "resume"))
            menu.addAction(resume)
        else:
            suspend = QAction("Suspend", self)
            suspend.triggered.connect(lambda: self._act(process, "suspend"))
            menu.addAction(suspend)

        terminate = QAction("Terminate (SIGTERM)", self)
        terminate.triggered.connect(lambda: self._confirm_signal(process, "terminate"))
        menu.addAction(terminate)

        kill = QAction("Kill (SIGKILL)", self)
        kill.triggered.connect(lambda: self._confirm_signal(process, "kill"))
        menu.addAction(kill)

        source = self._tree if self._view_toggle.isChecked() else self._table
        menu.exec(source.viewport().mapToGlobal(position))

    def _copy(self, value: str) -> None:
        """Copy text to the clipboard."""
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(value)
        self.status_message.emit("Copied to clipboard")

    def _confirm_signal(self, process: ProcessInfo, action: str) -> None:
        """Confirm before sending a terminating signal.

        The dialog names the process and PID explicitly.  ``kill`` additionally
        warns that the process cannot save its state, because that is the
        practical difference the user needs to weigh.
        """
        verb = "Terminate" if action == "terminate" else "Force kill"
        detail = (
            "The process will be asked to shut down cleanly and can save its "
            "work."
            if action == "terminate"
            else "The process will be stopped immediately and CANNOT save its "
                 "work. Unsaved data will be lost."
        )
        box = QMessageBox(self)
        box.setWindowTitle(f"{verb} process?")
        box.setIcon(
            QMessageBox.Icon.Warning if action == "terminate"
            else QMessageBox.Icon.Critical
        )
        box.setText(f"{verb} <b>{process.name}</b> (PID {process.pid})?")
        box.setInformativeText(f"{detail}\n\n{process.display_command[:300]}")
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._act(process, action)

    def _act(self, process: ProcessInfo, action: str) -> None:
        """Perform a process action and report the outcome."""
        collector = self.context.monitor.collector_of("processes", ProcessCollector)
        if collector is None:
            return
        ok, message = getattr(collector, action)(process.pid)
        self.status_message.emit(message)
        if not ok:
            QMessageBox.warning(self, "Action failed", message)
        else:
            QTimer.singleShot(400, self.refresh)

    def _change_priority(self, process: ProcessInfo) -> None:
        """Prompt for and apply a new nice value."""
        current = process.nice if isinstance(process.nice, int) else 0
        value, accepted = QInputDialog.getInt(
            self, "Change priority",
            f"Nice value for {process.name} (PID {process.pid}).\n"
            "Lower is higher priority; values below 0 require root.",
            current, -20, 19, 1,
        )
        if not accepted:
            return
        collector = self.context.monitor.collector_of("processes", ProcessCollector)
        if collector is None:
            return
        ok, message = collector.set_nice(process.pid, value)
        self.status_message.emit(message)
        if not ok:
            QMessageBox.warning(self, "Could not change priority", message)

    def _change_affinity(self, process: ProcessInfo) -> None:
        """Prompt for and apply a CPU affinity mask."""
        import psutil

        total = psutil.cpu_count(logical=True) or 1
        current = ", ".join(str(c) for c in process.cpu_affinity) or \
            ", ".join(str(i) for i in range(total))
        value, accepted = QInputDialog.getText(
            self, "Set CPU affinity",
            f"Comma-separated CPU list for {process.name} "
            f"(0-{total - 1}):",
            QLineEdit.EchoMode.Normal, current,
        )
        if not accepted:
            return
        try:
            cpus = sorted({
                int(token.strip()) for token in value.split(",") if token.strip()
            })
        except ValueError:
            QMessageBox.warning(
                self, "Invalid input",
                "Enter CPU numbers separated by commas, for example: 0, 1, 2, 3",
            )
            return
        if any(cpu < 0 or cpu >= total for cpu in cpus):
            QMessageBox.warning(
                self, "Invalid CPU",
                f"CPU numbers must be between 0 and {total - 1}.",
            )
            return
        collector = self.context.monitor.collector_of("processes", ProcessCollector)
        if collector is None:
            return
        ok, message = collector.set_affinity(process.pid, cpus)
        self.status_message.emit(message)
        if not ok:
            QMessageBox.warning(self, "Could not set affinity", message)

    def on_shown(self) -> None:
        """Refresh on entry."""
        super().on_shown()
        self.refresh()


class _Inspector(QWidget):
    """Detail panel for the selected process.

    Expensive attributes (threads, open files, environment) are fetched only for
    the selected process and only while the inspector is visible, which is what
    keeps the bulk process poll cheap.
    """

    def __init__(self, page: ProcessesPage) -> None:
        super().__init__(page)
        self._page = page
        self._pid: int | None = None
        theme = page.theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = Card("Inspector", theme=theme,
                          accent=page.context.colour("processes"))
        layout.addWidget(self._card)

        self._empty = EmptyState(
            "No process selected",
            "Select a process to see its threads, open files, environment and "
            "resource usage.",
            theme=theme, icon="▸",
        )
        self._card.add(self._empty)

        self._tabs = QTabWidget()
        self._tabs.setVisible(False)
        self._card.add(self._tabs)

        self._overview = KeyValueTable(theme=theme, label_width=130)
        for key, label in (
            ("pid", "PID"), ("ppid", "Parent PID"), ("name", "Name"),
            ("state", "State"), ("user", "User"), ("started", "Started"),
            ("runtime", "Runtime"), ("cpu", "CPU usage"),
            ("cpu_time", "CPU time"), ("memory", "Resident memory"),
            ("vms", "Virtual memory"), ("threads", "Threads"),
            ("nice", "Nice value"), ("fds", "Open descriptors"),
            ("ctx", "Context switches"), ("connections", "Network sockets"),
            ("affinity", "CPU affinity"), ("exe", "Executable"),
            ("cwd", "Working directory"),
        ):
            self._overview.add_row(key, label)
        self._tabs.addTab(_scrollable(self._overview), "Overview")

        self._command = QPlainTextEdit()
        self._command.setReadOnly(True)
        self._tabs.addTab(self._command, "Command line")

        self._threads = DataTable(
            [
                Column("tid", "Thread ID", lambda t: t.tid, width=110, mono=True),
                Column("user", "User time", lambda t: t.user_time,
                       display=lambda v: f"{v:.2f} s", width=110, mono=True),
                Column("system", "System time", lambda t: t.system_time,
                       display=lambda v: f"{v:.2f} s", width=110, mono=True),
                Column("total", "Total CPU", lambda t: t.cpu_time,
                       display=lambda v: f"{v:.2f} s", width=0, mono=True),
            ],
            theme=theme, stretch_column=3,
        )
        self._tabs.addTab(self._threads, "Threads")

        self._files = DataTable(
            [
                Column("fd", "FD", lambda f: f.fd, width=64, mono=True),
                Column("mode", "Mode", lambda f: text(f.mode), width=70),
                Column("path", "Path", lambda f: f.path, width=0, mono=True),
            ],
            theme=theme, stretch_column=2,
        )
        self._tabs.addTab(self._files, "Open files")

        self._environment = DataTable(
            [
                Column("key", "Variable", lambda e: e[0], width=190, mono=True),
                Column("value", "Value", lambda e: e[1], width=0, mono=True),
            ],
            theme=theme, stretch_column=1,
        )
        self._tabs.addTab(self._environment, "Environment")

    def set_pid(self, pid: int | None) -> None:
        """Change the inspected process."""
        self._pid = pid
        self._empty.setVisible(pid is None)
        self._tabs.setVisible(pid is not None)
        self.refresh()

    def focus(self) -> None:
        """Bring the inspector's overview tab forward."""
        self._tabs.setCurrentIndex(0)

    def refresh(self) -> None:
        """Re-read the inspected process's expensive attributes."""
        if self._pid is None:
            return
        collector = self._page.context.monitor.collector_of(
            "processes", ProcessCollector
        )
        if collector is None:
            return
        process = collector.inspect(self._pid)
        if process is None:
            self._card.set_subtitle(f"PID {self._pid} has exited")
            return
        self._card.set_subtitle(f"{process.name} (PID {process.pid})")
        self._overview.update_many({
            "pid": process.pid,
            "ppid": process.ppid,
            "name": process.name,
            "state": process.state.value,
            "user": process.username,
            "started": timestamp(process.create_time),
            "runtime": duration(process.runtime),
            "cpu": percent(process.cpu_percent),
            "cpu_time": clock(process.cpu_time),
            "memory": f"{bytes_(process.memory_rss_bytes)} "
                      f"({percent(process.memory_percent)})",
            "vms": bytes_(process.memory_vms_bytes),
            "threads": process.num_threads,
            "nice": process.nice,
            "fds": process.num_fds,
            "ctx": count(process.num_ctx_switches),
            "connections": process.connections,
            "affinity": (
                ", ".join(str(c) for c in process.cpu_affinity) or "—"
            ),
            "exe": process.exe,
            "cwd": process.cwd,
        })
        self._command.setPlainText(process.display_command)
        self._threads.set_rows(process.threads)
        self._files.set_rows(process.open_files)
        self._environment.set_rows(sorted(process.environment.items()))
        self._tabs.setTabText(2, f"Threads ({len(process.threads)})")
        self._tabs.setTabText(3, f"Open files ({len(process.open_files)})")
        self._tabs.setTabText(4, f"Environment ({len(process.environment)})")


def _scrollable(widget: QWidget) -> QWidget:
    """Wrap a widget in a scroll area, for tabs whose content can overflow."""
    from PySide6.QtWidgets import QFrame, QScrollArea

    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.addWidget(widget)
    layout.addStretch(1)
    area.setWidget(container)
    return area
