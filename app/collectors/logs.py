"""Journal reader.

``journalctl --output=json`` is used because the journal's own export format
resolves every field explicitly, handles multi-line messages, and needs no
locale-dependent parsing.  Each line is a self-contained JSON object, so the
output can be consumed incrementally.

Reads are always bounded by a line count and a time window.  An unbounded
``journalctl`` on a machine with months of logs would return hundreds of
megabytes; the viewer pages instead.
"""

from __future__ import annotations

import json
import logging

from app.core.capabilities import CapabilityRegistry
from app.models.base import Reason, Unavailable
from app.models.service import LogEntry, LogPriority
from app.utils.shell import run

_log = logging.getLogger(__name__)

#: Hard ceiling on entries returned in a single query, to bound memory use.
MAX_LINES = 5000


class JournalReader:
    """Queries the systemd journal.

    Not a :class:`~app.collectors.base.Collector`: log reading is user-driven
    (a search, a filter, a page of results) rather than a periodic sample, so it
    has a query interface instead of a ``collect`` method.
    """

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities

    @property
    def available(self) -> bool:
        """True when the journal can be queried."""
        return bool(self._capabilities.journal)

    def unavailable_reason(self) -> Unavailable:
        """Why the journal cannot be read."""
        return self._capabilities.journal.as_unavailable()

    def query(
        self,
        *,
        lines: int = 500,
        priority: LogPriority | None = None,
        unit: str | None = None,
        since: str | None = None,
        search: str | None = None,
        kernel_only: bool = False,
        boot: int | None = 0,
    ) -> tuple[list[LogEntry], str]:
        """Read journal entries matching the given filters.

        Parameters
        ----------
        lines:
            Maximum entries to return, clamped to :data:`MAX_LINES`.
        priority:
            Return entries at this severity *or worse*, matching
            ``journalctl -p`` semantics.
        unit:
            Restrict to one systemd unit.
        since:
            A systemd time expression such as ``"-1h"`` or ``"today"``.
        search:
            Case-insensitive substring filter, applied locally.
        kernel_only:
            Restrict to kernel ring-buffer messages.
        boot:
            Boot offset; ``0`` is the current boot, ``-1`` the previous one, and
            ``None`` spans all boots.

        Returns
        -------
        tuple
            ``(entries, note)`` where ``note`` is empty on success or explains
            the failure.
        """
        if not self.available:
            return [], str(self.unavailable_reason())

        argv = ["journalctl", "--output=json", "--no-pager",
                f"--lines={min(lines, MAX_LINES)}"]
        if boot is not None:
            argv.append(f"--boot={boot}")
        if priority is not None:
            argv.append(f"--priority={int(priority)}")
        if unit:
            argv.extend(["--unit", unit])
        if since:
            argv.extend(["--since", since])
        if kernel_only:
            argv.append("--dmesg")

        result = run(argv, timeout=25.0)
        if not result.stdout:
            message = (result.stderr or "").strip()
            if "No journal files" in message or "not been found" in message:
                return [], "No journal files are accessible to this user"
            if message:
                return [], message
            return [], "The journal returned no entries for these filters"

        entries: list[LogEntry] = []
        needle = search.lower() if search else None
        for line in result.stdout.splitlines():
            entry = self._parse(line)
            if entry is None:
                continue
            if needle and needle not in entry.message.lower():
                continue
            entries.append(entry)

        note = ""
        if not entries:
            note = (
                f"No entries matched {search!r}" if search
                else "No entries matched these filters"
            )
        # journalctl returns oldest-first; the viewer shows newest at the top.
        entries.reverse()
        return entries, note

    def _parse(self, line: str) -> LogEntry | None:
        """Parse one JSON journal record."""
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None

        message = record.get("MESSAGE", "")
        # A binary or truncated message arrives as a list of byte values.
        if isinstance(message, list):
            try:
                message = bytes(message).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                message = "<unprintable message>"
        elif not isinstance(message, str):
            message = str(message)

        return LogEntry(
            timestamp=self._timestamp(record),
            message=message,
            priority=self._priority(record),
            unit=record.get("_SYSTEMD_UNIT") or record.get("UNIT")
            or Unavailable(Reason.NOT_EXPOSED, "not attributed to a unit"),
            identifier=record.get("SYSLOG_IDENTIFIER") or record.get("_COMM"),
            pid=self._int(record.get("_PID")),
            hostname=record.get("_HOSTNAME"),
            cursor=record.get("__CURSOR"),
        )

    @staticmethod
    def _timestamp(record: dict) -> float:
        """Extract the entry's wall-clock time.

        The journal stores microseconds since the epoch as a *string*, and the
        source timestamp is preferred over the receive timestamp because it
        reflects when the event actually happened.
        """
        for key in ("_SOURCE_REALTIME_TIMESTAMP", "__REALTIME_TIMESTAMP"):
            raw = record.get(key)
            if not isinstance(raw, (str, int)):
                continue
            try:
                return int(raw) / 1_000_000.0
            except (ValueError, TypeError):
                continue
        return 0.0

    @staticmethod
    def _priority(record: dict) -> LogPriority:
        """Map the record's syslog priority onto the enum."""
        raw = record.get("PRIORITY")
        if not isinstance(raw, (str, int)):
            return LogPriority.INFO
        try:
            return LogPriority(int(raw))
        except (TypeError, ValueError):
            return LogPriority.INFO

    @staticmethod
    def _int(raw: object) -> int | None:
        """Parse an integer field that the journal stores as a string."""
        if not isinstance(raw, (str, int, float)):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def boots(self, limit: int = 12) -> list[tuple[int, str]]:
        """Recent boots as ``(offset, label)`` pairs for a filter dropdown."""
        if not self.available:
            return []
        result = run(["journalctl", "--list-boots", "--no-pager"], timeout=10.0)
        boots: list[tuple[int, str]] = []
        for line in result.lines[-limit:]:
            fields = line.split()
            if not fields:
                continue
            try:
                offset = int(fields[0])
            except ValueError:
                continue
            label = "Current boot" if offset == 0 else f"{offset} boot(s) ago"
            if len(fields) >= 4:
                label = f"{label} — {fields[2]} {fields[3]}"
            boots.append((offset, label))
        return boots

    def units_with_errors(self, since: str = "-1h") -> dict[str, int]:
        """Count warning-or-worse entries per unit, for the diagnostics page."""
        entries, _ = self.query(
            lines=MAX_LINES, priority=LogPriority.WARNING, since=since
        )
        counts: dict[str, int] = {}
        for entry in entries:
            if isinstance(entry.unit, str):
                counts[entry.unit] = counts.get(entry.unit, 0) + 1
        return counts
