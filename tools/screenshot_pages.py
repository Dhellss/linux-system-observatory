#!/usr/bin/env python3
"""Render every page to a PNG for visual review.

Runs the real application headlessly against the real machine, visits each page
in turn and saves a screenshot. This is how the interface is reviewed without a
display server, and how several genuine rendering bugs were caught during
development -- an unstyled sidebar, a white chart background on a dark card, a
clipped legend.

Usage::

    QT_QPA_PLATFORM=offscreen python tools/screenshot_pages.py out/ [theme]

Any exception raised while building or updating a page is reported at the end
with its traceback, so this doubles as an integration smoke test.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import bootstrap  # noqa: E402
from app.core.config import ConfigService  # noqa: E402
from app.core.logging_setup import configure  # noqa: E402
from app.services.history import HistoryService  # noqa: E402
from app.services.monitor import MonitorService  # noqa: E402
from app.ui.main_window import PAGE_CLASSES, MainWindow  # noqa: E402
from app.ui.themes.stylesheet import build as build_stylesheet  # noqa: E402
from app.ui.themes.tokens import Theme  # noqa: E402

#: Seconds to let collectors warm up before touring, so pages show real data.
WARMUP_SECONDS = 6.0
#: Seconds between page visits.
STEP_SECONDS = 0.7


def main() -> int:
    """Render every page and report any errors."""
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
    theme_name = sys.argv[2] if len(sys.argv) > 2 else "dark"
    out_dir.mkdir(parents=True, exist_ok=True)

    configure(logging.WARNING)

    # Redirect XDG directories so a review run never touches real settings or
    # the user's metrics database.
    sandbox = tempfile.mkdtemp(prefix="observatory-shots-")
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        os.environ[variable] = sandbox

    application = QApplication(sys.argv[:1])
    container = bootstrap.build_container()
    container.resolve(ConfigService).set(
        "appearance.theme", theme_name, persist=False
    )

    theme = Theme.build(theme_name)
    application.setStyleSheet(build_stylesheet(theme))
    bootstrap.register_collectors(container)
    bootstrap.wire_cross_domain(container)
    container.resolve(HistoryService).start()

    window = MainWindow(bootstrap.build_page_context(container, theme))
    window.resize(1500, 950)
    window.show()

    monitor = container.resolve(MonitorService)
    monitor.start()

    names = [page.__name__ for page in PAGE_CLASSES]
    failures: list[tuple[str, str]] = []
    state = {"index": 0, "warmed": 0.0}

    def step() -> None:
        if state["warmed"] < WARMUP_SECONDS:
            state["warmed"] += STEP_SECONDS
            return
        position = state["index"]
        if position >= len(names):
            application.quit()
            return
        name = names[position]
        try:
            window.navigate(name)
            for _ in range(4):
                application.processEvents()
            window.grab().save(str(out_dir / f"{position:02d}_{name}.png"))
        except Exception:  # noqa: BLE001 - reporting is the whole point
            failures.append((name, traceback.format_exc()))
        state["index"] += 1

    timer = QTimer()
    timer.setInterval(int(STEP_SECONDS * 1000))
    timer.timeout.connect(step)
    timer.start()
    QTimer.singleShot(90_000, application.quit)
    application.exec()
    monitor.stop()

    print(f"Captured {state['index']}/{len(names)} pages into {out_dir}")
    print(
        f"Monitoring cost: {monitor.total_cost_ms_per_second:.0f} ms/s "
        f"({monitor.total_cost_ms_per_second / 10:.1f}% of one core)"
    )
    if failures:
        print(f"\n{len(failures)} PAGE FAILURES")
        for name, tb in failures:
            print(f"\n===== {name} =====\n{tb}")
        return 1
    print("No page errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
