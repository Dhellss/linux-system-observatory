"""Data access for the history database.

Repositories keep SQL out of the service layer.  Each exposes intention-revealing
methods (``record_batch``, ``series``, ``prune``) rather than a generic query
interface, so the storage shape can change without touching callers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.database.connection import Database
from app.models.diagnostics import AlertEvent, Severity

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One historical sample."""

    recorded: float
    value: float


@dataclass(frozen=True, slots=True)
class StoredAlert:
    """An alert firing read back from the database."""

    occurred: float
    rule_id: str
    label: str
    metric: str
    value: float
    threshold: float
    severity: Severity
    active: bool


class MetricRepository:
    """Reads and writes the ``metrics`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_batch(self, samples: list[tuple[float, str, float]]) -> int:
        """Insert many samples in one transaction.

        Batching is essential: one transaction per metric per second would mean
        thousands of fsyncs a minute.  The history service accumulates samples in
        memory and calls this on a flush interval instead.

        Returns the number of rows written (0 when persistence is disabled).
        """
        if self._database.degraded or not samples:
            return 0
        try:
            with self._database.transaction() as connection:
                connection.executemany(
                    "INSERT INTO metrics (recorded, metric, value) VALUES (?, ?, ?)",
                    samples,
                )
        except Exception:
            _log.exception("Failed to persist %d metric samples", len(samples))
            return 0
        return len(samples)

    def series(
        self, metric: str, *, since: float | None = None, limit: int = 5000
    ) -> list[MetricPoint]:
        """Read one metric's history, oldest first."""
        window = since if since is not None else 0.0
        rows = self._database.execute(
            "SELECT recorded, value FROM metrics "
            "WHERE metric = ? AND recorded >= ? "
            "ORDER BY recorded ASC LIMIT ?",
            (metric, window, limit),
        )
        return [MetricPoint(row["recorded"], row["value"]) for row in rows]

    def summary(self, metric: str, *, since: float | None = None) -> dict[str, float]:
        """Aggregate statistics for one metric over a window."""
        window = since if since is not None else 0.0
        rows = self._database.execute(
            "SELECT MIN(value) AS low, MAX(value) AS high, AVG(value) AS mean, "
            "COUNT(*) AS samples FROM metrics WHERE metric = ? AND recorded >= ?",
            (metric, window),
        )
        if not rows or rows[0]["samples"] == 0:
            return {}
        row = rows[0]
        return {
            "minimum": row["low"],
            "maximum": row["high"],
            "mean": row["mean"],
            "samples": row["samples"],
        }

    def metrics(self) -> list[str]:
        """Every distinct metric name present in the database."""
        rows = self._database.execute(
            "SELECT DISTINCT metric FROM metrics ORDER BY metric"
        )
        return [row["metric"] for row in rows]

    def bounds(self) -> tuple[float, float] | None:
        """Oldest and newest sample timestamps, or ``None`` when empty."""
        rows = self._database.execute(
            "SELECT MIN(recorded) AS oldest, MAX(recorded) AS newest FROM metrics"
        )
        if not rows or rows[0]["oldest"] is None:
            return None
        return rows[0]["oldest"], rows[0]["newest"]

    def count(self) -> int:
        """Total number of stored samples."""
        rows = self._database.execute("SELECT COUNT(*) AS total FROM metrics")
        return int(rows[0]["total"]) if rows else 0

    def prune(self, older_than_days: float) -> int:
        """Delete samples older than the retention window.

        Returns the number of rows removed, so the caller can decide whether the
        deletion was large enough to justify a vacuum.
        """
        if self._database.degraded:
            return 0
        cutoff = time.time() - older_than_days * 86_400
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM metrics WHERE recorded < ?", (cutoff,)
                )
                return cursor.rowcount or 0
        except Exception:
            _log.exception("Retention prune failed")
            return 0

    def trim_to_newest(self, keep: int) -> int:
        """Emergency trim that keeps only the newest ``keep`` samples.

        Used when the database exceeds its configured size limit despite
        time-based retention -- for instance after the user lowered the limit.
        """
        if self._database.degraded:
            return 0
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM metrics WHERE id NOT IN "
                    "(SELECT id FROM metrics ORDER BY id DESC LIMIT ?)",
                    (keep,),
                )
                return cursor.rowcount or 0
        except Exception:
            _log.exception("Size-based trim failed")
            return 0


class AlertRepository:
    """Reads and writes the ``alert_events`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record(self, event: AlertEvent) -> None:
        """Persist one alert transition."""
        if self._database.degraded:
            return
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    "INSERT INTO alert_events "
                    "(occurred, rule_id, label, metric, value, threshold, "
                    " severity, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.timestamp,
                        event.rule.rule_id,
                        event.rule.label or event.rule.metric,
                        event.rule.metric,
                        event.value,
                        event.rule.threshold,
                        int(event.rule.severity),
                        1 if event.active else 0,
                    ),
                )
        except Exception:
            _log.exception("Failed to record alert event")

    def recent(self, limit: int = 200) -> list[StoredAlert]:
        """Most recent alert transitions, newest first."""
        rows = self._database.execute(
            "SELECT occurred, rule_id, label, metric, value, threshold, "
            "severity, active FROM alert_events ORDER BY occurred DESC LIMIT ?",
            (limit,),
        )
        return [
            StoredAlert(
                occurred=row["occurred"],
                rule_id=row["rule_id"],
                label=row["label"],
                metric=row["metric"],
                value=row["value"],
                threshold=row["threshold"],
                severity=Severity(row["severity"]),
                active=bool(row["active"]),
            )
            for row in rows
        ]

    def prune(self, older_than_days: float) -> int:
        """Delete alert history older than the retention window."""
        if self._database.degraded:
            return 0
        cutoff = time.time() - older_than_days * 86_400
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM alert_events WHERE occurred < ?", (cutoff,)
                )
                return cursor.rowcount or 0
        except Exception:
            return 0

    def clear(self) -> None:
        """Remove every recorded alert."""
        if self._database.degraded:
            return
        try:
            with self._database.transaction() as connection:
                connection.execute("DELETE FROM alert_events")
        except Exception:
            _log.exception("Failed to clear alert history")


class SessionRepository:
    """Reads and writes named recording sessions."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def start(self, name: str, note: str = "") -> int | None:
        """Begin a session and return its identifier."""
        if self._database.degraded:
            return None
        try:
            with self._database.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO sessions (name, started, note) VALUES (?, ?, ?)",
                    (name, time.time(), note),
                )
                return cursor.lastrowid
        except Exception:
            _log.exception("Failed to start recording session")
            return None

    def finish(self, session_id: int) -> None:
        """Mark a session as ended."""
        if self._database.degraded:
            return
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    "UPDATE sessions SET ended = ? WHERE id = ?",
                    (time.time(), session_id),
                )
        except Exception:
            _log.exception("Failed to finish recording session")

    def all(self) -> list[dict]:
        """Every session, newest first."""
        rows = self._database.execute(
            "SELECT id, name, started, ended, note FROM sessions "
            "ORDER BY started DESC"
        )
        return [dict(row) for row in rows]

    def delete(self, session_id: int) -> None:
        """Remove one session record."""
        if self._database.degraded:
            return
        try:
            with self._database.transaction() as connection:
                connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        except Exception:
            _log.exception("Failed to delete session")
