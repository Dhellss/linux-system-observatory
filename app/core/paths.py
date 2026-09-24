"""XDG-compliant filesystem locations.

Respecting the XDG Base Directory specification is what separates an application
that belongs on a Linux desktop from one that scatters dotfiles in ``$HOME``.
Configuration, state and cache are kept strictly apart so that a user can back
up their settings without dragging along a multi-megabyte metrics database.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

APP_DIRNAME = "linux-system-observatory"


def _xdg(env_var: str, default: str) -> Path:
    """Resolve an XDG directory, honouring the environment variable if set."""
    raw = os.environ.get(env_var, "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / default
    return base / APP_DIRNAME


@lru_cache(maxsize=1)
def config_dir() -> Path:
    """``$XDG_CONFIG_HOME`` -- user preferences and dashboard layouts."""
    return _ensure(_xdg("XDG_CONFIG_HOME", ".config"))


@lru_cache(maxsize=1)
def data_dir() -> Path:
    """``$XDG_DATA_HOME`` -- the metrics history database."""
    return _ensure(_xdg("XDG_DATA_HOME", ".local/share"))


@lru_cache(maxsize=1)
def cache_dir() -> Path:
    """``$XDG_CACHE_HOME`` -- regenerable data such as hardware inventories."""
    return _ensure(_xdg("XDG_CACHE_HOME", ".cache"))


@lru_cache(maxsize=1)
def state_dir() -> Path:
    """``$XDG_STATE_HOME`` -- window geometry and log files."""
    return _ensure(_xdg("XDG_STATE_HOME", ".local/state"))


def _ensure(path: Path) -> Path:
    """Create ``path`` if possible; fall back to a temp dir if the disk is full."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile

        path = Path(tempfile.gettempdir()) / APP_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    """Path to the JSON settings document."""
    return config_dir() / "settings.json"


def database_file() -> Path:
    """Path to the SQLite metrics history database."""
    return data_dir() / "history.db"


def log_file() -> Path:
    """Path to the rotating application log."""
    return _ensure(state_dir() / "logs") / "observatory.log"


def export_dir() -> Path:
    """Default destination offered in export dialogs."""
    candidate = Path.home() / "Documents"
    return candidate if candidate.is_dir() else Path.home()
