"""Live charts built on pyqtgraph.

Why pyqtgraph rather than QtCharts or custom painting
-----------------------------------------------------
pyqtgraph draws through a ``QGraphicsScene`` backed by NumPy, so updating a
300-point series is a buffer assignment rather than a widget rebuild.  That is what
keeps a page with a dozen live charts at a smooth frame rate.

Performance decisions that matter, learned from making this smooth
-----------------------------------------------------------------
* **Mouse interaction is disabled by default.**  Left enabled, pyqtgraph installs
  hover handlers and recomputes ranges on every mouse move -- a real cost on a
  page of small charts, for an interaction nobody wants on a live sparkline.
* **Auto-ranging is off.**  A y-axis that rescales every frame makes a chart
  impossible to read, so percentage charts are pinned to 0-100 and rate charts
  grow to a rounded headroom figure and then hold.
* **``setData`` replaces the whole series.**  Appending point-by-point forces
  pyqtgraph to re-derive bounds incrementally; a single array assignment is both
  simpler and faster.
* **Downsampling and clipping are enabled**, so off-screen points cost nothing.
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from app.core.timeseries import TimeSeries
from app.ui.themes.tokens import Theme

# Global pyqtgraph configuration.  Applied once at import: antialiasing is a
# per-application setting, and row-major ordering matches how the series arrays
# are built.
pg.setConfigOptions(antialias=True, imageAxisOrder="row-major", useOpenGL=False)


class ByteAxis(pg.AxisItem):
    """A y-axis that labels ticks as byte quantities rather than raw numbers.

    pyqtgraph's default formatter switches to scientific notation past about
    100,000, so a network chart ends up labelled "1.5e+06" -- technically the
    right number and useless to a reader.  Overriding ``tickStrings`` puts real
    units on the axis instead: "1.5 MB/s".
    """

    def __init__(self, *args, per_second: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._per_second = per_second

    def tickStrings(self, values, scale, spacing):
        """Format each tick as a byte figure."""
        from app.core.units import bytes_

        labels = []
        for value in values:
            if value <= 0:
                labels.append("0")
                continue
            text = bytes_(value * scale, precision=0, binary=False)
            labels.append(f"{text}/s" if self._per_second else text)
        return labels


def _pen(colour: str, width: float) -> QPen:
    """Build a cosmetic pen, which keeps line width constant under transforms."""
    pen: QPen = pg.mkPen(colour, width=width)
    pen.setCosmetic(True)
    return pen


class LiveChart(QWidget):
    """A scrolling time-series chart with one or more series.

    The x-axis is fixed to the configured window and data scrolls through it, so
    the chart reads left-to-right as "oldest to now" without the axis labels
    jumping around.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        window_seconds: int = 300,
        y_range: tuple[float, float] | None = (0.0, 100.0),
        y_label: str = "",
        height: int = 150,
        fill: bool = True,
        show_axes: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._window = window_seconds
        self._fixed_range = y_range
        self._fill = fill
        self._series: dict[str, pg.PlotDataItem] = {}
        self._fills: dict[str, pg.FillBetweenItem] = {}
        self._colours: dict[str, str] = {}
        self._auto_ceiling = 1.0

        palette = theme.palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # A byte-rate chart needs unit-aware tick labels; everything else is a
        # plain number and the default axis is correct.
        axis_items = (
            {"left": ByteAxis(orientation="left")}
            if y_label in ("B/s", "bytes") else {}
        )
        self._plot = pg.PlotWidget(parent=self, axisItems=axis_items)
        # pyqtgraph paints its own scene background and does NOT inherit the
        # parent's stylesheet, so passing None leaves the default light grey and
        # a dark-theme card ends up with a glaring white plot area.  The surface
        # colour has to be set explicitly from the palette.
        self._plot.setBackground(palette.surface)
        self._plot.setMinimumHeight(height)
        self._plot.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._plot)

        item = self._plot.getPlotItem()
        item.setMenuEnabled(False)
        item.hideButtons()
        # Disabling mouse interaction removes pyqtgraph's hover and drag handlers,
        # which are pure overhead for a live chart nobody pans.
        item.setMouseEnabled(x=False, y=False)
        item.setClipToView(True)
        item.setDownsampling(auto=True, mode="peak")

        grid_alpha = 0.10 if theme.dark else 0.13
        if theme.metrics and show_axes:
            item.showGrid(x=False, y=True, alpha=grid_alpha)

        for side in ("left", "bottom"):
            axis = item.getAxis(side)
            axis.setPen(_pen(palette.divider, 1))
            axis.setTextPen(_pen(palette.text_subtle, 1))
            axis.setStyle(tickFont=None, tickLength=-3)
            axis.setVisible(show_axes)
        item.getAxis("top").setVisible(False)
        item.getAxis("right").setVisible(False)
        if y_label and y_label not in ("B/s", "bytes"):
            item.getAxis("left").setLabel(y_label, color=palette.text_subtle)
        if show_axes:
            # Relabel the x axis so the negative offsets read as elapsed time
            # rather than as unexplained negative numbers.
            item.getAxis("bottom").setLabel("seconds ago", color=palette.text_subtle)

        # The plot widget also draws a frame of its own; remove it so the card
        # border is the only visible edge.
        self._plot.setFrameShape(pg.QtWidgets.QFrame.Shape.NoFrame)
        item.getViewBox().setBackgroundColor(palette.surface)
        item.setContentsMargins(0, 4, 8, 0)

        item.setXRange(-self._window, 0, padding=0.0)
        if y_range is not None:
            item.setYRange(y_range[0], y_range[1], padding=0.0)
        else:
            item.enableAutoRange(axis="y", enable=False)

    # ------------------------------------------------------------------- series

    def add_series(
        self, key: str, colour: str | None = None, *, width: float | None = None
    ) -> None:
        """Register a series, assigning it the next palette colour if needed."""
        if key in self._series:
            return
        palette = self._theme.palette
        chosen = colour or palette.series[len(self._series) % len(palette.series)]
        line_width = width if width is not None else 1.8

        curve = self._plot.plot(
            [], [], pen=_pen(chosen, line_width), name=key, antialias=True
        )
        self._series[key] = curve
        self._colours[key] = chosen

        # A gradient fill under a single series reads as "volume" and makes a
        # sparse chart legible; with many series it becomes muddy, so it is only
        # applied to the first.
        if self._fill and len(self._series) == 1:
            baseline = self._plot.plot(
                [], [], pen=_pen("transparent", 0)
            )
            colour_object = QColor(chosen)
            colour_object.setAlpha(45)
            area = pg.FillBetweenItem(curve, baseline, brush=colour_object)
            self._plot.addItem(area)
            self._fills[key] = area

    def set_series_colour(self, key: str, colour: str) -> None:
        """Recolour one series."""
        curve = self._series.get(key)
        if curve is None:
            return
        curve.setPen(_pen(colour, 1.8))
        self._colours[key] = colour
        if (area := self._fills.get(key)) is not None:
            filled = QColor(colour)
            filled.setAlpha(45)
            area.setBrush(filled)

    def update_series(self, key: str, series: TimeSeries) -> None:
        """Push the contents of a :class:`TimeSeries` into one chart series."""
        if key not in self._series:
            self.add_series(key)
        times, values = series.snapshot()
        if not times:
            return
        self._series[key].setData(times, values)
        if (area := self._fills.get(key)) is not None:
            # The fill's lower edge must span the same x values as the curve.
            baseline = self._fill_baseline()
            area.curves[1].setData(times, [baseline] * len(times))
        if self._fixed_range is None:
            self._grow_range(values)

    def set_data(self, key: str, times: list[float], values: list[float]) -> None:
        """Push explicit x/y arrays, for charts fed from stored history."""
        if key not in self._series:
            self.add_series(key)
        self._series[key].setData(times, values)
        if self._fixed_range is None and values:
            self._grow_range(values)

    def clear_series(self) -> None:
        """Remove every series from the chart."""
        self._plot.clear()
        self._series.clear()
        self._fills.clear()
        self._colours.clear()

    # -------------------------------------------------------------------- ranges

    def _fill_baseline(self) -> float:
        """The y value the gradient fill descends to."""
        if self._fixed_range is not None:
            return self._fixed_range[0]
        return 0.0

    def _grow_range(self, values: list[float]) -> None:
        """Expand the y-axis to fit new data, without ever shrinking it abruptly.

        A y-axis that tracks the data exactly makes every chart look equally busy
        and hides real changes in magnitude.  Instead the ceiling ratchets up to a
        rounded value and only decays when the data has been well below it for a
        while -- so a network chart that peaked at 10 MB/s keeps that scale for a
        moment rather than instantly re-zooming to a 2 KB/s trickle.
        """
        peak = max(values) if values else 0.0
        if peak > self._auto_ceiling:
            self._auto_ceiling = _round_up(peak * 1.15)
        elif peak < self._auto_ceiling * 0.35:
            self._auto_ceiling = max(_round_up(max(peak * 1.5, 1.0)),
                                     self._auto_ceiling * 0.6)
        self._plot.getPlotItem().setYRange(0, self._auto_ceiling, padding=0.0)

    def set_window(self, seconds: int) -> None:
        """Change the visible time window."""
        self._window = max(10, seconds)
        self._plot.getPlotItem().setXRange(-self._window, 0, padding=0.0)

    def set_y_range(self, low: float, high: float) -> None:
        """Pin the y-axis to an explicit range."""
        self._fixed_range = (low, high)
        self._plot.getPlotItem().setYRange(low, high, padding=0.0)

    def add_threshold(self, value: float, colour: str, label: str = "") -> None:
        """Draw a horizontal reference line, used for temperature limits."""
        line = pg.InfiniteLine(
            pos=value,
            angle=0,
            pen=pg.mkPen(colour, width=1, style=Qt.PenStyle.DashLine),
            label=label or None,
            labelOpts={"color": colour, "position": 0.04, "anchor": (0, 1)},
        )
        self._plot.addItem(line)

    def plot_widget(self) -> pg.PlotWidget:
        """The underlying pyqtgraph widget, for advanced customisation."""
        return self._plot


def _round_up(value: float) -> float:
    """Round a value up to a clean 1/2/5 x power-of-ten step.

    Axis ceilings of 1, 2, 5, 10, 20, 50... produce tick labels people can read
    at a glance, where an arbitrary 17.3 does not.
    """
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if value <= step * base:
            return step * base
    return 10.0 * base


class Sparkline(QWidget):
    """A tiny, axis-free trend line for dashboard cards.

    Hand-painted rather than a pyqtgraph plot: at this size the axes, view box and
    scene graph are pure overhead, and a dashboard may hold a dozen of these.
    """

    def __init__(
        self,
        *,
        theme: Theme,
        colour: str | None = None,
        height: int = 34,
        fill: bool = True,
        y_range: tuple[float, float] | None = (0.0, 100.0),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._colour = QColor(colour or theme.palette.accent)
        self._values: list[float] = []
        self._range = y_range
        self._fill = fill
        self.setFixedHeight(height)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

    def set_values(self, values: list[float]) -> None:
        """Replace the plotted values and repaint."""
        self._values = values
        self.update()

    def set_colour(self, colour: str) -> None:
        """Recolour the line."""
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, event) -> None:
        """Paint the trend line as a single polyline plus an optional fill."""
        if len(self._values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        width = self.width()
        height = self.height()
        low, high = self._range or (min(self._values), max(self._values))
        if high - low < 1e-9:
            high = low + 1.0

        count = len(self._values)
        step = width / max(1, count - 1)
        points = [
            (
                index * step,
                height - 2 - ((value - low) / (high - low)) * (height - 4),
            )
            for index, value in enumerate(self._values)
        ]

        if self._fill:
            from PySide6.QtCore import QPointF
            from PySide6.QtGui import QLinearGradient, QPolygonF

            gradient = QLinearGradient(0, 0, 0, height)
            top = QColor(self._colour)
            top.setAlpha(70)
            bottom = QColor(self._colour)
            bottom.setAlpha(0)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, bottom)
            polygon = QPolygonF(
                [QPointF(0, height), *[QPointF(x, y) for x, y in points],
                 QPointF(width, height)]
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPolygon(polygon)

        pen = QPen(self._colour, 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        from PySide6.QtCore import QPointF

        painter.drawPolyline([QPointF(x, y) for x, y in points])
        painter.end()
