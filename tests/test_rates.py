"""Tests for counter-to-rate conversion and the chart ring buffers.

Rate arithmetic is where monitoring tools quietly go wrong: a counter reset
becomes a negative rate, a first sample becomes an enormous spike, and a late
scheduler tick skews everything. Each of those is pinned here.
"""

from __future__ import annotations

import pytest
from app.collectors.base import RateTracker
from app.core.timeseries import SeriesGroup, TimeSeries


class TestRateTracker:
    """Monotonic counter differentiation."""

    def test_first_sample_is_zero_not_the_counter_value(self) -> None:
        """Without this, every chart opens with a huge false spike."""
        tracker = RateTracker()
        assert tracker.rate("k", 1_000_000_000, now=100.0) == 0.0

    def test_computes_per_second_rate(self) -> None:
        """A 1000-unit rise over two seconds is 500 per second."""
        tracker = RateTracker()
        tracker.rate("k", 1000, now=100.0)
        assert tracker.rate("k", 2000, now=102.0) == 500.0

    def test_counter_reset_reports_zero(self) -> None:
        """An interface going down resets its counters; that is not negative traffic."""
        tracker = RateTracker()
        tracker.rate("k", 5000, now=100.0)
        assert tracker.rate("k", 10, now=101.0) == 0.0

    def test_uses_measured_elapsed_time(self) -> None:
        """A late tick must not inflate the rate.

        Dividing by the *configured* interval instead of the measured one would
        report double the real rate whenever the scheduler ran late.
        """
        tracker = RateTracker()
        tracker.rate("k", 0, now=100.0)
        # Ten seconds elapsed, not the nominal one.
        assert tracker.rate("k", 1000, now=110.0) == 100.0

    def test_zero_interval_does_not_divide_by_zero(self) -> None:
        """Two samples at the same instant yield zero, not an exception."""
        tracker = RateTracker()
        tracker.rate("k", 100, now=100.0)
        assert tracker.rate("k", 200, now=100.0) == 0.0

    def test_keys_are_independent(self) -> None:
        """Each counter tracks its own history."""
        tracker = RateTracker()
        tracker.rate("a", 0, now=0.0)
        tracker.rate("b", 0, now=0.0)
        assert tracker.rate("a", 10, now=1.0) == 10.0
        assert tracker.rate("b", 20, now=1.0) == 20.0

    def test_forget_drops_a_prefix(self) -> None:
        """A departed device's counters are discarded, not reused.

        Otherwise a replacement device with the same kernel name would produce
        one enormous spike from the stale baseline.
        """
        tracker = RateTracker()
        tracker.rate("net.eth0.rx", 5000, now=0.0)
        tracker.forget("net.eth0.")
        assert tracker.rate("net.eth0.rx", 6000, now=1.0) == 0.0

    def test_delta_returns_raw_increase(self) -> None:
        """Deltas are available where a rate is not wanted."""
        tracker = RateTracker()
        tracker.delta("k", 100, now=0.0)
        assert tracker.delta("k", 150, now=1.0) == 50.0


class TestTimeSeries:
    """The fixed-capacity chart buffer."""

    def test_respects_capacity(self) -> None:
        """Memory use is bounded no matter how long the session runs."""
        series = TimeSeries(capacity=10)
        for value in range(100):
            series.append(value)
        assert len(series) == 10
        assert series.values() == list(range(90, 100))

    def test_rejects_non_finite_values(self) -> None:
        """A single NaN from a flaky sensor must not break the plot."""
        series = TimeSeries(capacity=10)
        series.append(float("nan"))
        series.append(float("inf"))
        series.append(1.0)
        assert series.values() == [1.0]

    def test_latest_and_stats(self) -> None:
        """Summary statistics describe the retained window."""
        series = TimeSeries(capacity=5)
        for value in (1.0, 2.0, 3.0):
            series.append(value)
        assert series.latest == 3.0
        stats = series.stats()
        assert (stats.minimum, stats.maximum, stats.mean, stats.samples) == (
            1.0, 3.0, 2.0, 3,
        )

    def test_empty_series_is_safe(self) -> None:
        """An unpopulated series answers without raising."""
        series = TimeSeries()
        assert series.latest is None
        assert series.stats() is None
        assert series.snapshot() == ([], [])

    def test_snapshot_times_are_relative_and_ordered(self) -> None:
        """Chart x values count backwards from now, increasing to zero."""
        series = TimeSeries(capacity=5)
        for value in range(3):
            series.append(value)
        times, values = series.snapshot()
        assert len(times) == len(values) == 3
        assert all(t <= 0.001 for t in times)
        assert times == sorted(times)

    def test_requires_a_sane_capacity(self) -> None:
        """A one-sample buffer cannot draw a line."""
        with pytest.raises(ValueError):
            TimeSeries(capacity=1)


class TestSeriesGroup:
    """Lazily-created named series."""

    def test_creates_series_on_demand(self) -> None:
        """A new CPU core or interface needs no special handling."""
        group = SeriesGroup(capacity=5)
        group.record("cpu.0", 50.0)
        assert "cpu.0" in group
        assert group["cpu.0"].latest == 50.0

    def test_drop_forgets_a_series(self) -> None:
        """A vanished device's series can be released."""
        group = SeriesGroup()
        group.record("eth0", 1.0)
        group.drop("eth0")
        assert "eth0" not in group
