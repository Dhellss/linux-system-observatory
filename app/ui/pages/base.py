"""Page infrastructure.

Every page follows the same contract, which is what keeps sixteen screens
coherent:

* It declares its ``domain``, ``title`` and ``icon`` as class attributes, so the
  sidebar builds itself from the page registry rather than from a parallel list
  that can drift out of sync.
* It receives a :class:`PageContext` holding the services it may use.  A page is
  never handed the whole application, so its dependencies are explicit and it can
  be constructed in isolation for testing.
* It implements :meth:`Page.on_snapshot` to consume data.  Pages never poll and
  never call a collector directly; data arrives through the monitor service.
* It may implement :meth:`Page.on_shown` / :meth:`Page.on_hidden`, which the main
  window uses to suspend expensive collectors while a page is not visible.

The MVVM boundary sits here: :class:`Page` is the View, and the snapshots it
receives are the ViewModel's output.  Pages hold no collection logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.capabilities import CapabilityRegistry
from app.core.config import ConfigService
from app.ui.themes.tokens import Theme

if TYPE_CHECKING:
    # Imported for typing only.  At run time these would form an import cycle
    # (services build pages, pages reference services), which is exactly the
    # situation TYPE_CHECKING exists for: full type information for the
    # checker and the IDE, no cycle for the interpreter.
    from app.services.alerts import AlertEngine
    from app.services.diagnostics import DiagnosticsService
    from app.services.export import ExportService
    from app.services.history import HistoryService
    from app.services.metrics import MetricRegistry
    from app.services.monitor import MonitorService
    from app.services.notifications import NotificationService


@dataclass(frozen=True)
class PageContext:
    """The services a page is permitted to use.

    Passing a context object rather than the application itself keeps the
    dependency surface visible: everything a page can reach is listed right here.
    """

    theme: Theme
    config: ConfigService
    capabilities: CapabilityRegistry
    monitor: MonitorService
    history: HistoryService
    alerts: AlertEngine
    diagnostics: DiagnosticsService
    export: ExportService
    notifications: NotificationService
    metrics: MetricRegistry

    def colour(self, domain: str) -> str:
        """Accent colour for a domain."""
        return self.theme.palette.domain(domain)


class Page(QWidget):
    """Base class for every page in the application."""

    #: Collector domain this page consumes; "" for pages with no live data.
    domain: ClassVar[str] = ""
    #: Additional domains this page also needs.
    extra_domains: ClassVar[tuple[str, ...]] = ()
    #: Sidebar label.
    title: ClassVar[str] = "Page"
    #: Single-character glyph used as the sidebar icon.
    icon: ClassVar[str] = "•"
    #: Sidebar grouping.
    section: ClassVar[str] = "Monitor"
    #: Short description shown in the toolbar and command palette.
    description: ClassVar[str] = ""
    #: When true, the page's domain collector is suspended while it is hidden.
    #: Set for expensive collectors whose data is not shown elsewhere.
    suspend_when_hidden: ClassVar[bool] = False

    #: Emitted when the page wants a transient message in the status bar.
    status_message = Signal(str)

    def __init__(self, context: PageContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.theme = context.theme
        self.palette_tokens = context.theme.palette
        self.metrics = context.theme.metrics
        self.setObjectName("PageRoot")
        # See the note in Sidebar: a QWidget subclass needs WA_StyledBackground
        # before a stylesheet `background` rule has any effect on it.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._visible = False
        self._built = False

        # Every page scrolls vertically.  Without this a small window clips
        # content with no way to reach it, which is the single most common
        # layout failure in dense monitoring UIs.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("PageScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        outer.addWidget(self._scroll)

        self._content = QWidget()
        self._content.setObjectName("PageScrollArea")
        self._content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._scroll.setWidget(self._content)

        self._layout = QVBoxLayout(self._content)
        margin = self.metrics.space_5
        self._layout.setContentsMargins(margin, margin, margin, margin)
        self._layout.setSpacing(self.metrics.space_4)

    # ------------------------------------------------------------------ building

    def build(self) -> None:
        """Construct the page's widgets.

        Called lazily the first time a page is shown, so start-up only pays for
        the pages the user actually opens.  With sixteen pages that is a
        meaningful difference in time-to-first-paint.
        """
        if self._built:
            return
        self._built = True
        self.build_ui()

    def build_ui(self) -> None:
        """Construct widgets.  Implemented by subclasses."""

    @property
    def built(self) -> bool:
        """True once :meth:`build` has run."""
        return self._built

    # ------------------------------------------------------------------- content

    def content_layout(self) -> QVBoxLayout:
        """The vertical layout page content is added to."""
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """Append a widget to the page and return it."""
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch: int = 0):
        """Append a nested layout to the page."""
        self._layout.addLayout(layout, stretch)
        return layout

    def add_grid(self, columns: int = 2, spacing: int | None = None) -> QGridLayout:
        """Append a responsive grid and return it.

        Columns are given equal stretch so cards share the width evenly, which is
        what makes the dashboard reflow sensibly when the window is resized.
        """
        grid = QGridLayout()
        gap = spacing if spacing is not None else self.metrics.space_4
        grid.setSpacing(gap)
        grid.setContentsMargins(0, 0, 0, 0)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        self._layout.addLayout(grid)
        return grid

    def add_section(self, title: str) -> QLabel:
        """Append a section heading."""
        label = QLabel(title, self._content)
        label.setProperty("role", "subheading")
        label.setStyleSheet(
            f"color: {self.palette_tokens.text_muted}; "
            f"font-size: {self.metrics.font_caption}pt; font-weight: 700; "
            "text-transform: uppercase; letter-spacing: 0.5px;"
        )
        self._layout.addWidget(label)
        return label

    def add_stretch(self) -> None:
        """Push subsequent content to the top of the page."""
        self._layout.addStretch(1)

    # ----------------------------------------------------------------- lifecycle

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Receive a new snapshot.  Implemented by subclasses that need data."""

    def on_shown(self) -> None:
        """Called when the page becomes visible."""
        self._visible = True

    def on_hidden(self) -> None:
        """Called when the page is navigated away from."""
        self._visible = False

    @property
    def is_visible_page(self) -> bool:
        """True while this page is the active one.

        Pages check this before doing expensive repaint work, because a snapshot
        still arrives for every domain regardless of which page is on screen.
        """
        return self._visible

    def domains_of_interest(self) -> tuple[str, ...]:
        """Every domain this page consumes."""
        return ((self.domain,) if self.domain else ()) + self.extra_domains

    def refresh(self) -> None:
        """Re-request data for this page's domains.

        Invoked by the toolbar refresh button and on page entry, so switching to
        a page with a slow cadence shows current data immediately.
        """
        monitor = self.context.monitor
        for domain in self.domains_of_interest():
            monitor.request(domain)

    # ------------------------------------------------------------------- helpers

    def unavailable_card(self, title: str, reason: object, hint: str = "") -> QWidget:
        """Build a card explaining why a whole feature is unavailable."""
        from app.ui.widgets.card import Card
        from app.ui.widgets.stat import EmptyState

        card = Card(title, theme=self.theme)
        card.add(EmptyState(
            str(reason),
            hint,
            theme=self.theme,
            icon="⊘",
        ))
        return card
