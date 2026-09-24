"""Application entry point.

Start-up sequence, in order and for a reason:

1. Configure logging, so anything that fails afterwards is recorded.
2. Create the ``QApplication`` -- required before any widget exists.
3. Build the service container, which probes capabilities once.
4. Register collectors and start the scheduler.
5. Build and show the window.

The window is shown before the first samples arrive, so the application appears
immediately rather than after a second of collection.  Pages render their empty
states until data lands, which is why every page has one.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app import bootstrap
from app.core.config import ConfigService
from app.core.logging_setup import configure
from app.services.history import HistoryService
from app.services.monitor import MonitorService

_log = logging.getLogger(__name__)

APPLICATION_NAME = "Linux System Observatory"
ORGANISATION = "observatory"
VERSION = "1.0.0"


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="observatory",
        description=(
            "A comprehensive Linux system monitor. Everything stays on this "
            "machine: no telemetry, no analytics, no network access."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{APPLICATION_NAME} {VERSION}"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable verbose logging to the console.",
    )
    parser.add_argument(
        "--page", metavar="NAME", default=None,
        help="Open a specific page at start-up, e.g. --page CpuPage.",
    )
    parser.add_argument(
        "--database", metavar="PATH", default=None,
        help="Use an alternative history database.",
    )
    parser.add_argument(
        "--no-history", action="store_true",
        help="Run without writing any metrics to disk.",
    )
    parser.add_argument(
        "--theme", choices=("dark", "light", "midnight"), default=None,
        help="Override the configured theme for this run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the application, returning a process exit code."""
    arguments = parse_arguments(argv)
    configure(logging.DEBUG if arguments.debug else logging.INFO)
    _log.info("%s %s starting", APPLICATION_NAME, VERSION)

    application = QApplication(sys.argv[:1])
    application.setApplicationName(APPLICATION_NAME)
    application.setApplicationDisplayName(APPLICATION_NAME)
    application.setOrganizationName(ORGANISATION)
    application.setApplicationVersion(VERSION)
    application.setDesktopFileName("observatory")
    # Qt's default is to quit when the last window closes, which is what we want;
    # stating it explicitly guards against a stray dialog changing the behaviour.
    application.setQuitOnLastWindowClosed(True)

    container = bootstrap.build_container(database_path=arguments.database)
    config = container.resolve(ConfigService)

    if arguments.no_history:
        config.set("history.enabled", False, persist=False)
    if arguments.theme:
        config.set("appearance.theme", arguments.theme, persist=False)

    capabilities = container.resolve(bootstrap.CapabilityRegistry)
    _log.info("Capability probe: %s", capabilities.summary())

    theme = _build_theme(config)
    from app.ui.themes.stylesheet import build as build_stylesheet

    application.setStyleSheet(build_stylesheet(theme))
    application.setWindowIcon(_application_icon(theme))

    bootstrap.register_collectors(container)
    bootstrap.wire_cross_domain(container)

    history = container.resolve(HistoryService)
    history.start()

    from app.ui.main_window import MainWindow

    context = bootstrap.build_page_context(container, theme)
    window = MainWindow(context)
    window.show()

    if arguments.page:
        window.navigate(arguments.page)

    monitor = container.resolve(MonitorService)
    # Start sampling on the next event-loop turn so the window paints first.
    QTimer.singleShot(0, monitor.start)

    _install_signal_handlers(application)
    _log.info("Start-up complete")

    try:
        code = int(application.exec())
    finally:
        _log.info("Shutting down")
        monitor.close()
        container.dispose()
    return code


def _build_theme(config: ConfigService):
    """Construct the theme from persisted appearance settings."""
    from app.ui.themes.tokens import Theme

    appearance = config.settings.appearance
    return Theme.build(
        appearance.theme,
        density=appearance.density,
        font_scale=appearance.font_scale,
    )


def _application_icon(theme) -> QIcon:
    """Render the window icon.

    Drawn at run time from the theme's accent colour rather than shipped as a
    file, so the icon always matches the active theme and the project needs no
    binary assets.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap

    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(theme.palette.surface))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    pen_colour = QColor(theme.palette.accent)
    painter.setPen(pen_colour)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    from PySide6.QtGui import QPen

    painter.setPen(QPen(pen_colour, 6))
    painter.drawArc(QRectF(14, 14, 36, 36), 60 * 16, 250 * 16)
    painter.setBrush(pen_colour)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QRectF(27, 27, 10, 10))
    painter.end()
    return QIcon(pixmap)


def _install_signal_handlers(application: QApplication) -> None:
    """Make Ctrl+C in a terminal quit cleanly.

    Python's signal handlers only run between bytecodes, and a Qt event loop
    blocks in C for long stretches.  A periodic no-op timer gives the interpreter
    regular opportunities to notice the signal -- the standard remedy.
    """
    signal.signal(signal.SIGINT, lambda *_: application.quit())
    signal.signal(signal.SIGTERM, lambda *_: application.quit())
    timer = QTimer(application)
    timer.setInterval(400)
    timer.timeout.connect(lambda: None)
    timer.start()


if __name__ == "__main__":
    sys.exit(main())
