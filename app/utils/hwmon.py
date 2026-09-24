"""Shared, briefly-cached access to hardware monitoring readings.

Why this exists
---------------
``psutil.sensors_temperatures()`` walks every chip under ``/sys/class/hwmon``,
opening dozens of files.  On the development machine it costs roughly 9 ms --
trivial once, but three separate collectors need it:

* the CPU collector wants the package temperature (1 s cadence),
* the sensors collector wants everything (2 s cadence),
* the storage collector wants NVMe composite temperatures (2 s cadence).

Sampling independently would triple the cost and, worse, mean the temperature
shown on the CPU page could disagree with the one on the sensors page for the
same instant.  A tiny time-to-live cache solves both: collectors that sample
within the same window share one scan and therefore one consistent view.

The TTL is deliberately shorter than the fastest consumer's interval, so a
reading is never served stale enough to be misleading.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import psutil

T = TypeVar("T")

#: Cache lifetime.  Shorter than the 1 s minimum sampling interval.
DEFAULT_TTL = 0.45


class Reading(NamedTuple):
    """One normalised hwmon reading."""

    label: str
    current: float
    high: float | None
    critical: float | None


class HwmonProvider:
    """Time-to-live cache over the psutil sensor entry points.

    Injected as a single instance so that every collector shares one cache.
    Thread-safe: worker threads on different cadences will hit this
    concurrently, and the lock ensures only one of them pays for the scan.
    """

    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}
        #: Resolved sysfs paths per chip, for the targeted fast path.
        self._chip_paths: dict[str, list[tuple[str, Path, Path, Path]] | None] = {}

    def _cached(self, key: str, producer: Callable[[], T], fallback: T) -> T:
        """Return a cached value, refreshing it when older than the TTL."""
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and now - entry[0] < self._ttl:
                return entry[1]  # type: ignore[return-value]
            try:
                value: T = producer()
            except (AttributeError, OSError, RuntimeError):
                # Sensor subsystems can disappear mid-session (a removed USB
                # dock, an unloaded module).  Serve the fallback rather than
                # propagating -- absence is expected, not exceptional.
                value = fallback
            self._cache[key] = (now, value)
            return value

    def temperatures(self) -> dict[str, list[Reading]]:
        """All temperature sensors, grouped by chip name."""
        raw: dict[str, list] = self._cached(
            "temps", lambda: psutil.sensors_temperatures(fahrenheit=False), {}
        )
        return {
            chip: [
                Reading(entry.label or chip, entry.current, entry.high, entry.critical)
                for entry in entries
            ]
            for chip, entries in raw.items()
        }

    def fans(self) -> dict[str, list[Reading]]:
        """All fan sensors, grouped by chip name."""
        raw: dict[str, list] = self._cached("fans", psutil.sensors_fans, {})
        return {
            chip: [Reading(entry.label or chip, float(entry.current), None, None)
                   for entry in entries]
            for chip, entries in raw.items()
        }

    def battery(self) -> Any:
        """The psutil battery tuple, or ``None`` when no battery exists."""
        return self._cached("battery", psutil.sensors_battery, None)

    def chip(self, *names: str) -> list[Reading]:
        """Readings from one named chip, read directly from sysfs.

        This is the fast path, and it exists because the full psutil scan costs
        about 9 ms -- it walks every hwmon chip and opens every sensor file.  A
        collector that wants only the CPU package temperature should not pay for
        the NVMe, battery and Wi-Fi sensors as well.

        The sysfs *paths* are resolved once and cached (hwmon numbering is stable
        for the life of a boot), so subsequent reads are a handful of small file
        reads -- roughly two orders of magnitude cheaper than the full scan.
        Chip names are tried in order, expressing a reliability preference.
        """
        for name in names:
            paths = self._resolve_chip(name)
            if paths is None:
                continue
            readings: list[Reading] = []
            for label, input_path, high_path, crit_path in paths:
                value = _read_milli(input_path)
                if value is None:
                    continue
                readings.append(
                    Reading(
                        label, value,
                        _read_milli(high_path), _read_milli(crit_path),
                    )
                )
            if readings:
                return readings
        return []

    def _resolve_chip(self, name: str) -> list[tuple[str, Path, Path, Path]] | None:
        """Locate and cache the sensor file paths for one hwmon chip."""
        with self._lock:
            if name in self._chip_paths:
                return self._chip_paths[name]

        resolved: list[tuple[str, Path, Path, Path]] | None = None
        for hwmon_dir in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                chip_name = (hwmon_dir / "name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if chip_name != name:
                continue
            entries: list[tuple[str, Path, Path, Path]] = []
            for input_path in sorted(hwmon_dir.glob("temp[0-9]*_input")):
                stem = input_path.name.rsplit("_", 1)[0]
                label_path = hwmon_dir / f"{stem}_label"
                try:
                    label = label_path.read_text(encoding="utf-8").strip()
                except OSError:
                    label = chip_name
                entries.append((
                    label,
                    input_path,
                    hwmon_dir / f"{stem}_max",
                    hwmon_dir / f"{stem}_crit",
                ))
            if entries:
                resolved = entries
                break

        with self._lock:
            self._chip_paths[name] = resolved
        return resolved

    def find(self, *chips: str) -> list[Reading]:
        """Readings from the first of ``chips`` that reports anything.

        Chip names are tried in the order given, which lets callers express a
        reliability preference -- ``find("coretemp", "acpitz")`` prefers the
        on-die sensor over the motherboard proxy.
        """
        temperatures = self.temperatures()
        for chip in chips:
            if readings := temperatures.get(chip):
                return readings
        return []

    def invalidate(self) -> None:
        """Discard the cache, forcing the next read to rescan."""
        with self._lock:
            self._cache.clear()
            self._chip_paths.clear()


def _read_milli(path: Path) -> float | None:
    """Read a hwmon millidegree file as degrees, or ``None`` if unreadable."""
    try:
        return int(path.read_text(encoding="utf-8").strip()) / 1000.0
    except (OSError, ValueError):
        return None
