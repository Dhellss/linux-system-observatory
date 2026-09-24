"""Application logging configuration.

A monitoring tool that swallows its own errors is untrustworthy, so logging is
configured early and writes to both the console and a rotating file under
``$XDG_STATE_HOME``.  The file handler is capped so an all-night session with a
misbehaving sensor cannot fill the user's disk -- which would be a particularly
ironic failure mode for a disk-space monitor.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from app.core.paths import log_file

_CONSOLE_FORMAT = "%(levelname)-7s %(name)-34s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-34s %(message)s"

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3


def configure(level: int = logging.INFO, *, quiet_libraries: bool = True) -> None:
    """Install console and rotating-file handlers on the root logger.

    Idempotent: calling twice will not duplicate handlers, which matters because
    tests construct the application repeatedly in one process.
    """
    root = logging.getLogger()
    if getattr(root, "_observatory_configured", False):
        root.setLevel(level)
        return

    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - depends on filesystem state
        root.warning("File logging disabled: %s", exc)

    if quiet_libraries:
        # pyqtgraph is chatty about OpenGL fallbacks that we handle deliberately.
        logging.getLogger("pyqtgraph").setLevel(logging.WARNING)
        logging.getLogger("OpenGL").setLevel(logging.ERROR)

    root._observatory_configured = True  # type: ignore[attr-defined]
