"""The monitoring scheduler -- the heart of the application's concurrency model.

The problem
-----------
Ten collectors want to run at different cadences.  Their costs differ by two
orders of magnitude: reading ``/proc/stat`` takes half a millisecond, enumerating
370 processes takes 60 ms, and listing systemd units takes 800 ms.  All of this
must happen without the UI dropping a single frame.

Approaches considered
---------------------
*One thread per collector* -- ten OS threads, each mostly idle, each with its own
event loop.  Wasteful, and shutdown ordering becomes fiddly.

*One shared worker thread* -- simple, but the 800 ms systemd collector would
block the 1 s CPU collector, making the CPU chart visibly stutter every fifteen
seconds.

*A thread pool driven by one lightweight ticker* -- chosen.  A single
:class:`~PySide6.QtCore.QTimer` on the GUI thread ticks at a coarse resolution,
decides which collectors are due, and dispatches each to a
:class:`~PySide6.QtCore.QThreadPool`.  Slow collectors occupy a pool thread
without delaying fast ones, and the ticker itself does no work beyond comparing
timestamps.

Guarantees
----------
* **No overlapping runs.**  A collector still working when its next tick arrives
  is skipped, so a pathologically slow collector degrades its own frequency
  rather than saturating the pool.
* **Results arrive on the GUI thread.**  Snapshots travel via a Qt signal, which
  Qt marshals across the thread boundary automatically.  No view ever touches
  data being mutated by a worker.
* **Immutable payloads.**  Snapshots are frozen dataclasses, so there is nothing
  to race on even if a consumer keeps a reference.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TypeVar

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal

from app.collectors.base import Collector
from app.core.config import ConfigService
from app.models.base import Snapshot

_log = logging.getLogger(__name__)

#: Bound for :meth:`MonitorService.collector_of`.
C = TypeVar("C", bound=Collector)

#: Ticker resolution.  Fine enough that a 1 s cadence is honoured closely,
#: coarse enough that the timer itself is free.
TICK_MS = 200


@dataclass
class _Registration:
    """Scheduling state for one registered collector."""

    collector: Collector
    interval: float
    next_due: float = 0.0
    in_flight: bool = False
    #: Rolling count of samples taken, shown on the diagnostics page.
    samples: int = 0
    #: Exponential moving average of collection cost, in milliseconds.
    average_ms: float = 0.0
    #: Times the collector was skipped because it was still running.
    overruns: int = 0
    enabled: bool = True

    def record(self, duration_ms: float) -> None:
        """Fold one timing into the moving average."""
        self.samples += 1
        # A 0.2 smoothing factor reacts within a few samples without being noisy.
        self.average_ms = (
            duration_ms if self.samples == 1
            else 0.8 * self.average_ms + 0.2 * duration_ms
        )


class _CollectTask(QRunnable):
    """Runs one collector on a pool thread and reports the result.

    Deliberately minimal: it owns no state beyond the callback, because a
    ``QRunnable`` may outlive the tick that created it.
    """

    def __init__(self, domain: str, collector: Collector, sink) -> None:
        super().__init__()
        self._domain = domain
        self._collector = collector
        self._sink = sink
        self.setAutoDelete(True)

    def run(self) -> None:
        """Collect, then hand the result back to the scheduler."""
        started = time.perf_counter()
        snapshot = None
        try:
            snapshot = self._collector.collect()
        except Exception:
            _log.exception("Unhandled error collecting %s", self._domain)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            # The sink is a bound Qt slot, so this crosses back to the GUI thread
            # through Qt's queued-connection machinery rather than by touching
            # scheduler state from here.
            self._sink(self._domain, snapshot, elapsed_ms)


class MonitorService(QObject):
    """Schedules collectors and publishes their snapshots.

    Views never call collectors.  They connect to :attr:`snapshot_ready` (or to a
    ViewModel that does) and receive immutable snapshots on the GUI thread.
    """

    #: Emitted for every successful sample: ``(domain, snapshot)``.
    snapshot_ready = Signal(str, object)
    #: Emitted when a collector returns nothing: ``(domain,)``.
    collection_failed = Signal(str)
    #: Emitted after each tick so the status bar can show live scheduler health.
    statistics_changed = Signal()

    #: Internal relay used to marshal pool results onto the GUI thread.
    _result_ready = Signal(str, object, float)

    def __init__(self, config: ConfigService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._registry: dict[str, _Registration] = {}
        self._pool = QThreadPool(self)
        # Bounded deliberately.  Collectors are I/O-bound on /proc and /sys, so a
        # handful of threads saturates the available parallelism; more would only
        # add contention and context switches while the machine is already busy.
        self._pool.setMaxThreadCount(4)
        self._pool.setExpiryTimeout(30_000)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._tick)

        # QueuedConnection is what moves the payload from the pool thread to the
        # GUI thread; without it, _on_result would run on the worker.
        self._result_ready.connect(
            self._on_result, Qt.ConnectionType.QueuedConnection
        )

        self._paused = False
        self._unsubscribe = config.subscribe(self._on_setting_changed)

    # ---------------------------------------------------------------- registration

    def register(self, collector: Collector, interval: float | None = None) -> None:
        """Add a collector to the schedule.

        ``interval`` defaults to the configured cadence for the collector's
        domain, so adding a collector requires no scheduling decisions here.
        """
        domain = collector.domain
        cadence = (
            interval if interval is not None
            else self._config.settings.sampling.for_domain(domain)
        )
        self._registry[domain] = _Registration(
            collector=collector,
            interval=max(0.1, cadence),
            # Stagger start times so that ten collectors do not all fire on the
            # first tick and briefly saturate the pool at launch.
            next_due=time.monotonic() + 0.05 * len(self._registry),
        )
        _log.debug("Registered %s collector at %.1f s", domain, cadence)

    def collector(self, domain: str) -> Collector | None:
        """The registered collector for ``domain``, if any.

        Used for imperative operations that are not part of the sampling loop --
        inspecting a process, pinging a host, controlling a systemd unit.
        """
        registration = self._registry.get(domain)
        return registration.collector if registration else None

    def collector_of(self, domain: str, expected: type[C]) -> C | None:
        """Return the collector for ``domain`` only if it is of ``expected`` type.

        The untyped :meth:`collector` is enough for the scheduler, but a page
        that wants to call a collector's own methods -- ``inspect`` on the
        process collector, ``ping`` on the network one -- needs the concrete
        type.  Checking it here keeps that both type-safe for the checker and
        safe at run time: a domain whose collector failed to register simply
        yields ``None`` instead of an AttributeError deep inside a slot.
        """
        collector = self.collector(domain)
        return collector if isinstance(collector, expected) else None

    def domains(self) -> list[str]:
        """Registered domain names."""
        return list(self._registry)

    # -------------------------------------------------------------------- control

    def start(self) -> None:
        """Prime every collector, then begin sampling.

        Priming matters: rate-based collectors return zeros on their first
        sample, so without it the first thing the user sees is a row of zeros.
        Priming runs on the pool so start-up is not delayed by the slow ones.
        """
        for registration in self._registry.values():
            self._pool.start(_PrimeTask(registration.collector))
        self._timer.start()
        _log.info(
            "Monitoring started: %d collectors, %d worker threads",
            len(self._registry), self._pool.maxThreadCount(),
        )

    def stop(self) -> None:
        """Stop sampling and wait for in-flight collectors to finish."""
        self._timer.stop()
        # A bounded wait: a collector blocked on an unresponsive device must not
        # prevent the application from exiting.
        if not self._pool.waitForDone(3000):
            _log.warning("Some collectors did not finish within the shutdown timeout")

    def pause(self) -> None:
        """Suspend sampling while keeping registrations intact."""
        if not self._paused:
            self._paused = True
            self._timer.stop()
            _log.info("Monitoring paused")

    def resume(self) -> None:
        """Resume sampling after a pause."""
        if self._paused:
            self._paused = False
            now = time.monotonic()
            # Reset due times so a long pause does not cause every collector to
            # fire at once on resume.
            for registration in self._registry.values():
                registration.next_due = now
            self._timer.start()
            _log.info("Monitoring resumed")

    @property
    def paused(self) -> bool:
        """True while sampling is suspended."""
        return self._paused

    def set_enabled(self, domain: str, enabled: bool) -> None:
        """Enable or disable one collector without unregistering it.

        Lets the UI stop paying for a domain the user is not looking at -- for
        instance, the expensive systemd collector while the services page is
        closed.
        """
        if registration := self._registry.get(domain):
            registration.enabled = enabled

    def request(self, domain: str) -> None:
        """Sample one domain immediately, outside its normal cadence.

        Used when the user switches to a page and should not wait for the next
        tick, and by the refresh button.
        """
        registration = self._registry.get(domain)
        if registration and not registration.in_flight:
            self._dispatch(domain, registration)

    # ----------------------------------------------------------------- scheduling

    def _tick(self) -> None:
        """Dispatch every collector whose interval has elapsed."""
        now = time.monotonic()
        for domain, registration in self._registry.items():
            if not registration.enabled or now < registration.next_due:
                continue
            if registration.in_flight:
                # Still working from last time.  Skip this slot rather than
                # queueing another run, and push the next attempt out so a
                # chronically slow collector settles at a sustainable rate.
                registration.overruns += 1
                registration.next_due = now + registration.interval
                continue
            self._dispatch(domain, registration)
        self.statistics_changed.emit()

    def _dispatch(self, domain: str, registration: _Registration) -> None:
        """Submit one collector to the thread pool."""
        registration.in_flight = True
        registration.next_due = time.monotonic() + registration.interval
        self._pool.start(
            _CollectTask(domain, registration.collector, self._result_ready.emit)
        )

    def _on_result(self, domain: str, snapshot: object, elapsed_ms: float) -> None:
        """Handle a completed collection.  Runs on the GUI thread."""
        registration = self._registry.get(domain)
        if registration is not None:
            registration.in_flight = False
            registration.record(elapsed_ms)
        if isinstance(snapshot, Snapshot):
            self.snapshot_ready.emit(domain, snapshot)
        else:
            self.collection_failed.emit(domain)

    def _on_setting_changed(self, key: str, value: object) -> None:
        """Apply a live change to a sampling interval."""
        if not key.startswith("sampling."):
            return
        domain = key.split(".", 1)[1]
        registration = self._registry.get(domain)
        if registration is not None and isinstance(value, (int, float)):
            registration.interval = max(0.1, float(value))
            registration.next_due = time.monotonic() + registration.interval
            _log.debug("Cadence for %s changed to %.1f s", domain, value)

    # ------------------------------------------------------------------ reporting

    def statistics(self) -> list[dict[str, object]]:
        """Per-collector scheduling statistics for the diagnostics page."""
        return [
            {
                "domain": domain,
                "title": registration.collector.title,
                "interval": registration.interval,
                "samples": registration.samples,
                "average_ms": registration.average_ms,
                "overruns": registration.overruns,
                "healthy": registration.collector.healthy,
                "enabled": registration.enabled,
            }
            for domain, registration in sorted(self._registry.items())
        ]

    @property
    def total_cost_ms_per_second(self) -> float:
        """Estimated worker-thread cost of monitoring, in ms of CPU per second.

        Surfaced so the application can be held to its own standard: a monitor
        that consumes a whole core to report that a core is busy has failed.
        """
        return sum(
            r.average_ms / r.interval
            for r in self._registry.values()
            if r.enabled and r.samples
        )

    def close(self) -> None:
        """Stop sampling and release resources."""
        self._unsubscribe()
        self.stop()


class _PrimeTask(QRunnable):
    """Takes a throwaway first sample to initialise a collector's delta state."""

    def __init__(self, collector: Collector) -> None:
        super().__init__()
        self._collector = collector
        self.setAutoDelete(True)

    def run(self) -> None:
        """Prime the collector, ignoring any failure."""
        try:
            self._collector.prime()
        except Exception:
            _log.debug("Priming %s failed", self._collector.domain, exc_info=True)
