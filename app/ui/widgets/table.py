"""Sortable, filterable table infrastructure.

Qt's model/view framework is used rather than ``QTableWidget`` for one decisive
reason: the process page refreshes several hundred rows every couple of seconds.
``QTableWidget`` would mean destroying and recreating a ``QTableWidgetItem`` per
cell -- tens of thousands of object allocations per refresh, which visibly
stutters and discards the user's selection and scroll position each time.

A model instead keeps a list of row objects and emits ``dataChanged``, so Qt
repaints only the visible rows and selection survives the update.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QWidget

from app.ui.themes.tokens import Theme

T = TypeVar("T")

#: Custom role carrying the raw, unformatted value used for sorting.
#: Qt calls these overrides with an invalid index for top-level items.  A
#: shared instance avoids constructing one in a default argument, which would
#: be evaluated once at import time anyway.
_NO_PARENT = QModelIndex()

SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
#: Custom role carrying the row's underlying object.
OBJECT_ROLE = int(Qt.ItemDataRole.UserRole) + 2


@dataclass(frozen=True)
class Column(Generic[T]):
    """One table column.

    Separating ``value`` from ``display`` is what makes sorting correct: a
    "Memory" column sorts on an integer byte count while displaying "1.4 GiB".
    Sorting the formatted strings would put 900 MiB after 1.4 GiB.
    """

    key: str
    title: str
    #: Extracts the sortable value from a row object.
    value: Callable[[T], Any]
    #: Formats the value for display; defaults to ``str``.
    display: Callable[[Any], str] | None = None
    #: Column width in pixels; 0 means stretch to fill.
    width: int = 110
    #: Text alignment.
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    #: Optional per-row foreground colour.
    colour: Callable[[T], str | None] | None = None
    #: Render this column in the monospace font (for numbers and paths).
    mono: bool = False
    #: Optional tooltip provider.
    tooltip: Callable[[T], str] | None = None

    def text(self, row: T) -> str:
        """Formatted cell text for ``row``."""
        raw = self.value(row)
        if self.display is not None:
            return self.display(raw)
        return "—" if raw is None else str(raw)


class ObjectTableModel(QAbstractTableModel, Generic[T]):
    """A table model over a list of arbitrary row objects."""

    def __init__(
        self, columns: Sequence[Column[T]], theme: Theme,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows: list[T] = []
        self._theme = theme

    # ------------------------------------------------------------ Qt model API

    def rowCount(self, parent: QModelIndex = _NO_PARENT) -> int:
        """Number of rows."""
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = _NO_PARENT) -> int:
        """Number of columns."""
        return 0 if parent.isValid() else len(self._columns)

    def headerData(
        self, section: int, orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        """Column titles."""
        if (
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._columns)
        ):
            return self._columns[section].title
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Cell data for each requested role."""
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = self._columns[index.column()]

        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(row)
        if role == SORT_ROLE:
            value = column.value(row)
            # Mixed types break comparison during a sort, so anything that is not
            # a number is reduced to a lowercase string.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
            return str(value).lower() if value is not None else ""
        if role == OBJECT_ROLE:
            return row
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(column.align)
        if role == Qt.ItemDataRole.ForegroundRole and column.colour is not None:
            colour = column.colour(row)
            return QColor(colour) if colour else None
        if role == Qt.ItemDataRole.FontRole and column.mono:
            from PySide6.QtGui import QFont

            font = QFont()
            font.setFamilies(
                ["JetBrains Mono", "Noto Sans Mono", "DejaVu Sans Mono", "monospace"]
            )
            font.setPointSizeF(self._theme.metrics.font_body)
            return font
        if role == Qt.ItemDataRole.ToolTipRole and column.tooltip is not None:
            return column.tooltip(row)
        return None

    # --------------------------------------------------------------- refreshing

    def set_rows(self, rows: Sequence[T]) -> None:
        """Replace the table contents.

        When the row *count* is unchanged, only ``dataChanged`` is emitted rather
        than a full model reset.  This is the difference between a process table
        that keeps its selection and scroll position across refreshes and one
        that jumps to the top every two seconds.
        """
        if len(rows) == len(self._rows):
            self._rows = list(rows)
            if self._rows:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(self._rows) - 1, len(self._columns) - 1),
                    [Qt.ItemDataRole.DisplayRole, SORT_ROLE,
                     Qt.ItemDataRole.ForegroundRole],
                )
            return
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, index: int) -> T | None:
        """The row object at a model row index."""
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def rows(self) -> list[T]:
        """Every row object."""
        return list(self._rows)

    def columns(self) -> list[Column[T]]:
        """The column definitions."""
        return list(self._columns)


class FilterProxy(QSortFilterProxyModel):
    """Sorts on the raw value and filters across every column."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._needle = ""
        self._predicate: Callable[[Any], bool] | None = None

    def set_search(self, text: str) -> None:
        """Set a free-text filter applied across all columns."""
        self._needle = text.strip().lower()
        self.invalidateFilter()

    def set_predicate(self, predicate: Callable[[Any], bool] | None) -> None:
        """Set a structured filter evaluated against the row object."""
        self._predicate = predicate
        self.invalidateFilter()

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex
    ) -> bool:
        """Accept a row when it satisfies both the predicate and the search."""
        model = self.sourceModel()
        if model is None:
            return True
        if self._predicate is not None:
            row_object = model.index(source_row, 0, source_parent).data(OBJECT_ROLE)
            if row_object is not None and not self._predicate(row_object):
                return False
        if not self._needle:
            return True
        for column in range(model.columnCount()):
            text = model.index(source_row, column, source_parent).data(
                Qt.ItemDataRole.DisplayRole
            )
            if text and self._needle in str(text).lower():
                return True
        return False


class DataTable(QTableView):
    """A configured table view: sortable, alternating rows, sensible sizing."""

    def __init__(
        self,
        columns: Sequence[Column],
        *,
        theme: Theme,
        stretch_column: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model_ = ObjectTableModel(columns, theme, self)
        self.proxy = FilterProxy(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)

        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        # Uniform row heights let Qt skip per-row height calculation, which is a
        # measurable win on a table of several hundred rows.
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(theme.metrics.row_height)
        self.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )

        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsMovable(True)
        for index, column in enumerate(columns):
            if index == stretch_column or column.width == 0:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(
                    index, QHeaderView.ResizeMode.Interactive
                )
                self.setColumnWidth(index, column.width)

    def set_rows(self, rows: Sequence) -> None:
        """Replace the table contents."""
        self.model_.set_rows(rows)

    def selected_object(self):
        """The row object under the current selection, or ``None``."""
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return None
        return indexes[0].data(OBJECT_ROLE)

    def object_at(self, position):
        """The row object at a viewport position, for context menus."""
        index = self.indexAt(position)
        return index.data(OBJECT_ROLE) if index.isValid() else None

    def sort_by(self, column: int, descending: bool = True) -> None:
        """Apply an initial sort order."""
        self.sortByColumn(
            column,
            Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder,
        )
