"""Sidebar navigation, toolbar, status bar and command palette."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.ui.themes.tokens import Theme


@dataclass(frozen=True)
class NavEntry:
    """One sidebar destination."""

    key: str
    title: str
    icon: str
    section: str
    description: str = ""


class Sidebar(QWidget):
    """Grouped, single-selection navigation list.

    Entries come from the page registry, so the sidebar cannot drift out of sync
    with the pages that actually exist.
    """

    navigated = Signal(str)

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._buttons: dict[str, QPushButton] = {}
        self._badges: dict[str, QLabel] = {}
        #: Entry metadata, kept so the collapse toggle can rebuild the labels.
        self._entries: dict[str, NavEntry] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._collapsed = False

        self.setObjectName("Sidebar")
        # A plain QWidget subclass does not paint the `background` declared for
        # it in a stylesheet -- Qt only does that for QFrame and friends unless
        # WA_StyledBackground is set explicitly.  Without this the sidebar keeps
        # the default light palette background while the rest of the window is
        # dark.  The same applies to every QWidget subclass styled by object
        # name below.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(theme.metrics.sidebar_width)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # The scroll area's viewport and its content widget are separate,
        # unnamed children; without their own background rule they paint the
        # default palette and punch a light rectangle through the dark sidebar.
        scroll.viewport().setAutoFillBackground(False)
        container = QWidget()
        container.setObjectName("SidebarList")
        container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(0, theme.metrics.space_2, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        outer.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        """Application title block."""
        metrics = self._theme.metrics
        header = QWidget(self)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(
            metrics.space_4, metrics.space_4, metrics.space_3, metrics.space_3
        )
        layout.setSpacing(metrics.space_2)

        mark = QLabel("◉", header)
        mark.setStyleSheet(
            f"color: {self._theme.palette.accent}; "
            f"font-size: {metrics.font_heading}pt;"
        )
        layout.addWidget(mark)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        title = QLabel("Observatory", header)
        title.setObjectName("AppTitle")
        titles.addWidget(title)
        subtitle = QLabel("Linux System Monitor", header)
        subtitle.setProperty("role", "caption")
        subtitle.setStyleSheet(
            f"color: {self._theme.palette.text_subtle}; "
            f"font-size: {metrics.font_caption}pt;"
        )
        titles.addWidget(subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)
        self._header_texts = (title, subtitle, mark)
        return header

    def _build_footer(self) -> QWidget:
        """Collapse toggle and keyboard hint."""
        metrics = self._theme.metrics
        footer = QWidget(self)
        layout = QVBoxLayout(footer)
        layout.setContentsMargins(
            metrics.space_2, metrics.space_2, metrics.space_2, metrics.space_3
        )
        layout.setSpacing(metrics.space_1)

        divider = QFrame(footer)
        divider.setProperty("role", "divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        self._hint = QLabel("Ctrl+K  command palette", footer)
        self._hint.setProperty("role", "caption")
        self._hint.setStyleSheet(
            f"color: {self._theme.palette.text_subtle}; "
            f"font-size: {metrics.font_caption}pt; padding: 2px 8px;"
        )
        layout.addWidget(self._hint)
        return footer

    # ------------------------------------------------------------------ entries

    def add_entries(self, entries: list[NavEntry]) -> None:
        """Build the navigation list, grouped by section in declaration order."""
        # Remove the trailing stretch so new rows append above it.
        stretch = self._list_layout.takeAt(self._list_layout.count() - 1)
        current_section = ""
        for entry in entries:
            if entry.section != current_section:
                current_section = entry.section
                label = QLabel(entry.section)
                label.setObjectName("SidebarSection")
                self._list_layout.addWidget(label)
            self._list_layout.addWidget(self._build_button(entry))
        if stretch is not None:
            self._list_layout.addItem(stretch)

    def _build_button(self, entry: NavEntry) -> QWidget:
        """One navigation row, with an optional badge."""
        metrics = self._theme.metrics
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, metrics.space_3, 0)
        layout.setSpacing(0)

        button = QPushButton(f"  {entry.icon}   {entry.title}", row)
        button.setObjectName("NavItem")
        button.setCheckable(True)
        button.setToolTip(entry.description or entry.title)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda: self.navigated.emit(entry.key))
        self._group.addButton(button)
        self._buttons[entry.key] = button
        self._entries[entry.key] = entry
        layout.addWidget(button, 1)

        badge = QLabel("", row)
        badge.setObjectName("NavBadge")
        badge.setVisible(False)
        self._badges[entry.key] = badge
        layout.addWidget(badge)
        return row

    def select(self, key: str) -> None:
        """Mark one entry as active without emitting :attr:`navigated`."""
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def set_badge(self, key: str, count: int, colour: str | None = None) -> None:
        """Show a count badge on one entry; zero hides it."""
        badge = self._badges.get(key)
        if badge is None:
            return
        badge.setVisible(count > 0)
        badge.setText(str(count) if count < 100 else "99+")
        if colour:
            metrics = self._theme.metrics
            badge.setStyleSheet(
                f"background: {colour}; color: {self._theme.palette.text_inverse}; "
                f"border-radius: {metrics.radius_pill}px; "
                f"font-size: {metrics.font_caption}pt; font-weight: 700; "
                "padding: 1px 7px;"
            )

    def set_collapsed(self, collapsed: bool) -> None:
        """Collapse the sidebar to an icon rail."""
        self._collapsed = collapsed
        metrics = self._theme.metrics
        self.setFixedWidth(
            metrics.sidebar_collapsed if collapsed else metrics.sidebar_width
        )
        for key, button in self._buttons.items():
            entry = self._entries[key]
            button.setText(
                f"  {entry.icon}" if collapsed
                else f"  {entry.icon}   {entry.title}"
            )
            # In the icon rail the label is gone, so the tooltip carries the name.
            button.setToolTip(
                f"{entry.title} — {entry.description}" if collapsed
                else (entry.description or entry.title)
            )
        title, subtitle, _mark = self._header_texts
        title.setVisible(not collapsed)
        subtitle.setVisible(not collapsed)
        self._hint.setVisible(not collapsed)

    @property
    def collapsed(self) -> bool:
        """True while the sidebar is collapsed."""
        return self._collapsed


class Toolbar(QWidget):
    """Top bar: page title, search, and page-level actions."""

    search_changed = Signal(str)
    refresh_requested = Signal()
    palette_requested = Signal()
    pause_toggled = Signal(bool)
    sidebar_toggled = Signal()

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("Toolbar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(theme.metrics.toolbar_height)

        metrics = theme.metrics
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_3, metrics.space_2, metrics.space_4, metrics.space_2
        )
        layout.setSpacing(metrics.space_3)

        toggle = QPushButton("☰")
        toggle.setProperty("variant", "ghost")
        toggle.setFixedWidth(30)
        toggle.setToolTip("Collapse or expand the sidebar (Ctrl+B)")
        toggle.clicked.connect(self.sidebar_toggled.emit)
        layout.addWidget(toggle)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        self._title = QLabel("Dashboard", self)
        self._title.setObjectName("PageTitle")
        titles.addWidget(self._title)
        self._subtitle = QLabel("", self)
        self._subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self._subtitle)
        layout.addLayout(titles)
        layout.addStretch(1)

        self._search = QLineEdit(self)
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Search…  (Ctrl+F)")
        self._search.setFixedWidth(240)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self.search_changed.emit)
        layout.addWidget(self._search)

        palette_button = QPushButton("⌘  Commands")
        palette_button.setToolTip("Open the command palette (Ctrl+K)")
        palette_button.clicked.connect(self.palette_requested.emit)
        layout.addWidget(palette_button)

        self._pause = QPushButton("Pause")
        self._pause.setCheckable(True)
        self._pause.setToolTip("Suspend all monitoring (Ctrl+P)")
        self._pause.toggled.connect(self._on_pause)
        layout.addWidget(self._pause)

        refresh = QPushButton("Refresh")
        refresh.setToolTip("Sample this page's data now (F5)")
        refresh.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(refresh)

    def set_page(self, title: str, subtitle: str) -> None:
        """Update the page title block."""
        self._title.setText(title)
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))

    def focus_search(self) -> None:
        """Move keyboard focus to the search field."""
        self._search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._search.selectAll()

    def clear_search(self) -> None:
        """Empty the search field."""
        self._search.clear()

    def set_paused(self, paused: bool) -> None:
        """Reflect the monitoring state without re-emitting."""
        self._pause.blockSignals(True)
        self._pause.setChecked(paused)
        self._pause.setText("Resume" if paused else "Pause")
        self._pause.blockSignals(False)

    def _on_pause(self, paused: bool) -> None:
        """Relay the pause toggle."""
        self._pause.setText("Resume" if paused else "Pause")
        self.pause_toggled.emit(paused)


class StatusBarWidgets(QWidget):
    """Live summary strip shown in the window's status bar."""

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, theme.metrics.space_3, 0)
        layout.setSpacing(theme.metrics.space_4)
        self._labels: dict[str, QLabel] = {}
        for key in ("cpu", "memory", "gpu", "network", "thermal", "alerts", "cost"):
            label = QLabel("", self)
            label.setStyleSheet(
                f"color: {theme.palette.text_muted}; "
                f"font-family: {theme.metrics.mono_family}; "
                f"font-size: {theme.metrics.font_caption}pt;"
            )
            self._labels[key] = label
            layout.addWidget(label)

    def set(self, key: str, text: str, colour: str | None = None) -> None:
        """Update one status field."""
        label = self._labels.get(key)
        if label is None:
            return
        label.setText(text)
        if colour:
            label.setStyleSheet(
                f"color: {colour}; "
                f"font-family: {self._theme.metrics.mono_family}; "
                f"font-size: {self._theme.metrics.font_caption}pt;"
            )


@dataclass(frozen=True)
class Command:
    """One command-palette entry."""

    key: str
    title: str
    category: str
    handler: Callable[[], None]
    shortcut: str = ""


class CommandPalette(QWidget):
    """A fuzzy-matching command launcher, in the manner of an editor.

    Implemented as a frameless popup over the main window rather than a modal
    dialog, so it appears instantly and dismisses on focus loss.
    """

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._commands: list[Command] = []
        self.setObjectName("CommandPalette")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setFixedWidth(620)

        metrics = theme.metrics
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_3, metrics.space_3, metrics.space_3, metrics.space_3
        )
        layout.setSpacing(metrics.space_2)

        self._input = QLineEdit(self)
        self._input.setPlaceholderText("Type a command or page name…")
        self._input.textChanged.connect(self._filter)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        self._list = QListWidget(self)
        self._list.setObjectName("CommandList")
        self._list.setFixedHeight(340)
        self._list.itemActivated.connect(self._activate)
        self._list.itemClicked.connect(self._activate)
        layout.addWidget(self._list)

    def set_commands(self, commands: list[Command]) -> None:
        """Replace the command set."""
        self._commands = commands

    def open(self) -> None:
        """Show the palette centred over the parent window."""
        parent = self.parentWidget()
        if parent is not None:
            geometry = parent.geometry()
            self.move(
                geometry.center().x() - self.width() // 2,
                geometry.top() + 120,
            )
        self._input.clear()
        self._filter("")
        self.show()
        self._input.setFocus(Qt.FocusReason.PopupFocusReason)

    def _filter(self, needle: str) -> None:
        """Rank commands against the query.

        Scoring prefers a prefix match on the title, then a substring match, then
        a subsequence match -- so typing "cpu" reaches the processor page before
        anything merely containing those letters.
        """
        self._list.clear()
        query = needle.strip().lower()
        scored: list[tuple[int, Command]] = []
        for command in self._commands:
            score = _score(query, command)
            if score >= 0:
                scored.append((score, command))
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))

        for _score_value, command in scored[:60]:
            item = QListWidgetItem(
                # A right-pointing guillemet is the conventional separator
                # in a command palette; it is deliberate, not a stray character.
                f"{command.category}  \u203a  {command.title}"
                + (f"      {command.shortcut}" if command.shortcut else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, command.key)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _activate(self, item: QListWidgetItem | None = None) -> None:
        """Run the selected command."""
        item = item or self._list.currentItem()
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        self.hide()
        for command in self._commands:
            if command.key == key:
                command.handler()
                return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Route arrow keys and Enter from the input to the list."""
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self._list.currentRow()
                delta = 1 if key == Qt.Key.Key_Down else -1
                self._list.setCurrentRow(
                    max(0, min(self._list.count() - 1, row + delta))
                )
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._activate()
                return True
            if key == Qt.Key.Key_Escape:
                self.hide()
                return True
        return bool(super().eventFilter(watched, event))


def _score(query: str, command: Command) -> int:
    """Rank one command against a query; negative means no match."""
    if not query:
        return 1
    title = command.title.lower()
    category = command.category.lower()
    if title.startswith(query):
        return 100
    if query in title:
        return 70
    if category.startswith(query):
        return 50
    if query in category:
        return 40
    # Subsequence match: every query character appears in order.
    position = 0
    for char in query:
        position = title.find(char, position)
        if position < 0:
            return -1
        position += 1
    return 20
