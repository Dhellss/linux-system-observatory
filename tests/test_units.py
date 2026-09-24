"""Tests for the formatting helpers.

These are pure functions, so they are exhaustively testable -- and worth testing,
because a unit error here is invisible until a user notices their 4 TB disk
reported as 4 GB.
"""

from __future__ import annotations

import pytest
from app.core.units import (
    DASH,
    bits_rate,
    bytes_,
    clock,
    count,
    duration,
    frequency,
    percent,
    rate,
    temperature,
    text,
    volts,
    watts,
)
from app.models.base import Reason, Unavailable


class TestBytes:
    """Byte formatting."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024**2, "1.0 MiB"),
            (1024**3 * 1.5, "1.5 GiB"),
            (1024**4, "1.0 TiB"),
        ],
    )
    def test_binary_ladder(self, value: float, expected: str) -> None:
        """IEC prefixes are used for memory and file sizes."""
        assert bytes_(value) == expected

    def test_decimal_ladder(self) -> None:
        """SI prefixes are available for throughput figures."""
        assert bytes_(1500, binary=False) == "1.5 KB"
        assert bytes_(1_000_000, binary=False) == "1.0 MB"

    def test_negative(self) -> None:
        """Negative byte counts keep their sign."""
        assert bytes_(-2048) == "-2.0 KiB"

    def test_never_exceeds_the_unit_ladder(self) -> None:
        """An absurd value saturates at the largest unit rather than failing."""
        assert bytes_(1024**9).endswith("EiB")


class TestAbsence:
    """Every formatter must accept an absent value."""

    FORMATTERS = (
        bytes_, rate, percent, temperature, frequency, watts, volts,
        duration, clock, count, bits_rate,
    )

    @pytest.mark.parametrize("formatter", FORMATTERS)
    def test_unavailable_renders_as_dash(self, formatter) -> None:
        """An Unavailable reading formats as an em dash, never as zero."""
        assert formatter(Unavailable(Reason.NO_HARDWARE)) == DASH

    @pytest.mark.parametrize("formatter", FORMATTERS)
    def test_none_renders_as_dash(self, formatter) -> None:
        """A missing value formats as an em dash."""
        assert formatter(None) == DASH

    @pytest.mark.parametrize("formatter", FORMATTERS)
    def test_nan_renders_as_dash(self, formatter) -> None:
        """A NaN from a flaky sensor must not reach the user as 'nan'."""
        assert formatter(float("nan")) == DASH

    @pytest.mark.parametrize("formatter", FORMATTERS)
    def test_infinity_renders_as_dash(self, formatter) -> None:
        """An infinite rate (divide by a zero interval) is also absence."""
        assert formatter(float("inf")) == DASH


class TestOtherFormatters:
    """The remaining quantity formatters."""

    def test_frequency_promotes_to_gigahertz(self) -> None:
        """Past 1000 MHz the figure reads better in GHz."""
        assert frequency(800) == "800 MHz"
        assert frequency(3600) == "3.60 GHz"

    def test_watts_demotes_to_milliwatts(self) -> None:
        """Sub-watt draws are more legible in milliwatts."""
        assert watts(0.25) == "250 mW"
        assert watts(65.0) == "65.0 W"

    def test_duration_scales_by_magnitude(self) -> None:
        """Durations drop irrelevant units."""
        assert duration(45) == "45s"
        assert duration(125) == "2m 5s"
        assert duration(3725) == "1h 2m"
        assert duration(93_784) == "1d 2h 3m"

    def test_clock_is_fixed_width(self) -> None:
        """Process runtimes use a stable HH:MM:SS width."""
        assert clock(0) == "00:00:00"
        assert clock(3661) == "01:01:01"

    def test_rate_uses_decimal_units(self) -> None:
        """Throughput follows the SI convention used by network counters."""
        assert rate(1_500_000) == "1.5 MB/s"

    def test_bits_rate_converts_from_bytes(self) -> None:
        """Link speeds are quoted in bits per second."""
        assert bits_rate(125_000) == "1.0 Mbps"

    def test_text_maps_empty_to_dash(self) -> None:
        """Blank strings are absence, not a value."""
        assert text("") == DASH
        assert text(None) == DASH
        assert text("value") == "value"
