"""The application shell: navigation, page hosting and global shortcuts.

Responsibilities are deliberately narrow.  The window routes snapshots to pages,
owns the chrome (sidebar, toolbar, status bar, command palette) and handles
shortcuts.  It contains no monitoring logic and no metric formatting; those
belong to the services and pages respectively.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.core.units import percent, rate, temperature
from app.models.base import is_available
from app.models.diagnostics import AlertEvent, Severity
from app.ui.pages.alerts import AlertsPage
from app.ui.pages.base import Page, PageContext
from app.ui.pages.battery import BatteryPage
from app.ui.pages.cpu import CpuPage
from app.ui.pages.dashboard import DashboardPage
from app.ui.pages.diagnostics import DiagnosticsPage
from app.ui.pages.gpu import GpuPage
from app.ui.pages.history import HistoryPage
from app.ui.pages.logs import LogsPage
from app.ui.pages.memory import MemoryPage
from app.ui.pages.network import NetworkPage
from app.ui.pages.processes import ProcessesPage
from app.ui.pages.sensors import SensorsPage
from app.ui.pages.services import ServicesPage
from app.ui.pages.settings import SettingsPage
from app.ui.pages.storage import StoragePage
from app.ui.pages.system import SystemPage
from app.ui.widgets.navigation import (
    Command,
    CommandPalette,
    NavEntry,
    Sidebar,
    StatusBarWidgets,
    Toolbar,
)

_log = logging.getLogger(__name__)

#: Page classes in sidebar order.
PAGE_CLASSES = (
    DashboardPage,
    CpuPage,
    MemoryPage,
    GpuPage,
    StoragePage,
    NetworkPage,
    ProcessesPage,
    ServicesPage,
    SensorsPage,
    BatteryPage,
    SystemPage,
    LogsPage,
    DiagnosticsPage,
    HistoryPage,
    AlertsPage,
    SettingsPage,
)


class MainWindow(QMainWindow):
    """The application's main window."""

    def __init__(self, context: PageContext) -> None:
        super().__init__()
        self.context = context
        self.theme = context.theme
        self._pages: dict[str, Page] = {}
        self._current: str = ""
        self._banner_timer: QTimer | None = None
        #: The most recent snapshot per domain.  Pages are built lazily, so a
        #: page opened for the first time would otherwise sit empty until its
        #: collector next fires -- up to 30 seconds for systemd.  Replaying the
        #: cached snapshot on entry makes every page populate instantly.
        self._latest: dict[str, object] = {}

        self.setWindowTitle("Linux System Observatory")
        self.setMinimumSize(1080, 700)

        self._build_layout()
        self._build_pages()
        self._build_shortcuts()
        self._connect_services()
        self._restore_state()

    # ------------------------------------------------------------------ layout

    def _build_layout(self) -> None:
        """Assemble the window chrome."""
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar(self.theme, central)
        self.sidebar.navigated.connect(self.navigate)
        root.addWidget(self.sidebar)

        right = QWidget(central)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.toolbar = Toolbar(self.theme, right)
        self.toolbar.refresh_requested.connect(self._refresh_current)
        self.toolbar.palette_requested.connect(self._open_palette)
        self.toolbar.pause_toggled.connect(self._set_paused)
        self.toolbar.sidebar_toggled.connect(self._toggle_sidebar)
        right_layout.addWidget(self.toolbar)

        self._banner = _Banner(self.theme, right)
        self._banner.setVisible(False)
        right_layout.addWidget(self._banner)

        self.stack = QStackedWidget(right)
        right_layout.addWidget(self.stack, 1)
        root.addWidget(right, 1)
        self.setCentralWidget(central)

        status = QStatusBar(self)
        status.setSizeGripEnabled(True)
        self._status_message = QLabel("Starting…")
        status.addWidget(self._status_message, 1)
        self.status_widgets = StatusBarWidgets(self.theme, status)
        status.addPermanentWidget(self.status_widgets)
        self.setStatusBar(status)
        status.setVisible(
            bool(self.context.config.get("appearance.show_status_bar", True))
        )

        self.palette_widget = CommandPalette(self.theme, self)

    def _build_pages(self) -> None:
        """Instantiate every page and populate the sidebar."""
        entries: list[NavEntry] = []
        for page_class in PAGE_CLASSES:
            page = page_class(self.context, self.stack)
            page.status_message.connect(self.show_message)
            self._pages[page_class.__name__] = page
            self.stack.addWidget(page)
            entries.append(NavEntry(
                key=page_class.__name__,
                title=page_class.title,
                icon=page_class.icon,
                section=page_class.section,
                description=page_class.description,
            ))
        self.sidebar.add_entries(entries)
        self.palette_widget.set_commands(self._build_commands())

    def _build_commands(self) -> list[Command]:
        """Assemble the command palette entries."""
        commands: list[Command] = [
            Command(
                key=f"go:{name}",
                title=page.title,
                category="Go to page",
                handler=partial(self.navigate, name),
                shortcut=(f"Ctrl+{index + 1}" if index < 9 else ""),
            )
            for index, (name, page) in enumerate(self._pages.items())
        ]
        commands.extend([
            Command("act:refresh", "Refresh current page", "Action",
                    self._refresh_current, "F5"),
            Command("act:pause", "Pause or resume monitoring", "Action",
                    lambda: self._set_paused(not self.context.monitor.paused),
                    "Ctrl+P"),
            Command("act:diagnostics", "Run diagnostics now", "Action",
                    self._run_diagnostics),
            Command("act:sidebar", "Toggle sidebar", "Action",
                    self._toggle_sidebar, "Ctrl+B"),
            Command("act:search", "Focus search", "Action",
                    self.toolbar.focus_search, "Ctrl+F"),
        ])
        for name, label in (
            ("dark", "Dark"), ("midnight", "Midnight (OLED)"), ("light", "Light"),
        ):
            commands.append(Command(
                f"theme:{name}", f"Switch to {label} theme", "Appearance",
                partial(self.context.config.set, "appearance.theme", name),
            ))
        return commands

    def _build_shortcuts(self) -> None:
        """Install global keyboard shortcuts."""
        bindings = (
            ("Ctrl+K", self._open_palette),
            ("Ctrl+P", lambda: self._set_paused(not self.context.monitor.paused)),
            ("Ctrl+F", self.toolbar.focus_search),
            ("Ctrl+B", self._toggle_sidebar),
            ("F5", self._refresh_current),
            ("Ctrl+Q", self.close),
            ("Escape", self._on_escape),
        )
        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

        # Ctrl+1..9 jump straight to the first nine pages.
        for index, name in enumerate(list(self._pages)[:9], start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda n=name: self.navigate(n))

    def _connect_services(self) -> None:
        """Wire service signals into the window."""
        monitor = self.context.monitor
        monitor.snapshot_ready.connect(self._on_snapshot)
        monitor.collection_failed.connect(self._on_collection_failed)
        self.context.notifications.banner_requested.connect(self._show_banner)
        self.context.alerts.alert_changed.connect(self._on_alert)
        self.context.config.subscribe(self._on_setting_changed)

        # The status bar's monitoring-cost figure updates on its own slow timer:
        # recomputing it on every snapshot would be wasted work for a number
        # that changes slowly.
        self._cost_timer = QTimer(self)
        self._cost_timer.setInterval(5000)
        self._cost_timer.timeout.connect(self._update_cost)
        self._cost_timer.start()

    def _restore_state(self) -> None:
        """Restore geometry and the last visited page."""
        config = self.context.config
        width = int(config.get("ui_state.window_width", 1360))
        height = int(config.get("ui_state.window_height", 880))
        self.resize(width, height)
        if config.get("ui_state.maximised", False):
            self.showMaximized()
        if config.get("ui_state.sidebar_collapsed", False):
            self.sidebar.set_collapsed(True)

        last = str(config.get("ui_state.last_page", "DashboardPage"))
        self.navigate(last if last in self._pages else "DashboardPage")

    # -------------------------------------------------------------- navigation

    def navigate(self, key: str) -> None:
        """Switch to a page, building it on first visit."""
        page = self._pages.get(key)
        if page is None or key == self._current:
            return
        if previous := self._pages.get(self._current):
            previous.on_hidden()

        first_build = not page.built
        page.build()
        if first_build:
            self._replay_snapshots(page)
        self.stack.setCurrentWidget(page)
        self.sidebar.select(key)
        self.toolbar.set_page(page.title, page.description)
        self.toolbar.clear_search()
        self._current = key
        page.on_shown()
        self.context.config.set("ui_state.last_page", key)

    def _replay_snapshots(self, page: Page) -> None:
        """Feed a newly-built page every snapshot it missed."""
        for domain in page.domains_of_interest():
            snapshot = self._latest.get(domain)
            if snapshot is not None:
                # on_shown has not run yet, so mark the page visible for the
                # replay -- pages skip expensive repaints when hidden, and this
                # one genuinely is about to be shown.
                page.on_shown()
                page.on_snapshot(domain, snapshot)

    def open_logs_for_unit(self, unit: str) -> None:
        """Open the log viewer filtered to one systemd unit.

        Called by the services page, which is why it lives on the window: a page
        should not reach directly into a sibling page.
        """
        self.navigate("LogsPage")
        logs = self._pages.get("LogsPage")
        if isinstance(logs, LogsPage):
            logs.filter_to_unit(unit)

    # ------------------------------------------------------------------ actions

    def _refresh_current(self) -> None:
        """Re-sample the current page's domains."""
        if page := self._pages.get(self._current):
            page.refresh()
            self.show_message(f"Refreshing {page.title}…")

    def _set_paused(self, paused: bool) -> None:
        """Pause or resume monitoring."""
        monitor = self.context.monitor
        if paused:
            monitor.pause()
            self.show_message("Monitoring paused")
        else:
            monitor.resume()
            self.show_message("Monitoring resumed")
        self.toolbar.set_paused(paused)

    def _toggle_sidebar(self) -> None:
        """Collapse or expand the sidebar."""
        collapsed = not self.sidebar.collapsed
        self.sidebar.set_collapsed(collapsed)
        self.context.config.set("ui_state.sidebar_collapsed", collapsed)

    def _open_palette(self) -> None:
        """Show the command palette."""
        self.palette_widget.open()

    def _on_escape(self) -> None:
        """Dismiss transient UI."""
        if self.palette_widget.isVisible():
            self.palette_widget.hide()
        elif self._banner.isVisible():
            self._banner.setVisible(False)
        else:
            self.toolbar.clear_search()

    def _run_diagnostics(self) -> None:
        """Jump to diagnostics and run the checks."""
        self.navigate("DiagnosticsPage")
        page = self._pages.get("DiagnosticsPage")
        if isinstance(page, DiagnosticsPage):
            page.run_checks()

    # ------------------------------------------------------------------ signals

    def _on_snapshot(self, domain: str, snapshot: object) -> None:
        """Route a snapshot to the history, alerts and every interested page."""
        self._latest[domain] = snapshot
        history = self.context.history
        values = history.record(domain, snapshot)
        self.context.alerts.evaluate(values)
        self.context.diagnostics.store.put(domain, snapshot)

        for page in self._pages.values():
            # Unbuilt pages have no widgets to update; skipping them is what
            # makes lazy page construction worthwhile.
            if page.built and domain in page.domains_of_interest():
                page.on_snapshot(domain, snapshot)
        self._update_status(domain, snapshot)

    def _on_collection_failed(self, domain: str) -> None:
        """Note a collector failure in the status bar."""
        _log.debug("Collector %s returned no snapshot", domain)

    def _on_alert(self, event: AlertEvent) -> None:
        """Deliver a notification and update the sidebar badge."""
        self.context.notifications.notify_alert(event)
        active = self.context.alerts.active()
        worst = self.context.alerts.worst_severity()
        self.sidebar.set_badge(
            "AlertsPage", len(active), self.theme.palette.status(int(worst))
        )
        self.status_widgets.set(
            "alerts",
            f"{len(active)} alert(s)" if active else "",
            self.theme.palette.status(int(worst)) if active else None,
        )

    def _on_setting_changed(self, key: str, value: object) -> None:
        """React to settings the window itself owns."""
        if key == "appearance.show_status_bar":
            self.statusBar().setVisible(bool(value))
        elif key in ("appearance.theme", "appearance.density",
                     "appearance.font_scale"):
            self._apply_theme()

    def _apply_theme(self) -> None:
        """Rebuild and apply the stylesheet after a theme change.

        Only the stylesheet is regenerated.  Widgets that painted themselves with
        palette colours (charts, gauges) keep their original colours until the
        application restarts, which the settings page states plainly rather than
        pretending a full live restyle happens.
        """
        from PySide6.QtWidgets import QApplication

        from app.ui.themes.stylesheet import build
        from app.ui.themes.tokens import Theme

        settings = self.context.config.settings.appearance
        theme = Theme.build(
            settings.theme,
            density=settings.density,
            font_scale=settings.font_scale,
        )
        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(build(theme))
        self.show_message(
            "Theme applied. Restart to restyle charts and gauges as well."
        )

    # ------------------------------------------------------------------- status

    def _update_status(self, domain: str, snapshot: Any) -> None:
        """Keep the status bar's live summary current."""
        palette = self.theme.palette
        if domain == "cpu":
            self.status_widgets.set(
                "cpu", f"CPU {percent(snapshot.usage, precision=0)}",
                palette.load_colour(snapshot.usage),
            )
            if is_available(snapshot.temperature):
                self.status_widgets.set(
                    "thermal", temperature(snapshot.temperature, precision=0)
                )
        elif domain == "memory":
            self.status_widgets.set(
                "memory",
                f"RAM {percent(snapshot.percent, precision=0)}",
                palette.load_colour(snapshot.percent),
            )
        elif domain == "gpu":
            primary = snapshot.primary
            if primary is not None and is_available(primary.utilisation):
                self.status_widgets.set(
                    "gpu", f"GPU {percent(primary.utilisation, precision=0)}"
                )
        elif domain == "network":
            totals = snapshot.totals
            self.status_widgets.set(
                "network",
                f"↓{rate(totals.download_bytes_per_s, precision=0)} "
                f"↑{rate(totals.upload_bytes_per_s, precision=0)}",
            )

    def _update_cost(self) -> None:
        """Show what monitoring itself is costing."""
        total = self.context.monitor.total_cost_ms_per_second
        self.status_widgets.set("cost", f"monitor {total / 10:.1f}% CPU")

    def show_message(self, message: str, timeout: int = 4000) -> None:
        """Display a transient status-bar message."""
        self._status_message.setText(message)
        QTimer.singleShot(
            timeout,
            lambda: (
                self._status_message.setText("")
                if self._status_message.text() == message else None
            ),
        )

    def _show_banner(self, title: str, body: str, severity: int) -> None:
        """Show an in-app alert banner."""
        if not self.context.config.get("notifications.in_app_banners", True):
            return
        self._banner.show_message(title, body, Severity(severity))
        if self._banner_timer is not None:
            self._banner_timer.stop()
        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(lambda: self._banner.setVisible(False))
        self._banner_timer.start(12000)

    # ---------------------------------------------------------------- lifecycle

    def closeEvent(self, event: QCloseEvent) -> None:
        """Persist window state before closing."""
        config = self.context.config
        config.set("ui_state.maximised", self.isMaximized(), persist=False)
        if not self.isMaximized():
            config.set("ui_state.window_width", self.width(), persist=False)
            config.set("ui_state.window_height", self.height(), persist=False)
        config.save()
        super().closeEvent(event)


class _Banner(QFrame):
    """An in-window alert strip."""

    def __init__(self, theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("Banner")
        metrics = theme.metrics
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_4, metrics.space_2, metrics.space_3, metrics.space_2
        )
        layout.setSpacing(metrics.space_3)

        self._icon = QLabel("●", self)
        layout.addWidget(self._icon)

        self._text = QLabel("", self)
        self._text.setWordWrap(True)
        layout.addWidget(self._text, 1)

        from PySide6.QtWidgets import QPushButton

        dismiss = QPushButton("Dismiss", self)
        dismiss.setProperty("variant", "ghost")
        dismiss.clicked.connect(lambda: self.setVisible(False))
        layout.addWidget(dismiss)

    def show_message(self, title: str, body: str, severity: Severity) -> None:
        """Display an alert."""
        colour = self._theme.palette.status(int(severity))
        self._icon.setStyleSheet(f"color: {colour}; font-size: 14px;")
        self._text.setText(f"<b>{title}</b> — {body}")
        self.setStyleSheet(
            f"QFrame#Banner {{ background: {self._theme.palette.surface_raised}; "
            f"border-left: 3px solid {colour}; "
            f"border-bottom: 1px solid {self._theme.palette.border}; }}"
        )
        self.setVisible(True)
