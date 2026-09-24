"""In-memory chart buffers plus write-behind persistence to SQLite.

Two tiers, because charts and history have different needs
----------------------------------------------------------
**Tier 1, in memory.**  Charts need the last few hundred samples at repaint
speed.  A :class:`~app.core.timeseries.TimeSeries` ring buffer gives O(1) append
and bounded memory, and never touches the disk.

**Tier 2, on disk.**  The history page needs days of data.  Writing every sample
synchronously would mean thousands of transactions a minute, so samples are
accumulated and flushed in one batch on a timer -- a write-behind cache.  Losing
the last few seconds of history in a crash is an acceptable trade for not
touching the disk on every tick.

Retention runs on the same timer, checking both age and total size so the
database honours the user's configured limits without a separate scheduler.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TypedDict

from PySide6.QtCore import QObject, QTimer

from app.core.config import ConfigService
from app.core.timeseries import SeriesGroup, TimeSeries
from app.database.connection import Database
from app.database.repositories import MetricRepository, SessionRepository
from app.services.metrics import MetricRegistry

_log = logging.getLogger(__name__)

#: How often retention is evaluated, in seconds.  Pruning is cheap when there is
#: nothing to prune, so this can be frequent without cost.
_RETENTION_INTERVAL = 600.0


class StorageReport(TypedDict):
    """Database size and coverage, as shown on the History and Settings pages."""

    path: str
    size_bytes: int
    samples: int
    metrics: int
    oldest: float | None
    newest: float | None
    persisting: bool
    degraded: bool


class HistoryService(QObject):
    """Records metrics into ring buffers and, optionally, into SQLite."""

    def __init__(
        self,
        config: ConfigService,
        database: Database,
        registry: MetricRegistry,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._registry = registry
        self._metrics = MetricRepository(database)
        self._sessions = SessionRepository(database)
        self._database = database

        capacity = self._capacity_for(config.settings.charts.history_seconds)
        self._series = SeriesGroup(capacity)
        self._pending: list[tuple[float, str, float]] = []
        self._pending_lock = threading.Lock()
        self._last_retention = time.monotonic()
        self._active_session: int | None = None

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(
            int(config.settings.history.flush_seconds * 1000)
        )
        self._flush_timer.timeout.connect(self._flush)

        self._unsubscribe = config.subscribe(self._on_setting_changed)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Begin the periodic flush, and prune anything already expired."""
        if self._config.settings.history.enabled and not self._database.degraded:
            self._flush_timer.start()
            self._apply_retention()
        _log.info(
            "History service started (persistence %s)",
            "enabled" if self.persisting else "disabled",
        )

    @property
    def persisting(self) -> bool:
        """True when samples are being written to disk."""
        return (
            self._config.settings.history.enabled and not self._database.degraded
        )

    def close(self) -> None:
        """Flush outstanding samples and stop."""
        self._unsubscribe()
        self._flush_timer.stop()
        self._flush()
        if self._active_session is not None:
            self._sessions.finish(self._active_session)

    # -------------------------------------------------------------------- recording

    def record(self, domain: str, snapshot: object) -> dict[str, float]:
        """Extract, buffer and queue every metric in one snapshot.

        Returns the extracted values so the caller (the application controller)
        can hand the same dictionary to the alert engine without extracting
        twice.
        """
        values = self._registry.extract(domain, snapshot)
        if not values:
            return values
        moment = time.monotonic()
        wall_clock = time.time()
        for name, value in values.items():
            self._series.record(name, value, moment)
        if self.persisting:
            with self._pending_lock:
                self._pending.extend(
                    (wall_clock, name, value) for name, value in values.items()
                )
        return values

    def series(self, metric: str) -> TimeSeries:
        """The in-memory ring buffer for ``metric``, created on first access."""
        return self._series[metric]

    def has_series(self, metric: str) -> bool:
        """True when ``metric`` has at least one buffered sample."""
        return metric in self._series and len(self._series[metric]) > 0

    def tracked_metrics(self) -> list[str]:
        """Metric names with in-memory data."""
        return sorted(self._series.keys())

    # -------------------------------------------------------------------- flushing

    def _flush(self) -> None:
        """Write buffered samples to SQLite in one transaction."""
        with self._pending_lock:
            if not self._pending:
                batch: list[tuple[float, str, float]] = []
            else:
                batch, self._pending = self._pending, []
        if batch:
            written = self._metrics.record_batch(batch)
            _log.debug("Flushed %d/%d metric samples", written, len(batch))
        if time.monotonic() - self._last_retention >= _RETENTION_INTERVAL:
            self._apply_retention()

    # ------------------------------------------------------------------ retention

    def _apply_retention(self) -> None:
        """Enforce both the age limit and the size ceiling.

        Age is the primary control; the size ceiling is a backstop for the case
        where the user has set a long retention on a fast sampling cadence, or has
        just lowered the limit.  A large deletion is followed by a vacuum, because
        SQLite otherwise keeps the freed pages and the file never shrinks.
        """
        self._last_retention = time.monotonic()
        settings = self._config.settings.history
        if not self.persisting:
            return

        removed = self._metrics.prune(settings.retention_days)
        if removed:
            _log.info("Pruned %d metric samples older than %d days",
                      removed, settings.retention_days)

        limit_bytes = int(settings.max_megabytes) * 1024 * 1024
        if self._database.size_bytes() > limit_bytes:
            # Estimate how many rows fit in the budget.  Measured at roughly
            # 40 bytes per row including index overhead; the estimate only needs
            # to be in the right order of magnitude because this repeats.
            keep = max(1000, int(limit_bytes / 40))
            trimmed = self._metrics.trim_to_newest(keep)
            _log.info(
                "Database exceeded %d MB; trimmed %d oldest samples",
                settings.max_megabytes, trimmed,
            )
            removed += trimmed
        if removed > 10_000:
            self._database.vacuum()

    # ------------------------------------------------------------------- sessions

    def start_session(self, name: str, note: str = "") -> bool:
        """Begin a named recording session."""
        if self._active_session is not None or not self.persisting:
            return False
        self._active_session = self._sessions.start(name, note)
        return self._active_session is not None

    def stop_session(self) -> bool:
        """End the active recording session."""
        if self._active_session is None:
            return False
        self._sessions.finish(self._active_session)
        self._active_session = None
        return True

    @property
    def recording(self) -> bool:
        """True while a named session is active."""
        return self._active_session is not None

    def sessions(self) -> list[dict]:
        """Every recorded session."""
        return self._sessions.all()

    # ----------------------------------------------------------------- reporting

    def stored_series(
        self, metric: str, *, hours: float = 24.0
    ) -> list[tuple[float, float]]:
        """Read one metric's persisted history as ``(timestamp, value)`` pairs."""
        since = time.time() - hours * 3600.0
        points = self._metrics.series(metric, since=since)
        return [(p.recorded, p.value) for p in points]

    def stored_summary(self, metric: str, *, hours: float = 24.0) -> dict[str, float]:
        """Aggregate statistics over the persisted history of one metric."""
        return self._metrics.summary(metric, since=time.time() - hours * 3600.0)

    def storage_report(self) -> StorageReport:
        """Database size and coverage, for the settings and history pages."""
        bounds = self._metrics.bounds()
        return {
            "path": self._database.path,
            "size_bytes": self._database.size_bytes(),
            "samples": self._metrics.count(),
            "metrics": len(self._metrics.metrics()),
            "oldest": bounds[0] if bounds else None,
            "newest": bounds[1] if bounds else None,
            "persisting": self.persisting,
            "degraded": self._database.degraded,
        }

    # ------------------------------------------------------------------- settings

    @staticmethod
    def _capacity_for(history_seconds: int) -> int:
        """Ring-buffer capacity for a requested chart window.

        Sized for a one-second cadence -- the fastest any collector runs -- with a
        margin so a slightly late tick cannot evict a sample the chart still
        wants to draw.
        """
        return max(60, min(7200, int(history_seconds * 1.2)))

    def _on_setting_changed(self, key: str, value: object) -> None:
        """React to changes in history or chart settings."""
        if key == "history.flush_seconds" and isinstance(value, (int, float)):
            self._flush_timer.setInterval(int(float(value) * 1000))
        elif key == "history.enabled":
            if value and not self._flush_timer.isActive():
                self._flush_timer.start()
            elif not value:
                self._flush()
                self._flush_timer.stop()
        elif key == "charts.history_seconds":
            # Ring buffers are fixed-capacity, so a window change means new
            # buffers.  Existing data is discarded rather than migrated: the user
            # explicitly asked for a different window, and charts refill within
            # seconds.
            _log.debug("Chart window changed; reallocating history buffers")
            if isinstance(value, (int, float)):
                self._series = SeriesGroup(self._capacity_for(int(value)))
