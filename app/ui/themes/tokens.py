"""Design tokens: the single source of truth for the application's appearance.

Why tokens rather than colours sprinkled through widgets
--------------------------------------------------------
Every colour, radius and spacing step is named here once.  Widgets reference
tokens, never literals.  That gives three things: a new theme is a new
:class:`Palette` instance rather than a search-and-replace; charts and stylesheets
provably agree on the accent colour; and the density switch works because spacing
comes from one scale.

The palettes are built on a neutral ramp with a blue accent, in the manner of
modern editor and desktop themes -- deliberately restrained, because a monitoring
tool's colour budget should be spent on *data*, not on chrome.  Semantic colours
(success, warning, danger) are reserved exclusively for status, so when something
turns amber the user knows it means something.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class Palette:
    """A complete colour set for one theme."""

    name: str
    dark: bool

    # Surfaces, from furthest back to nearest front.
    canvas: str            # window background
    surface: str           # card background
    surface_raised: str    # hovered card, menu, popover
    surface_sunken: str    # inset areas: table headers, inputs
    sidebar: str
    border: str
    border_strong: str
    divider: str

    # Text, in descending prominence.
    text: str
    text_muted: str
    text_subtle: str
    text_inverse: str

    # Accent, used for selection, focus and primary actions.
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str       # translucent accent for backgrounds

    # Semantic status colours.  Used only for status.
    success: str
    warning: str
    danger: str
    info: str

    # Chart series palette.  Ordered for maximum adjacent contrast, and chosen to
    # remain distinguishable for the most common forms of colour vision
    # deficiency -- it avoids relying on a red/green distinction.
    series: tuple[str, ...] = field(default_factory=tuple)

    # Per-domain accents, so CPU is consistently one colour across the dashboard,
    # the sidebar and the charts.
    domain_colours: dict[str, str] = field(default_factory=dict)

    def domain(self, name: str) -> str:
        """Accent colour for one monitoring domain."""
        return self.domain_colours.get(name, self.accent)

    def status(self, severity: int) -> str:
        """Colour for a severity level (0 OK, 1 info, 2 warning, 3 critical)."""
        return {
            0: self.success, 1: self.info, 2: self.warning, 3: self.danger
        }.get(severity, self.text_muted)

    def load_colour(self, percent: float) -> str:
        """Colour for a 0-100 utilisation figure.

        The bands are deliberately generous: a machine at 70% is working, not
        ailing, and colouring it amber would cry wolf.
        """
        if percent >= 90:
            return self.danger
        if percent >= 75:
            return self.warning
        return self.accent


_SERIES = (
    "#4a9eff",  # blue
    "#ff9f43",  # amber
    "#4ecdc4",  # teal
    "#c084fc",  # violet
    "#ff6b8a",  # rose
    "#8fd14f",  # lime
    "#ffd93d",  # yellow
    "#5eead4",  # aqua
    "#f4845f",  # coral
    "#a78bfa",  # lavender
    "#38bdf8",  # sky
    "#fb923c",  # orange
)

_DOMAIN_COLOURS = {
    "dashboard": "#4a9eff",
    "cpu": "#4a9eff",
    "memory": "#c084fc",
    "gpu": "#4ecdc4",
    "storage": "#ff9f43",
    "network": "#8fd14f",
    "processes": "#ff6b8a",
    "services": "#5eead4",
    "sensors": "#ffd93d",
    "battery": "#8fd14f",
    "system": "#a78bfa",
    "logs": "#38bdf8",
    "diagnostics": "#f4845f",
    "history": "#a78bfa",
    "alerts": "#fb923c",
    "settings": "#9aa4b2",
}


#: The default theme: a deep neutral dark scheme.
DARK = Palette(
    name="dark",
    dark=True,
    canvas="#0f1115",
    surface="#171a21",
    surface_raised="#1e222b",
    surface_sunken="#12151a",
    sidebar="#13161c",
    border="#252a34",
    border_strong="#333a47",
    divider="#1f242d",
    text="#e6e9ef",
    text_muted="#9aa4b2",
    text_subtle="#6b7482",
    text_inverse="#0f1115",
    accent="#4a9eff",
    accent_hover="#63adff",
    accent_pressed="#3b8ae6",
    accent_soft="rgba(74, 158, 255, 0.14)",
    success="#3fce8f",
    warning="#f5b041",
    danger="#ff5f6d",
    info="#4a9eff",
    series=_SERIES,
    domain_colours=dict(_DOMAIN_COLOURS),
)


#: An even darker variant for OLED displays and low-light use.
MIDNIGHT = replace(
    DARK,
    name="midnight",
    canvas="#08090c",
    surface="#0f1116",
    surface_raised="#161920",
    surface_sunken="#0b0d11",
    sidebar="#0a0c10",
    border="#1c2028",
    border_strong="#2a303a",
    divider="#15181e",
)


#: A light theme for bright environments.
LIGHT = Palette(
    name="light",
    dark=False,
    canvas="#f5f6f8",
    surface="#ffffff",
    surface_raised="#ffffff",
    surface_sunken="#f0f2f5",
    sidebar="#ffffff",
    border="#dfe3e9",
    border_strong="#c4cad3",
    divider="#e9ecf1",
    text="#141821",
    text_muted="#5a6473",
    text_subtle="#8891a0",
    text_inverse="#ffffff",
    accent="#1f6feb",
    accent_hover="#3b82f6",
    accent_pressed="#1a5fd0",
    accent_soft="rgba(31, 111, 235, 0.10)",
    success="#16a34a",
    warning="#d97706",
    danger="#dc2626",
    info="#1f6feb",
    series=(
        "#1f6feb", "#d97706", "#0d9488", "#7c3aed", "#db2777",
        "#65a30d", "#ca8a04", "#0891b2", "#ea580c", "#6d28d9",
        "#0284c7", "#c2410c",
    ),
    domain_colours=dict(_DOMAIN_COLOURS),
)

#: Available themes by name.
PALETTES: dict[str, Palette] = {
    "dark": DARK,
    "midnight": MIDNIGHT,
    "light": LIGHT,
}


@dataclass(frozen=True, slots=True)
class Metrics:
    """Spacing, radius and typography scale.

    A single multiplicative scale (4 px base) is what makes the layout look
    deliberate rather than assembled: every gap is a multiple of the same unit.
    """

    # Spacing scale, in pixels.
    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 20
    space_6: int = 24
    space_8: int = 32

    # Corner radii.
    radius_sm: int = 6
    radius_md: int = 10
    radius_lg: int = 14
    radius_pill: int = 999

    # Type scale, in points.
    font_caption: float = 8.5
    font_body: float = 9.5
    font_subheading: float = 10.5
    font_heading: float = 13.0
    font_display: float = 22.0
    font_metric: float = 26.0

    # Component dimensions.
    sidebar_width: int = 216
    sidebar_collapsed: int = 56
    toolbar_height: int = 46
    statusbar_height: int = 26
    row_height: int = 26
    card_min_width: int = 240

    @property
    def font_family(self) -> str:
        """Preferred UI font stack.

        Listed by likelihood on a Linux desktop, ending in the Qt-provided
        generic so there is always a working fallback.
        """
        return (
            '"Inter", "SF Pro Display", "Segoe UI Variable", "Noto Sans", '
            '"DejaVu Sans", "Cantarell", sans-serif'
        )

    @property
    def mono_family(self) -> str:
        """Monospace stack for numbers, paths and logs.

        Tabular figures matter here: a metric that changes from 9 to 10 must not
        shift the layout, which proportional digits would cause.
        """
        return (
            '"JetBrains Mono", "Fira Code", "Cascadia Mono", "Noto Sans Mono", '
            '"DejaVu Sans Mono", "Liberation Mono", monospace'
        )

    def scaled(self, factor: float) -> Metrics:
        """Return a copy with every type size multiplied by ``factor``."""
        if abs(factor - 1.0) < 0.01:
            return self
        return replace(
            self,
            font_caption=self.font_caption * factor,
            font_body=self.font_body * factor,
            font_subheading=self.font_subheading * factor,
            font_heading=self.font_heading * factor,
            font_display=self.font_display * factor,
            font_metric=self.font_metric * factor,
            row_height=int(self.row_height * factor),
        )

    def compact(self) -> Metrics:
        """A denser variant, for users who prefer more data per screen."""
        return replace(
            self,
            space_2=6, space_3=8, space_4=12, space_5=14, space_6=16, space_8=22,
            row_height=22, toolbar_height=40, sidebar_width=196,
        )


#: Default metrics.
METRICS = Metrics()


@dataclass(frozen=True, slots=True)
class Theme:
    """A palette and metrics pair, passed to every widget that needs styling."""

    palette: Palette
    metrics: Metrics

    @classmethod
    def build(
        cls, name: str = "dark", *, density: str = "comfortable",
        font_scale: float = 1.0,
    ) -> Theme:
        """Construct a theme from user settings."""
        palette = PALETTES.get(name, DARK)
        metrics = METRICS.compact() if density == "compact" else METRICS
        return cls(palette=palette, metrics=metrics.scaled(font_scale))

    @property
    def dark(self) -> bool:
        """True when this is a dark theme."""
        return self.palette.dark
