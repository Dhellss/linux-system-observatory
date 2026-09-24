"""Tolerant readers for ``/sys`` and ``/proc`` pseudo-files.

These filesystems are the richest source of Linux hardware telemetry, but they
are also littered with files that vanish mid-read (a removed USB device), return
``EACCES`` for unprivileged users, or contain ``-`` / ``unknown`` placeholders.
Every helper here answers with ``None`` instead of raising, so collectors read
as straight-line code rather than a thicket of ``try`` blocks.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)

PROC = Path("/proc")
SYS = Path("/sys")

#: Placeholder strings the kernel and firmware vendors use for "no value".
_PLACEHOLDERS = frozenset(
    {"", "-", "n/a", "na", "unknown", "none", "to be filled by o.e.m.",
     "to be filled by o.e.m", "default string", "system manufacturer",
     "not specified", "not available", "0x0000"}
)


def read_text(path: Path | str) -> str | None:
    """Read a pseudo-file, returning ``None`` on any failure or placeholder."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeError):
        return None
    return None if raw.lower() in _PLACEHOLDERS else raw


def read_int(path: Path | str, *, scale: float = 1.0) -> float | None:
    """Read an integer pseudo-file, optionally dividing by ``scale``.

    ``scale`` covers the kernel's habit of exposing values in inconvenient
    units -- microvolts, millidegrees, kilohertz.
    """
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw, 0) / scale
    except ValueError:
        return None


def read_lines(path: Path | str) -> list[str]:
    """Read a pseudo-file as a list of stripped, non-empty lines."""
    raw = read_text(path)
    return [ln.strip() for ln in raw.splitlines() if ln.strip()] if raw else []


def read_first_line(path: Path | str) -> str | None:
    """Read only the first line of a pseudo-file."""
    lines = read_lines(path)
    return lines[0] if lines else None


def glob(pattern: str, *, root: Path = SYS) -> list[Path]:
    """Sorted glob under ``root`` that tolerates permission errors."""
    try:
        return sorted(root.glob(pattern))
    except (OSError, ValueError):
        return []


def exists(path: Path | str) -> bool:
    """Existence check that never raises."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def read_keyed_file(path: Path | str, separator: str = ":") -> dict[str, str]:
    """Parse a ``key<sep>value`` pseudo-file such as ``/proc/meminfo``.

    Returns an empty mapping if the file is unreadable.
    """
    result: dict[str, str] = {}
    for line in read_lines(path):
        key, _, value = line.partition(separator)
        if _:
            result[key.strip()] = value.strip()
    return result
