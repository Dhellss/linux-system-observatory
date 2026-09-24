"""Card container -- the fundamental layout unit of every page.

A card is a titled, rounded surface holding one coherent piece of information.
Consistency matters more than flexibility here: because every card builds its
header the same way, pages look deliberate rather than improvised, and a page
author cannot accidentally invent a fifth heading style.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.ui.themes.tokens import Theme


class Card(QFrame):
    """A titled surface with an optional accent stripe and header actions."""

    clicked = Signal()

    def __init__(
        self,
        title: str = "",
        *,
        theme: Theme,
        subtitle: str = "",
        accent: str | None = None,
        interactive: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._accent = accent
        self._interactive = interactive
        self.setObjectName("Card")
        self.setProperty("interactive", "true" if interactive else "false")
        if interactive:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        metrics = theme.metrics
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(
            metrics.space_4, metrics.space_3, metrics.space_4, metrics.space_4
        )
        self._outer.setSpacing(metrics.space_3)

        self._header: QWidget | None = None
        self._title_label: QLabel | None = None
        self._subtitle_label: QLabel | None = None
        self._actions: QHBoxLayout | None = None
        if title or subtitle:
            self._build_header(title, subtitle)

        self._body = QVBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(metrics.space_3)
        self._outer.addLayout(self._body)

    # -------------------------------------------------------------------- header

    def _build_header(self, title: str, subtitle: str) -> None:
        """Construct the title row, with a slot for action buttons on the right."""
        metrics = self._theme.metrics
        header = QWidget(self)
        header.setObjectName("CardHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(metrics.space_2)

        if self._accent:
            # A 3 px stripe carries the domain colour.  Qt stylesheets cannot do
            # shadows, so colour-coded stripes are how cards get visual identity.
            stripe = QFrame(header)
            stripe.setFixedSize(3, int(metrics.font_subheading * 1.9))
            stripe.setStyleSheet(
                f"background: {self._accent}; border-radius: 2px;"
            )
            row.addWidget(stripe, 0, Qt.AlignmentFlag.AlignVCenter)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        if title:
            self._title_label = QLabel(title, header)
            self._title_label.setProperty("role", "subheading")
            titles.addWidget(self._title_label)
        if subtitle:
            self._subtitle_label = QLabel(subtitle, header)
            self._subtitle_label.setProperty("role", "caption")
            titles.addWidget(self._subtitle_label)
        row.addLayout(titles)
        row.addStretch(1)

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(metrics.space_1)
        row.addLayout(self._actions)

        self._header = header
        self._outer.addWidget(header)

    # ---------------------------------------------------------------------- API

    def body(self) -> QVBoxLayout:
        """The layout content should be added to."""
        return self._body

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """Append ``widget`` to the card body and return it."""
        self._body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch: int = 0):
        """Append a nested layout to the card body."""
        self._body.addLayout(layout, stretch)
        return layout

    def add_action(self, widget: QWidget) -> QWidget:
        """Place a control in the header's action area."""
        if self._actions is None:
            self._build_header("", "")
        self._actions.addWidget(widget)  # type: ignore[union-attr]
        return widget

    def set_title(self, title: str) -> None:
        """Update the card title."""
        if self._title_label is not None:
            self._title_label.setText(title)

    def set_subtitle(self, subtitle: str) -> None:
        """Update or add the card subtitle."""
        if self._subtitle_label is not None:
            self._subtitle_label.setText(subtitle)
            self._subtitle_label.setVisible(bool(subtitle))

    def set_accent_border(self, enabled: bool) -> None:
        """Highlight the card's border, used to draw attention to an alert."""
        self.setProperty("accent", "true" if enabled else "false")
        # Qt only re-evaluates stylesheet rules against dynamic properties when
        # the style is explicitly re-polished.
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:
        """Emit :attr:`clicked` for interactive cards."""
        if self._interactive and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def grow(widget: QWidget, horizontal: bool = True, vertical: bool = False) -> QWidget:
    """Let ``widget`` expand along the requested axes.

    A one-line helper because responsive layouts need this constantly, and
    spelling out ``QSizePolicy`` at every call site obscures the intent.
    """
    widget.setSizePolicy(
        QSizePolicy.Policy.Expanding if horizontal else QSizePolicy.Policy.Preferred,
        QSizePolicy.Policy.Expanding if vertical else QSizePolicy.Policy.Preferred,
    )
    return widget
