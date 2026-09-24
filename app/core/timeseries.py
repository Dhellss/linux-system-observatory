"""Fixed-capacity ring buffers for chart data.

Design rationale
----------------
Charts need the last N samples and nothing more.  An unbounded list would grow
without limit over a long session -- exactly the kind of slow leak that makes
monitoring tools embarrassing.  A :class:`collections.deque` with ``maxlen``
gives O(1) append and automatic eviction with no bookkeeping.

The buffer stores *plain floats in parallel arrays* rather than objects, because
pyqtgraph wants two sequences and converting a list of dataclasses on every
repaint would dominate the frame budget.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Default retention: 300 samples, i.e. five minutes at a one-second cadence.
DEFAULT_CAPACITY = 300


@dataclass(frozen=True, slots=True)
class Stats:
    """Summary statistics over a series window."""

    minimum: float
    maximum: float
    mean: float
    latest: float
    samples: int


class TimeSeries:
    """A thread-safe, fixed-capacity series of ``(timestamp, value)`` samples.

    Writes happen on collector worker threads; reads happen on the UI thread
    during repaint.  A lock is therefore mandatory, but it is held only for the
    duration of an append or a list copy, so contention is negligible.
    """

    __slots__ = ("_capacity", "_lock", "_origin", "_times", "_values")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 2:
            raise ValueError("TimeSeries capacity must be at least 2")
        self._capacity = capacity
        self._times: deque[float] = deque(maxlen=capacity)
        self._values: deque[float] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._origin = time.monotonic()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    @property
    def capacity(self) -> int:
        """Maximum number of retained samples."""
        return self._capacity

    def append(self, value: float, timestamp: float | None = None) -> None:
        """Record a sample, evicting the oldest one when at capacity.

        Non-finite values are silently dropped: a single ``NaN`` from a flaky
        sensor should not break the whole plot.
        """
        if value is None or value != value or value in (float("inf"), float("-inf")):
            return
        with self._lock:
            self._times.append(timestamp if timestamp is not None else time.monotonic())
            self._values.append(float(value))

    def extend(self, samples: Iterable[tuple[float, float]]) -> None:
        """Bulk-append ``(timestamp, value)`` pairs, e.g. when restoring history."""
        with self._lock:
            for moment, value in samples:
                self._times.append(moment)
                self._values.append(float(value))

    def clear(self) -> None:
        """Discard every retained sample."""
        with self._lock:
            self._times.clear()
            self._values.clear()

    # ------------------------------------------------------------------ reading

    def snapshot(self) -> tuple[list[float], list[float]]:
        """Return ``(relative_times, values)`` suitable for handing to a plot.

        Times are expressed as **seconds before now** (negative, increasing to
        zero), which lets a chart keep a fixed x-axis while data scrolls.
        """
        with self._lock:
            if not self._values:
                return [], []
            now = time.monotonic()
            return [t - now for t in self._times], list(self._values)

    def values(self) -> list[float]:
        """A copy of the retained values, oldest first."""
        with self._lock:
            return list(self._values)

    @property
    def latest(self) -> float | None:
        """The most recent value, or ``None`` when empty."""
        with self._lock:
            return self._values[-1] if self._values else None

    def stats(self) -> Stats | None:
        """Summary statistics, or ``None`` when empty."""
        with self._lock:
            if not self._values:
                return None
            values = list(self._values)
        return Stats(
            minimum=min(values),
            maximum=max(values),
            mean=statistics.fmean(values),
            latest=values[-1],
            samples=len(values),
        )


class SeriesGroup:
    """A named collection of series that share a capacity.

    Used wherever a page tracks several related metrics -- per-core CPU load, per
    interface throughput -- so the page does not have to manage a dict of
    buffers itself.  Series are created on first access, which means a new CPU
    core or a hot-plugged interface needs no special handling.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._series: dict[str, TimeSeries] = {}
        self._lock = threading.Lock()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._series

    def __getitem__(self, key: str) -> TimeSeries:
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = TimeSeries(self._capacity)
                self._series[key] = series
            return series

    def keys(self) -> Sequence[str]:
        """Names of every series currently tracked."""
        with self._lock:
            return tuple(self._series)

    def record(self, key: str, value: float, timestamp: float | None = None) -> None:
        """Append ``value`` to the series named ``key``, creating it if needed."""
        self[key].append(value, timestamp)

    def drop(self, key: str) -> None:
        """Forget a series entirely (e.g. an interface that disappeared)."""
        with self._lock:
            self._series.pop(key, None)

    def clear(self) -> None:
        """Forget every series."""
        with self._lock:
            self._series.clear()
