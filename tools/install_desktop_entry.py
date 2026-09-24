#!/usr/bin/env python3
"""Install a desktop entry so the application appears in the system menu.

Why a script rather than a static ``.desktop`` file
---------------------------------------------------
``packaging/observatory.desktop`` declares ``Exec=observatory``, which is only
correct once the project has been installed with ``pip install .`` and the
console script is on ``PATH``. Someone running from a checkout would get a menu
entry that silently does nothing.

This script writes an entry whose ``Exec`` matches how the application can
actually be launched on *this* machine, preferring the installed console script
and falling back to the checkout's interpreter and ``run.py``.

Installing the entry also resolves the XDG portal warning Qt emits at start-up::

    qt.qpa.services: Failed to register with host portal ...
    Could not register app ID: App info not found for 'observatory'

``main.py`` calls ``setDesktopFileName("observatory")``, so the desktop portal
looks for ``observatory.desktop``; without it, registration fails. The warning
is harmless -- portal registration only affects integrations such as the native
file chooser and screen sharing -- but it is noise on every launch.

Usage::

    python tools/install_desktop_entry.py            # install
    python tools/install_desktop_entry.py --dry-run  # show what would be written
    python tools/install_desktop_entry.py --uninstall
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: Must match ``setDesktopFileName`` in app/main.py, or portal registration fails.
APP_ID = "observatory"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def applications_dir() -> Path:
    """The per-user applications directory, honouring ``XDG_DATA_HOME``."""
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "share"
    return base / "applications"


def launch_command() -> tuple[str, str]:
    """Return ``(exec_line, how)`` describing how to start the application.

    The installed console script is preferred because it survives the checkout
    being moved; the checkout fallback uses absolute paths so the entry works
    regardless of the working directory the menu launches it from.
    """
    installed = shutil.which(APP_ID)
    if installed:
        return installed, "installed console script"

    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    interpreter = venv_python if venv_python.exists() else Path(sys.executable)
    runner = PROJECT_ROOT / "run.py"
    where = "project virtualenv" if venv_python.exists() else "current interpreter"
    return f"{interpreter} {runner}", f"checkout via {where}"


def build_entry(exec_line: str) -> str:
    """Render the desktop entry."""
    return f"""[Desktop Entry]
Type=Application
Version=1.0
Name=Linux System Observatory
GenericName=System Monitor
Comment=Monitor processor, memory, graphics, storage, network and sensors
Exec={exec_line}
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;Utility;
Keywords=system;monitor;process;task;manager;cpu;memory;gpu;disk;network;sensors;
StartupNotify=true
StartupWMClass={APP_ID}
"""


def refresh_menu(directory: Path) -> None:
    """Ask the desktop to re-read its application database, if it can."""
    if not shutil.which("update-desktop-database"):
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["update-desktop-database", str(directory)],
            check=False, capture_output=True, timeout=15,
        )


def main() -> int:
    """Install, preview or remove the desktop entry."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the entry instead of writing it.")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove a previously installed entry.")
    arguments = parser.parse_args()

    target = applications_dir() / f"{APP_ID}.desktop"

    if arguments.uninstall:
        if target.exists():
            target.unlink()
            refresh_menu(target.parent)
            print(f"Removed {target}")
        else:
            print(f"Nothing to remove at {target}")
        return 0

    exec_line, how = launch_command()
    entry = build_entry(exec_line)

    if arguments.dry_run:
        print(f"Would write {target}")
        print(f"Launch method: {how}\n")
        print(entry, end="")
        return 0

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry, encoding="utf-8")
        target.chmod(0o644)
    except OSError as exc:
        print(f"Could not write {target}: {exc}", file=sys.stderr)
        return 1

    refresh_menu(target.parent)
    print(f"Installed {target}")
    print(f"Launch method: {how}")
    print(
        "\nThe application will now appear in your menu, and Qt's "
        "'App info not found' portal warning at start-up will stop."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
