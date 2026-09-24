"""Qt Style Sheet generation from design tokens.

QSS is generated rather than shipped as a static file so that a theme change is a
matter of rebuilding a string from a different :class:`~app.ui.themes.tokens.Palette`
-- no duplicated colour literals, and no possibility of the stylesheet disagreeing
with the colours the charts use.

Qt's stylesheet engine has real limitations that shape what follows:

* It has no variables, hence generation.
* It cannot express box shadows, so depth is conveyed with borders and surface
  steps instead.
* A stylesheet on a parent cascades to children, so selectors are scoped by
  object name or dynamic property to avoid styling things unintentionally.
"""

from __future__ import annotations

from app.ui.themes.tokens import Theme


def build(theme: Theme) -> str:
    """Return the complete application stylesheet for ``theme``."""
    return "\n".join(
        section(theme) for section in (
            _base, _cards, _buttons, _inputs, _tables, _scrollbars,
            _sidebar, _toolbar, _statusbar, _tabs, _menus, _misc,
        )
    )


def _base(theme: Theme) -> str:
    """Window, default text and focus styling."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ---------------------------------------------------------------- foundation */
QWidget {{
    color: {p.text};
    font-family: {m.font_family};
    font-size: {m.font_body}pt;
}}
QMainWindow, QDialog {{
    background: {p.canvas};
}}
QWidget#PageRoot, QWidget#PageScrollArea, QScrollArea#PageScroll > QWidget > QWidget {{
    background: {p.canvas};
}}
QLabel {{
    background: transparent;
}}
QLabel[role="heading"] {{
    font-size: {m.font_heading}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="subheading"] {{
    font-size: {m.font_subheading}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="caption"] {{
    font-size: {m.font_caption}pt;
    color: {p.text_subtle};
}}
QLabel[role="muted"] {{
    color: {p.text_muted};
}}
QLabel[role="metric"] {{
    font-family: {m.mono_family};
    font-size: {m.font_metric}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="mono"] {{
    font-family: {m.mono_family};
}}
QToolTip {{
    background: {p.surface_raised};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_sm}px;
    padding: {m.space_2}px {m.space_3}px;
}}
"""


def _cards(theme: Theme) -> str:
    """Card surfaces, the primary content container."""
    p, m = theme.palette, theme.metrics
    return f"""
/* --------------------------------------------------------------------- cards */
QFrame#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.radius_lg}px;
}}
QFrame#Card[interactive="true"]:hover {{
    border-color: {p.border_strong};
    background: {p.surface_raised};
}}
QFrame#Card[accent="true"] {{
    border-color: {p.accent};
}}
QFrame#CardHeader {{
    background: transparent;
    border: none;
}}
QFrame#Inset {{
    background: {p.surface_sunken};
    border: 1px solid {p.divider};
    border-radius: {m.radius_md}px;
}}
QFrame[role="divider"] {{
    background: {p.divider};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame[role="vdivider"] {{
    background: {p.divider};
    border: none;
    max-width: 1px;
    min-width: 1px;
}}
"""


def _buttons(theme: Theme) -> str:
    """Push buttons and tool buttons."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ------------------------------------------------------------------- buttons */
QPushButton {{
    background: {p.surface_raised};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_sm}px;
    padding: {m.space_2}px {m.space_4}px;
    min-height: 20px;
}}
QPushButton:hover {{
    background: {p.surface};
    border-color: {p.accent};
}}
QPushButton:pressed {{
    background: {p.surface_sunken};
}}
QPushButton:disabled {{
    color: {p.text_subtle};
    border-color: {p.border};
    background: {p.surface};
}}
QPushButton[variant="primary"] {{
    background: {p.accent};
    color: {p.text_inverse};
    border-color: {p.accent};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QPushButton[variant="primary"]:pressed {{
    background: {p.accent_pressed};
}}
QPushButton[variant="danger"] {{
    background: transparent;
    color: {p.danger};
    border-color: {p.danger};
}}
QPushButton[variant="danger"]:hover {{
    background: {p.danger};
    color: {p.text_inverse};
}}
QPushButton[variant="ghost"] {{
    background: transparent;
    border-color: transparent;
    color: {p.text_muted};
}}
QPushButton[variant="ghost"]:hover {{
    background: {p.surface_raised};
    color: {p.text};
}}
QPushButton:checked {{
    background: {p.accent_soft};
    border-color: {p.accent};
    color: {p.accent};
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {m.radius_sm}px;
    padding: {m.space_1}px {m.space_2}px;
    color: {p.text_muted};
}}
QToolButton:hover {{
    background: {p.surface_raised};
    color: {p.text};
    border-color: {p.border};
}}
QToolButton:checked {{
    background: {p.accent_soft};
    color: {p.accent};
    border-color: {p.accent};
}}
"""


def _inputs(theme: Theme) -> str:
    """Text fields, combo boxes, spin boxes, sliders and checkboxes."""
    p, m = theme.palette, theme.metrics
    return f"""
/* -------------------------------------------------------------------- inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p.surface_sunken};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_sm}px;
    padding: {m.space_2}px {m.space_3}px;
    selection-background-color: {p.accent};
    selection-color: {p.text_inverse};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {p.text_subtle};
    background: {p.surface};
}}
QLineEdit#SearchField {{
    border-radius: {m.radius_pill}px;
    padding-left: {m.space_4}px;
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_muted};
    margin-right: {m.space_2}px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface_raised};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_sm}px;
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
    padding: {m.space_1}px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: {p.surface_raised};
    border: none;
    width: 14px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {p.text_muted};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {p.text_muted};
}}
QCheckBox, QRadioButton {{
    spacing: {m.space_2}px;
    color: {p.text};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {p.border_strong};
    background: {p.surface_sunken};
}}
QCheckBox::indicator {{
    border-radius: 4px;
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {p.surface_sunken};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {p.accent};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {p.text};
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {p.accent_hover};
}}
QProgressBar {{
    background: {p.surface_sunken};
    border: none;
    border-radius: {m.radius_sm}px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: {m.radius_sm}px;
}}
"""


def _tables(theme: Theme) -> str:
    """Tables and tree views, used by the process, service and log pages."""
    p, m = theme.palette, theme.metrics
    return f"""
/* -------------------------------------------------------------------- tables */
QTableView, QTreeView, QListView {{
    background: {p.surface};
    alternate-background-color: {p.surface_sunken};
    border: 1px solid {p.border};
    border-radius: {m.radius_md}px;
    gridline-color: {p.divider};
    selection-background-color: {p.accent_soft};
    selection-color: {p.text};
    outline: none;
}}
QTableView::item, QTreeView::item, QListView::item {{
    padding: {m.space_1}px {m.space_2}px;
    border: none;
    min-height: {m.row_height}px;
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {p.accent_soft};
    color: {p.text};
}}
QTableView::item:hover, QTreeView::item:hover, QListView::item:hover {{
    background: {p.surface_raised};
}}
QHeaderView {{
    background: transparent;
    border: none;
}}
QHeaderView::section {{
    background: {p.surface_sunken};
    color: {p.text_muted};
    border: none;
    border-bottom: 1px solid {p.border};
    border-right: 1px solid {p.divider};
    padding: {m.space_2}px {m.space_2}px;
    font-size: {m.font_caption}pt;
    font-weight: 600;
    text-transform: uppercase;
}}
QHeaderView::section:hover {{
    background: {p.surface_raised};
    color: {p.text};
}}
QHeaderView::section:last {{
    border-right: none;
}}
QHeaderView::down-arrow, QHeaderView::up-arrow {{
    width: 0;
    height: 0;
    subcontrol-position: center right;
    subcontrol-origin: padding;
    margin-right: 4px;
}}
QHeaderView::down-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.accent};
}}
QHeaderView::up-arrow {{
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {p.accent};
}}
QTreeView::branch {{
    background: transparent;
}}
QTableCornerButton::section {{
    background: {p.surface_sunken};
    border: none;
}}
"""


def _scrollbars(theme: Theme) -> str:
    """Slim, overlay-style scrollbars."""
    p = theme.palette
    return f"""
/* ---------------------------------------------------------------- scrollbars */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 5px;
    min-height: 28px;
    min-width: 28px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: {p.text_subtle};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
    width: 0;
    border: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
"""


def _sidebar(theme: Theme) -> str:
    """Navigation sidebar."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ------------------------------------------------------------------- sidebar */
QWidget#Sidebar, QWidget#SidebarList {{
    background: {p.sidebar};
}}
QWidget#Sidebar {{
    border-right: 1px solid {p.border};
}}
QWidget#Sidebar QScrollArea, QWidget#Sidebar QScrollArea > QWidget {{
    background: {p.sidebar};
}}
QLabel#SidebarSection {{
    color: {p.text_subtle};
    font-size: {m.font_caption}pt;
    font-weight: 700;
    text-transform: uppercase;
    padding: {m.space_3}px {m.space_4}px {m.space_1}px {m.space_4}px;
}}
QPushButton#NavItem {{
    background: transparent;
    border: none;
    border-radius: {m.radius_sm}px;
    color: {p.text_muted};
    text-align: left;
    padding: {m.space_2}px {m.space_3}px;
    margin: 1px {m.space_2}px;
    font-size: {m.font_body}pt;
}}
QPushButton#NavItem:hover {{
    background: {p.surface_raised};
    color: {p.text};
}}
QPushButton#NavItem:checked {{
    background: {p.accent_soft};
    color: {p.text};
    font-weight: 600;
}}
QLabel#AppTitle {{
    font-size: {m.font_subheading}pt;
    font-weight: 700;
    color: {p.text};
}}
QLabel#NavBadge {{
    background: {p.danger};
    color: {p.text_inverse};
    border-radius: {m.radius_pill}px;
    font-size: {m.font_caption}pt;
    font-weight: 700;
    padding: 1px {m.space_2}px;
}}
"""


def _toolbar(theme: Theme) -> str:
    """Top toolbar."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ------------------------------------------------------------------- toolbar */
QWidget#Toolbar {{
    background: {p.surface};
    border-bottom: 1px solid {p.border};
}}
QLabel#PageTitle {{
    font-size: {m.font_heading}pt;
    font-weight: 600;
    color: {p.text};
}}
QLabel#PageSubtitle {{
    font-size: {m.font_caption}pt;
    color: {p.text_subtle};
}}
"""


def _statusbar(theme: Theme) -> str:
    """Bottom status bar."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ----------------------------------------------------------------- statusbar */
QStatusBar {{
    background: {p.sidebar};
    border-top: 1px solid {p.border};
    color: {p.text_muted};
    font-size: {m.font_caption}pt;
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    color: {p.text_muted};
    font-size: {m.font_caption}pt;
    padding: 0 {m.space_2}px;
}}
"""


def _tabs(theme: Theme) -> str:
    """Tab widgets, used within detail pages."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ---------------------------------------------------------------------- tabs */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: {m.radius_md}px;
    background: {p.surface};
    top: -1px;
}}
QTabBar {{
    background: transparent;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_muted};
    border: none;
    border-bottom: 2px solid transparent;
    padding: {m.space_2}px {m.space_4}px;
    margin-right: {m.space_1}px;
}}
QTabBar::tab:hover {{
    color: {p.text};
}}
QTabBar::tab:selected {{
    color: {p.accent};
    border-bottom-color: {p.accent};
    font-weight: 600;
}}
QSplitter::handle {{
    background: {p.border};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
}}
QSplitter::handle:hover {{
    background: {p.accent};
}}
"""


def _menus(theme: Theme) -> str:
    """Context menus and the command palette popup."""
    p, m = theme.palette, theme.metrics
    return f"""
/* --------------------------------------------------------------------- menus */
QMenu {{
    background: {p.surface_raised};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_md}px;
    padding: {m.space_1}px;
}}
QMenu::item {{
    padding: {m.space_2}px {m.space_5}px {m.space_2}px {m.space_3}px;
    border-radius: {m.radius_sm}px;
    color: {p.text};
}}
QMenu::item:selected {{
    background: {p.accent_soft};
}}
QMenu::item:disabled {{
    color: {p.text_subtle};
}}
QMenu::separator {{
    height: 1px;
    background: {p.divider};
    margin: {m.space_1}px {m.space_2}px;
}}
QMenu::icon {{
    padding-left: {m.space_2}px;
}}
QWidget#CommandPalette {{
    background: {p.surface_raised};
    border: 1px solid {p.border_strong};
    border-radius: {m.radius_lg}px;
}}
QListWidget#CommandList {{
    background: transparent;
    border: none;
}}
QListWidget#CommandList::item {{
    padding: {m.space_2}px {m.space_3}px;
    border-radius: {m.radius_sm}px;
    color: {p.text};
}}
QListWidget#CommandList::item:selected {{
    background: {p.accent_soft};
    color: {p.text};
}}
"""


def _misc(theme: Theme) -> str:
    """Badges, banners and other small components."""
    p, m = theme.palette, theme.metrics
    return f"""
/* ---------------------------------------------------------- small components */
QLabel#Badge {{
    border-radius: {m.radius_pill}px;
    padding: 2px {m.space_2}px;
    font-size: {m.font_caption}pt;
    font-weight: 600;
}}
QFrame#Banner {{
    border-radius: {m.radius_md}px;
    border: 1px solid {p.border_strong};
    background: {p.surface_raised};
}}
QGroupBox {{
    border: 1px solid {p.border};
    border-radius: {m.radius_md}px;
    margin-top: {m.space_4}px;
    padding-top: {m.space_4}px;
    background: {p.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: {m.space_4}px;
    padding: 0 {m.space_2}px;
    color: {p.text_muted};
    font-size: {m.font_caption}pt;
    font-weight: 700;
    text-transform: uppercase;
}}
"""
