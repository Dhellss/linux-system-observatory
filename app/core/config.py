"""Typed, observable user settings backed by a JSON document.

Why a hand-rolled store instead of ``QSettings``?
-------------------------------------------------
Three reasons.  A plain JSON file under ``$XDG_CONFIG_HOME`` is inspectable and
editable by the user, which is expected of Linux software.  It keeps the settings
layer free of any Qt dependency, so collectors and services can read
configuration without importing a GUI toolkit.  And writes are atomic
(write-to-temp then ``os.replace``), so a crash mid-save cannot leave the user
with a truncated configuration.

Change notification is provided through a small callback list rather than Qt
signals for the same reason -- the UI adapts this into signals at its own
boundary.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, cast, get_type_hints

from app.core.paths import settings_file

_log = logging.getLogger(__name__)

#: Signature of a settings-change listener: ``(dotted_key, new_value)``.
Listener = Callable[[str, Any], None]


@dataclass
class SamplingSettings:
    """Polling cadences, in seconds, for each monitoring domain.

    Separate intervals are the single most effective performance lever in the
    application: process enumeration costs two orders of magnitude more than
    reading ``/proc/stat``, so they must not share a cadence.
    """

    cpu: float = 1.0
    memory: float = 1.0
    gpu: float = 1.5
    storage: float = 2.0
    network: float = 1.0
    processes: float = 2.5
    sensors: float = 2.0
    battery: float = 10.0
    services: float = 30.0
    smart: float = 120.0
    system: float = 300.0

    def for_domain(self, name: str) -> float:
        """Interval for ``name``, defaulting to two seconds if unknown."""
        return float(getattr(self, name, 2.0))


@dataclass
class ChartSettings:
    """Appearance and retention of live charts."""

    history_seconds: int = 300
    line_width: float = 1.8
    fill_opacity: int = 45
    antialias: bool = True
    show_grid: bool = True
    smooth_curves: bool = True


@dataclass
class HistorySettings:
    """Long-term persistence of metrics to SQLite."""

    enabled: bool = True
    #: How often aggregated samples are flushed to disk.
    flush_seconds: float = 15.0
    #: Retention window; older rows are pruned at start-up and daily thereafter.
    retention_days: int = 14
    #: Hard ceiling on database size; pruning becomes aggressive beyond this.
    max_megabytes: int = 256


@dataclass
class AppearanceSettings:
    """Theme and density preferences."""

    theme: str = "dark"          # "dark" | "light" | "midnight"
    accent: str = "blue"         # named accent from the theme's palette
    density: str = "comfortable"  # "compact" | "comfortable"
    font_scale: float = 1.0
    animations: bool = True
    show_status_bar: bool = True


@dataclass
class NotificationSettings:
    """Alert delivery preferences."""

    desktop_notifications: bool = True
    in_app_banners: bool = True
    sound: bool = False
    #: Minimum seconds between repeat notifications for the same alert rule.
    cooldown_seconds: float = 300.0


@dataclass
class PrivacySettings:
    """Explicit, user-visible confirmation that nothing leaves the machine.

    Every field defaults to the most private option.  The network probes are the
    only features in the entire application that touch a remote host, and they
    are opt-in and manually triggered.
    """

    allow_latency_probes: bool = False
    latency_target: str = "1.1.1.1"
    redact_command_lines: bool = False
    store_process_history: bool = False


@dataclass
class Settings:
    """Root settings document."""

    sampling: SamplingSettings = field(default_factory=SamplingSettings)
    charts: ChartSettings = field(default_factory=ChartSettings)
    history: HistorySettings = field(default_factory=HistorySettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)

    #: Free-form UI state: window geometry, last page, dashboard layouts.
    ui_state: dict[str, Any] = field(default_factory=dict)
    #: Persisted alert rule definitions (see :mod:`app.services.alerts`).
    alert_rules: list[dict[str, Any]] = field(default_factory=list)


class ConfigService:
    """Loads, mutates, persists and broadcasts :class:`Settings`.

    Access is by dotted path so that callers -- including generic settings
    widgets -- need no knowledge of the dataclass structure::

        config.get("charts.history_seconds")
        config.set("appearance.theme", "light")
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings_file()
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._settings = self._load()

    # --------------------------------------------------------------- properties

    @property
    def settings(self) -> Settings:
        """The live settings object.  Mutate through :meth:`set`, not directly."""
        return self._settings

    @property
    def path(self) -> Path:
        """Location of the backing JSON document."""
        return self._path

    # ------------------------------------------------------------------ loading

    def _load(self) -> Settings:
        """Read settings from disk, falling back to defaults on any problem.

        Unknown keys are ignored rather than fatal, so a file written by a newer
        version of the application still loads in an older one.
        """
        if not self._path.exists():
            return Settings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("Could not read %s (%s); using defaults", self._path, exc)
            return Settings()
        try:
            return cast("Settings", _from_dict(Settings, raw))
        except Exception:
            _log.exception("Malformed settings document; using defaults")
            return Settings()

    def save(self) -> None:
        """Persist settings atomically."""
        with self._lock:
            payload = json.dumps(asdict(self._settings), indent=2, sort_keys=True)
        temp = self._path.with_suffix(".json.tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self._path)
        except OSError as exc:
            _log.error("Failed to save settings: %s", exc)
            temp.unlink(missing_ok=True)

    def reset(self) -> None:
        """Restore factory defaults and notify listeners."""
        with self._lock:
            self._settings = Settings()
        self.save()
        self._notify("*", None)

    # ------------------------------------------------------------------- access

    def get(self, dotted: str, default: Any = None) -> Any:
        """Read a value by dotted path, e.g. ``"sampling.cpu"``."""
        node: Any = self._settings
        for part in dotted.split("."):
            if is_dataclass(node) and hasattr(node, part):
                node = getattr(node, part)
            elif isinstance(node, dict):
                if part not in node:
                    return default
                node = node[part]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any, *, persist: bool = True) -> None:
        """Write a value by dotted path and notify listeners.

        No-ops when the value is unchanged, which prevents feedback loops when a
        widget both renders and edits the same setting.
        """
        parts = dotted.split(".")
        with self._lock:
            node: Any = self._settings
            for part in parts[:-1]:
                if is_dataclass(node) and hasattr(node, part):
                    node = getattr(node, part)
                elif isinstance(node, dict):
                    node = node.setdefault(part, {})
                else:
                    _log.warning("Cannot set unknown settings path %r", dotted)
                    return
            leaf = parts[-1]
            if is_dataclass(node):
                if not hasattr(node, leaf):
                    _log.warning("Unknown settings key %r", dotted)
                    return
                if getattr(node, leaf) == value:
                    return
                setattr(node, leaf, value)
            elif isinstance(node, dict):
                if node.get(leaf) == value:
                    return
                node[leaf] = value
            else:
                return
        if persist:
            self.save()
        self._notify(dotted, value)

    # ------------------------------------------------------------ notifications

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register ``listener``; returns a callable that unsubscribes it."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, key: str, value: Any) -> None:
        """Invoke every listener, isolating failures so one cannot block others."""
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(key, value)
            except Exception:
                _log.exception("Settings listener raised for %s", key)

    def close(self) -> None:
        """Flush settings to disk (invoked by the DI container at shutdown)."""
        self.save()


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Reconstruct a nested dataclass tree from a mapping.

    Unknown keys in ``data`` are ignored and missing keys keep their defaults,
    which together give forward and backward compatibility across versions.
    Type hints are resolved with :func:`typing.get_type_hints` because
    ``from __future__ import annotations`` leaves ``field.type`` as a string.
    """
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for spec in fields(cls):
        if spec.name not in data:
            continue
        declared = hints.get(spec.name)
        raw = data[spec.name]
        if is_dataclass(declared) and isinstance(raw, dict):
            kwargs[spec.name] = _from_dict(declared, raw)  # type: ignore[arg-type]
        else:
            kwargs[spec.name] = raw
    return cls(**kwargs)
