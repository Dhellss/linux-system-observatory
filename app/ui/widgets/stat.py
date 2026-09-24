"""Stat tiles and key/value tables -- how numbers are presented.

Two rules make the numbers readable, and both are easy to get wrong:

**Tabular figures.**  Metrics use a monospace font so that a value changing from
9.9 to 10.0 does not shift everything after it.  A dashboard where the text jitters
every second feels broken even when it is correct.

**Absence is shown, not hidden.**  A tile whose value is ``Unavailable`` renders an
em dash and puts the explanation in its tooltip, so the layout stays stable and
the user can always find out *why* a number is missing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.models.base import Unavailable
from app.ui.themes.tokens import Theme


class StatTile(QWidget):
    """A label, a large value, and an optional caption and trend marker."""

    def __init__(
        self,
        label: str,
        *,
        theme: Theme,
        value: str = "—",
        caption: str = "",
        colour: str | None = None,
        compact: bool = False,
        size: str = "medium",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        metrics = theme.metrics
        palette = theme.palette

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        self._label = QLabel(label.upper(), self)
        self._label.setProperty("role", "caption")
        self._label.setStyleSheet(
            f"color: {palette.text_subtle}; font-size: {metrics.font_caption}pt;"
            "font-weight: 700; letter-spacing: 0.4px;"
        )
        layout.addWidget(self._label)

        # Three sizes, because one does not fit every context.  A dashboard hero
        # figure wants "display"; a four-up row of supporting values wants
        # "medium", where 26 pt would dominate the card and force text to wrap.
        point_size = {
            "display": metrics.font_metric,
            "medium": metrics.font_heading,
            "small": metrics.font_subheading,
        }.get("small" if compact else size, metrics.font_heading)
        self._value = QLabel(value, self)
        self._value.setProperty("role", "metric")
        self._base_colour = colour or palette.text
        self._value.setStyleSheet(
            f"font-family: {metrics.mono_family}; font-size: {point_size}pt; "
            f"font-weight: 600; color: {self._base_colour};"
        )
        self._value.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._value)

        self._caption = QLabel(caption, self)
        self._caption.setProperty("role", "caption")
        self._caption.setStyleSheet(
            f"color: {palette.text_muted}; font-size: {metrics.font_caption}pt;"
        )
        self._caption.setVisible(bool(caption))
        layout.addWidget(self._caption)
        layout.addStretch(1)

    def set_value(
        self, value: str, *, colour: str | None = None, tooltip: str = ""
    ) -> None:
        """Update the displayed value and optionally recolour it."""
        self._value.setText(value)
        if colour is not None and colour != self._base_colour:
            self._base_colour = colour
            current = self._value.styleSheet()
            # Replace only the colour declaration so font settings survive.
            self._value.setStyleSheet(
                current.rsplit("color:", 1)[0] + f"color: {colour};"
            )
        self._value.setToolTip(tooltip)

    def set_from(
        self,
        value: object,
        formatter,
        *,
        colour: str | None = None,
    ) -> None:
        """Set the value from a possibly-``Unavailable`` reading.

        This is the method pages should use: it formats the value, and when the
        reading is absent it dims the tile and moves the explanation into the
        tooltip rather than truncating the layout or showing a misleading zero.
        """
        # ``None`` reaches here whenever a collector had nothing to report but
        # did not build an Unavailable -- an optional latency figure on an idle
        # disk, for instance.  Treating it like an absent reading keeps every
        # caller free of its own None check, and stops a formatter being handed
        # a value it cannot format.
        if value is None or isinstance(value, Unavailable):
            self.set_value(
                "—",
                colour=self._theme.palette.text_subtle,
                tooltip=str(value) if value is not None else "",
            )
            return
        try:
            text = formatter(value)
        except (TypeError, ValueError):
            text = "—"
        self.set_value(text, colour=colour, tooltip="")

    def set_caption(self, caption: str) -> None:
        """Update the caption beneath the value."""
        self._caption.setText(caption)
        self._caption.setVisible(bool(caption))

    def set_label(self, label: str) -> None:
        """Update the tile's label."""
        self._label.setText(label.upper())


class StatRow(QWidget):
    """A responsive row of :class:`StatTile` widgets.

    Uses a grid rather than a horizontal box so that tiles wrap onto a second
    line on a narrow window instead of being squeezed illegibly.
    """

    def __init__(
        self, *, theme: Theme, columns: int = 4, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._columns = columns
        self._tiles: dict[str, StatTile] = {}
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(theme.metrics.space_6)
        self._grid.setVerticalSpacing(theme.metrics.space_4)

    def add(
        self, key: str, label: str, *, caption: str = "", colour: str | None = None,
        compact: bool = False, size: str = "medium",
    ) -> StatTile:
        """Add a tile identified by ``key``."""
        tile = StatTile(
            label, theme=self._theme, caption=caption, colour=colour,
            compact=compact, size=size, parent=self,
        )
        position = len(self._tiles)
        self._grid.addWidget(
            tile, position // self._columns, position % self._columns
        )
        for column in range(self._columns):
            self._grid.setColumnStretch(column, 1)
        self._tiles[key] = tile
        return tile

    def tile(self, key: str) -> StatTile | None:
        """Look up a tile by key."""
        return self._tiles.get(key)

    def __getitem__(self, key: str) -> StatTile:
        return self._tiles[key]

    def set(self, key: str, value: object, formatter, *,
            colour: str | None = None) -> None:
        """Update one tile, ignoring unknown keys."""
        tile = self._tiles.get(key)
        if tile is not None:
            tile.set_from(value, formatter, colour=colour)


class KeyValueTable(QWidget):
    """A two-column detail list, used wherever static properties are shown.

    Rows are added once and updated by key, so a page can refresh values without
    rebuilding widgets -- which would otherwise discard the user's text selection
    and scroll position on every poll.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        columns: int = 1,
        label_width: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._columns = max(1, columns)
        self._label_width = label_width
        self._rows: dict[str, QLabel] = {}
        self._count = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(theme.metrics.space_4)
        self._grid.setVerticalSpacing(theme.metrics.space_2)
        for index in range(self._columns):
            self._grid.setColumnStretch(index * 2 + 1, 1)

    def add_row(self, key: str, label: str, value: object = "—") -> QLabel:
        """Add a labelled row and return its value label."""
        palette = self._theme.palette
        metrics = self._theme.metrics

        name = QLabel(label, self)
        name.setStyleSheet(
            f"color: {palette.text_muted}; font-size: {metrics.font_body}pt;"
        )
        name.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        if self._label_width:
            name.setMinimumWidth(self._label_width)

        display = QLabel(self)
        display.setStyleSheet(
            f"color: {palette.text}; font-family: {metrics.mono_family}; "
            f"font-size: {metrics.font_body}pt;"
        )
        display.setWordWrap(True)
        display.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._set_label(display, value)

        column = self._count % self._columns
        row = self._count // self._columns
        self._grid.addWidget(name, row, column * 2)
        self._grid.addWidget(display, row, column * 2 + 1)
        self._rows[key] = display
        self._count += 1
        return display

    def set(self, key: str, value: object) -> None:
        """Update one row's value, ignoring unknown keys."""
        label = self._rows.get(key)
        if label is not None:
            self._set_label(label, value)

    def update_many(self, values: dict[str, object]) -> None:
        """Update several rows at once."""
        for key, value in values.items():
            self.set(key, value)

    def _set_label(self, label: QLabel, value: object) -> None:
        """Render a value, dimming and explaining it when unavailable."""
        palette = self._theme.palette
        if isinstance(value, Unavailable):
            label.setText("—")
            label.setToolTip(str(value))
            label.setStyleSheet(
                label.styleSheet().rsplit("color:", 1)[0]
                + f"color: {palette.text_subtle};"
            )
            return
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        elif value is None:
            text = "—"
        else:
            text = str(value)
        label.setText(text or "—")
        label.setToolTip("")

    def rows(self) -> int:
        """Number of rows added."""
        return self._count


class Badge(QLabel):
    """A small coloured pill, used for states and counts."""

    def __init__(
        self,
        text: str = "",
        *,
        theme: Theme,
        colour: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._theme = theme
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_colour(colour or theme.palette.text_muted)

    def set_colour(self, colour: str) -> None:
        """Recolour the badge, tinting its background from the same hue."""
        metrics = self._theme.metrics
        self.setStyleSheet(
            f"background: {_tint(colour)}; color: {colour}; "
            f"border: 1px solid {_tint(colour, 0.35)}; "
            f"border-radius: {metrics.radius_pill}px; padding: 2px 8px; "
            f"font-size: {metrics.font_caption}pt; font-weight: 600;"
        )

    def set_state(self, text: str, colour: str) -> None:
        """Update both the text and the colour."""
        self.setText(text)
        self.set_colour(colour)


def _tint(colour: str, alpha: float = 0.16) -> str:
    """Build a translucent version of a hex colour for use as a background."""
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        return "transparent"
    try:
        red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "transparent"
    return f"rgba({red}, {green}, {blue}, {alpha})"


class Divider(QFrame):
    """A one-pixel rule for separating sections within a card."""

    def __init__(
        self, *, vertical: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setProperty("role", "vdivider" if vertical else "divider")
        self.setFrameShape(
            QFrame.Shape.VLine if vertical else QFrame.Shape.HLine
        )
        self.setFrameShadow(QFrame.Shadow.Plain)


class EmptyState(QWidget):
    """Placeholder shown when a page has nothing to display.

    Always explains *why* the area is empty, and where possible what the user
    could do about it.  A blank panel is indistinguishable from a bug.
    """

    def __init__(
        self,
        title: str,
        detail: str = "",
        *,
        theme: Theme,
        icon: str = "○",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        palette, metrics = theme.palette, theme.metrics
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_6, metrics.space_8, metrics.space_6, metrics.space_8
        )
        layout.setSpacing(metrics.space_2)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        glyph = QLabel(icon, self)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setStyleSheet(
            f"color: {palette.text_subtle}; font-size: {metrics.font_display}pt;"
        )
        layout.addWidget(glyph)

        heading = QLabel(title, self)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet(
            f"color: {palette.text_muted}; font-size: {metrics.font_subheading}pt; "
            "font-weight: 600;"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self._detail = QLabel(detail, self)
        self._detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail.setWordWrap(True)
        self._detail.setStyleSheet(
            f"color: {palette.text_subtle}; font-size: {metrics.font_body}pt;"
        )
        self._detail.setVisible(bool(detail))
        self._detail.setMaximumWidth(420)
        self._detail.setMinimumHeight(int(metrics.font_body * 5))
        self._detail.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self._detail.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
        )
        layout.addWidget(self._detail, 1, Qt.AlignmentFlag.AlignHCenter)

        self._heading = heading

    def set_message(self, title: str, detail: str = "") -> None:
        """Update the placeholder text."""
        self._heading.setText(title)
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))
