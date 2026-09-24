"""Custom-painted indicators: ring gauges, bars, heatmaps and composition bars.

All hand-painted with ``QPainter``.  These shapes are either impossible in a Qt
stylesheet (an arc) or need a per-cell colour that would mean hundreds of styled
child widgets (a core heatmap).  Painting directly is both simpler and far
cheaper: the core heatmap for a 128-thread machine is one ``paintEvent``, not 128
widgets.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.models.base import Unavailable
from app.ui.themes.tokens import Palette, Theme


def _sense_colour(palette: Palette, value: float, sense: str) -> str:
    """Pick an automatic colour for a 0-100 value given its meaning.

    A single "high is bad" rule is wrong for half the gauges in a system
    monitor: 100% CPU deserves red, but 100% battery deserves green and a
    clock at 100% of its rated speed deserves no judgement at all.
    """
    if sense == "reserve":
        if value <= 15:
            return palette.danger
        if value <= 35:
            return palette.warning
        return palette.success
    if sense == "neutral":
        return palette.accent
    return palette.load_colour(value)


class RingGauge(QWidget):
    """A circular progress ring with a value in the middle.

    Used for the headline figure on dashboard cards, where a ring communicates
    "proportion of a whole" more immediately than a number alone.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        size: int = 116,
        thickness: int = 9,
        label: str = "",
        colour: str | None = None,
        sense: str = "load",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._thickness = thickness
        self._label = label
        self._value: float | None = 0.0
        self._text = "0%"
        self._colour = QColor(colour or theme.palette.accent)
        self._auto_colour = colour is None
        # How the automatic colour should read the value:
        #   "load"    -- high is bad  (CPU, memory, disk usage)
        #   "reserve" -- high is good (battery charge, battery health)
        #   "neutral" -- magnitude carries no judgement (clock speed)
        # Without this distinction a full battery would be painted the same
        # alarming red as a full disk.
        self._sense = sense
        self.setFixedSize(size, size)

    def set_value(
        self, percent: float | Unavailable | None, text: str | None = None
    ) -> None:
        """Set the ring fill (0-100) and the centre text."""
        if isinstance(percent, Unavailable) or percent is None:
            self._value = None
            self._text = "—"
            self.setToolTip(str(percent) if percent is not None else "")
        else:
            self._value = max(0.0, min(100.0, float(percent)))
            self._text = text if text is not None else f"{self._value:.0f}%"
            self.setToolTip("")
            if self._auto_colour:
                self._colour = QColor(_sense_colour(
                    self._theme.palette, self._value, self._sense
                ))
        self.update()

    def set_colour(self, colour: str) -> None:
        """Pin the ring to an explicit colour."""
        self._colour = QColor(colour)
        self._auto_colour = False
        self.update()

    def set_label(self, label: str) -> None:
        """Set the small caption beneath the value."""
        self._label = label
        self.update()

    def paintEvent(self, event) -> None:
        """Paint the track, the arc, and the centred text."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette, metrics = self._theme.palette, self._theme.metrics

        inset = self._thickness / 2 + 1
        box = QRectF(inset, inset,
                     self.width() - 2 * inset, self.height() - 2 * inset)

        track = QPen(QColor(palette.surface_sunken), self._thickness)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(box, 0, 360 * 16)

        if self._value is not None and self._value > 0:
            arc = QPen(self._colour, self._thickness)
            arc.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc)
            # Qt angles are in sixteenths of a degree, measured counter-clockwise
            # from three o'clock.  Starting at 90 puts zero at the top and the
            # negative span makes it fill clockwise, as users expect.
            painter.drawArc(box, 90 * 16, -int(self._value * 3.6 * 16))

        painter.setPen(QColor(palette.text if self._value is not None
                              else palette.text_subtle))
        value_font = QFont()
        value_font.setWeight(QFont.Weight.DemiBold)
        # Shrink the centre text until it fits inside the ring.  A value like
        # "3.41 GHz" is far wider than "37%", and at a fixed size it would spill
        # over the arc on both sides.
        available = self.width() - self._thickness * 2 - 12
        size = metrics.font_display * 0.82
        while size > 7.0:
            value_font.setPointSizeF(size)
            painter.setFont(value_font)
            if painter.fontMetrics().horizontalAdvance(self._text) <= available:
                break
            size -= 0.75
        painter.setFont(value_font)
        text_box = QRectF(0, 0, self.width(), self.height())
        if self._label:
            text_box.adjust(0, -metrics.space_3, 0, -metrics.space_3)
        painter.drawText(text_box, Qt.AlignmentFlag.AlignCenter, self._text)

        if self._label:
            painter.setPen(QColor(palette.text_subtle))
            label_font = QFont()
            label_font.setPointSizeF(metrics.font_caption)
            painter.setFont(label_font)
            label_box = QRectF(
                0, self.height() / 2 + metrics.space_2,
                self.width(), metrics.space_5,
            )
            painter.drawText(label_box, Qt.AlignmentFlag.AlignCenter, self._label)
        painter.end()


class BarMeter(QWidget):
    """A horizontal bar with an optional label and value text."""

    def __init__(
        self,
        *,
        theme: Theme,
        label: str = "",
        height: int = 6,
        colour: str | None = None,
        show_text: bool = True,
        sense: str = "load",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._sense = sense
        self._label = label
        self._bar_height = height
        self._value: float | None = 0.0
        self._text = ""
        self._colour = QColor(colour or theme.palette.accent)
        self._auto_colour = colour is None
        text_space = (
            int(theme.metrics.font_caption * 2.4) if (label or show_text) else 0
        )
        self.setMinimumHeight(height + text_space)
        self._show_text = show_text
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setFixedHeight(height + text_space)

    def set_value(
        self, percent: float | Unavailable | None, text: str = ""
    ) -> None:
        """Set the bar fill (0-100) and its trailing value text."""
        if isinstance(percent, Unavailable) or percent is None:
            self._value = None
            self._text = "—"
            self.setToolTip(str(percent) if percent is not None else "")
        else:
            self._value = max(0.0, min(100.0, float(percent)))
            self._text = text or f"{self._value:.0f}%"
            self.setToolTip("")
            if self._auto_colour:
                self._colour = QColor(_sense_colour(
                    self._theme.palette, self._value, self._sense
                ))
        self.update()

    def set_label(self, label: str) -> None:
        """Update the bar's label."""
        self._label = label
        self.update()

    def set_colour(self, colour: str) -> None:
        """Pin the bar to an explicit colour."""
        self._colour = QColor(colour)
        self._auto_colour = False
        self.update()

    def paintEvent(self, event) -> None:
        """Paint the label row then the bar."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette, metrics = self._theme.palette, self._theme.metrics

        top = 0.0
        if self._label or self._show_text:
            font = QFont()
            font.setPointSizeF(metrics.font_caption)
            painter.setFont(font)
            row = QRectF(0, 0, self.width(), metrics.font_caption * 2.0)
            if self._label:
                painter.setPen(QColor(palette.text_muted))
                painter.drawText(
                    row,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    self._label,
                )
            if self._show_text:
                painter.setPen(QColor(palette.text))
                mono = QFont(font)
                mono.setFamilies(["JetBrains Mono", "Noto Sans Mono", "monospace"])
                painter.setFont(mono)
                painter.drawText(
                    row,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    self._text,
                )
            top = row.height()

        radius = self._bar_height / 2
        track = QRectF(0, top, self.width(), self._bar_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.surface_sunken))
        painter.drawRoundedRect(track, radius, radius)

        if self._value:
            filled = QRectF(track)
            filled.setWidth(max(self._bar_height, track.width() * self._value / 100.0))
            painter.setBrush(self._colour)
            painter.drawRoundedRect(filled, radius, radius)
        painter.end()


class CoreHeatmap(QWidget):
    """A grid of per-core utilisation cells.

    One widget paints every core.  A 128-thread server would otherwise need 128
    child widgets, each with its own stylesheet evaluation -- this draws the whole
    grid in a single pass, and the cell layout adapts to the widget width so the
    grid stays roughly square on any window size.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        cell: int = 30,
        gap: int = 3,
        show_index: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._cell = cell
        self._gap = gap
        self._show_index = show_index
        self._values: list[float] = []
        self._tooltips: list[str] = []
        self.setMinimumHeight(cell + gap * 2)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.setMouseTracking(True)

    def set_values(
        self, values: list[float], tooltips: list[str] | None = None
    ) -> None:
        """Replace the per-core utilisation values."""
        changed = len(values) != len(self._values)
        self._values = values
        self._tooltips = tooltips or []
        if changed:
            self.updateGeometry()
        self.update()

    def _layout(self) -> tuple[int, int, float]:
        """Compute ``(columns, rows, cell_size)`` for the current width."""
        count = len(self._values)
        if count == 0:
            return 0, 0, float(self._cell)
        available = max(1, self.width())
        per_column = self._cell + self._gap
        columns = max(1, min(count, (available + self._gap) // per_column))
        # Prefer a layout that fills whole rows, which looks tidier than a
        # trailing row with a single cell.
        rows = (count + columns - 1) // columns
        if rows > 1:
            columns = (count + rows - 1) // rows
            rows = (count + columns - 1) // columns
        size = min(
            float(self._cell),
            (available - self._gap * (columns - 1)) / columns,
        )
        return columns, rows, size

    def sizeHint(self):
        """Height needed for the current core count."""
        from PySide6.QtCore import QSize

        _columns, rows, size = self._layout()
        height = (
            int(rows * size + max(0, rows - 1) * self._gap) if rows else self._cell
        )
        return QSize(self._cell * 4, height)

    def resizeEvent(self, event) -> None:
        """Re-measure when the width changes, since the grid reflows."""
        self.updateGeometry()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        """Paint each core as a rounded cell tinted by its utilisation."""
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette, metrics = self._theme.palette, self._theme.metrics
        columns, _rows, size = self._layout()

        font = QFont()
        font.setPointSizeF(max(6.0, metrics.font_caption - 1.5))
        painter.setFont(font)

        for index, value in enumerate(self._values):
            column = index % columns
            row = index // columns
            box = QRectF(
                column * (size + self._gap),
                row * (size + self._gap),
                size, size,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(palette.surface_sunken))
            painter.drawRoundedRect(box, 4, 4)

            if value > 0:
                tint = QColor(palette.load_colour(value))
                # Opacity tracks load so a lightly-used core reads as a faint
                # wash and a saturated one as solid colour -- the grid becomes
                # scannable at a glance rather than needing the numbers read.
                tint.setAlphaF(0.22 + 0.78 * min(1.0, value / 100.0))
                painter.setBrush(tint)
                painter.drawRoundedRect(box, 4, 4)

            if self._show_index and size >= 22:
                painter.setPen(QColor(
                    palette.text if value > 55 else palette.text_muted
                ))
                painter.drawText(box, Qt.AlignmentFlag.AlignCenter, str(index))
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        """Show a per-core tooltip under the cursor."""
        if not self._values:
            return
        columns, _rows, size = self._layout()
        position = event.position()
        column = int(position.x() // (size + self._gap))
        row = int(position.y() // (size + self._gap))
        index = row * columns + column
        if 0 <= index < len(self._values) and column < columns:
            if index < len(self._tooltips):
                QToolTip.showText(event.globalPosition().toPoint(),
                                  self._tooltips[index], self)
            else:
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"Core {index}: {self._values[index]:.1f}%", self,
                )
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)


class CompositionBar(QWidget):
    """A single stacked bar showing how a total divides into parts.

    Used for memory composition and disk usage, where the relationship between
    segments is the point and separate bars would obscure it.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        height: int = 26,
        show_legend: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._bar_height = height
        self._show_legend = show_legend
        self._segments: list[tuple[str, float, str]] = []
        # The legend sits below the bar and needs the full line height plus the
        # gap above it; a tighter allowance clips the descenders.
        legend_space = (
            theme.metrics.space_2 + int(theme.metrics.font_caption * 2.6)
            if show_legend else 0
        )
        self.setFixedHeight(height + legend_space)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

    def set_segments(self, segments: list[tuple[str, float, str]]) -> None:
        """Set the segments as ``(label, value, colour)`` triples."""
        self._segments = [s for s in segments if s[1] > 0]
        self.update()

    def paintEvent(self, event) -> None:
        """Paint the stacked bar and its legend."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette, metrics = self._theme.palette, self._theme.metrics

        total = sum(value for _, value, _ in self._segments)
        radius = 5.0
        track = QRectF(0, 0, self.width(), self._bar_height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.surface_sunken))
        painter.drawRoundedRect(track, radius, radius)

        if total > 0:
            # Clip to the rounded track so segments inherit its corners; drawing
            # each segment rounded would leave visible notches between them.
            from PySide6.QtGui import QPainterPath

            clip = QPainterPath()
            clip.addRoundedRect(track, radius, radius)
            painter.setClipPath(clip)
            offset = 0.0
            for _label, value, colour in self._segments:
                width = self.width() * value / total
                painter.setBrush(QColor(colour))
                painter.drawRect(QRectF(offset, 0, width + 0.5, self._bar_height))
                offset += width
            painter.setClipping(False)

        if self._show_legend and self._segments:
            font = QFont()
            font.setPointSizeF(metrics.font_caption)
            painter.setFont(font)
            metrics_helper = painter.fontMetrics()
            x = 0.0
            y = self._bar_height + metrics.space_2
            for label, _value, colour in self._segments:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colour))
                painter.drawEllipse(QRectF(x, y + 1, 7, 7))
                x += 11
                painter.setPen(QColor(palette.text_muted))
                painter.drawText(QPointF(x, y + metrics_helper.ascent()), label)
                x += metrics_helper.horizontalAdvance(label) + metrics.space_4
                if x > self.width() - 40:
                    break
        painter.end()
