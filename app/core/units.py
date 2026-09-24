"""Human-readable formatting for every quantity the application displays.

Formatting lives in one module for two reasons: consistency (a megabyte is
rendered identically on the dashboard, in a table and in an exported report) and
testability (these are pure functions with no Qt dependency).

``Unavailable`` values are accepted everywhere, so views can format
unconditionally instead of branching first.
"""

from __future__ import annotations

import datetime as dt
import math

from app.models.base import Unavailable

#: Binary (IEC) unit ladder -- the correct one for RAM and file sizes on Linux.
_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
#: Decimal (SI) unit ladder -- the correct one for network and disk throughput.
_DECIMAL_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")

DASH = "—"  # em dash, used consistently for "no value"


def _guard(value: object) -> str | None:
    """Return a placeholder string when ``value`` is not a usable number."""
    if isinstance(value, Unavailable):
        return DASH
    if value is None:
        return DASH
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return DASH
    return None


def bytes_(value: float | Unavailable | None, *, precision: int = 1,
           binary: bool = True) -> str:
    """Format a byte count using IEC (default) or SI prefixes.

    >>> bytes_(1536)
    '1.5 KiB'
    >>> bytes_(1500, binary=False)
    '1.5 KB'
    """
    if (placeholder := _guard(value)) is not None:
        return placeholder
    units = _BINARY_UNITS if binary else _DECIMAL_UNITS
    step = 1024.0 if binary else 1000.0
    amount = float(value)  # type: ignore[arg-type]
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    index = 0
    while amount >= step and index < len(units) - 1:
        amount /= step
        index += 1
    digits = 0 if index == 0 else precision
    return f"{sign}{amount:.{digits}f} {units[index]}"


def rate(value: float | Unavailable | None, *, precision: int = 1) -> str:
    """Format a bytes-per-second throughput using SI prefixes.

    Network and storage vendors quote decimal units, and so do kernel counters
    in practice, so throughput deliberately uses SI where memory uses IEC.
    """
    if (placeholder := _guard(value)) is not None:
        return placeholder
    return f"{bytes_(value, precision=precision, binary=False)}/s"


def bits_rate(value: float | Unavailable | None) -> str:
    """Format a bytes-per-second value as a bits-per-second link speed."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    bits = float(value) * 8  # type: ignore[arg-type]
    for unit in ("bps", "Kbps", "Mbps", "Gbps", "Tbps"):
        if bits < 1000 or unit == "Tbps":
            return f"{bits:.1f} {unit}"
        bits /= 1000
    return f"{bits:.1f} Tbps"


def percent(value: float | Unavailable | None, *, precision: int = 1) -> str:
    """Format a 0-100 percentage."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    return f"{float(value):.{precision}f}%"  # type: ignore[arg-type]


def temperature(value: float | Unavailable | None, *, precision: int = 1) -> str:
    """Format a Celsius temperature."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    return f"{float(value):.{precision}f} °C"  # type: ignore[arg-type]


def frequency(mhz: float | Unavailable | None) -> str:
    """Format a megahertz frequency, promoting to GHz past 1000 MHz."""
    if (placeholder := _guard(mhz)) is not None:
        return placeholder
    value = float(mhz)  # type: ignore[arg-type]
    return f"{value / 1000:.2f} GHz" if value >= 1000 else f"{value:.0f} MHz"


def watts(value: float | Unavailable | None, *, precision: int = 1) -> str:
    """Format a power draw in watts, demoting sub-watt values to milliwatts."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    amount = float(value)  # type: ignore[arg-type]
    if abs(amount) < 1:
        return f"{amount * 1000:.0f} mW"
    return f"{amount:.{precision}f} W"


def volts(value: float | Unavailable | None) -> str:
    """Format a voltage."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    return f"{float(value):.3f} V"  # type: ignore[arg-type]


def rpm(value: float | Unavailable | None) -> str:
    """Format a fan speed, distinguishing a stopped fan from an absent one."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    amount = float(value)  # type: ignore[arg-type]
    return "Stopped" if amount <= 0 else f"{amount:.0f} RPM"


def duration(seconds: float | Unavailable | None, *, compact: bool = False) -> str:
    """Format an elapsed time.

    >>> duration(93784)
    '1d 2h 3m'
    >>> duration(75, compact=True)
    '1m 15s'
    """
    if (placeholder := _guard(seconds)) is not None:
        return placeholder
    total = int(max(0.0, float(seconds)))  # type: ignore[arg-type]
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def clock(seconds: float | Unavailable | None) -> str:
    """Format an elapsed time as ``HH:MM:SS`` for process runtimes."""
    if (placeholder := _guard(seconds)) is not None:
        return placeholder
    total = int(max(0.0, float(seconds)))  # type: ignore[arg-type]
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def timestamp(epoch: float | Unavailable | None, *, with_date: bool = True) -> str:
    """Format a Unix timestamp in local time."""
    if (placeholder := _guard(epoch)) is not None:
        return placeholder
    moment = dt.datetime.fromtimestamp(float(epoch))  # type: ignore[arg-type]
    return moment.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def count(value: float | Unavailable | None) -> str:
    """Format a plain integer count with thousands separators."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    return f"{int(value):,}"  # type: ignore[arg-type]


def iops(value: float | Unavailable | None) -> str:
    """Format an I/O operations-per-second figure."""
    if (placeholder := _guard(value)) is not None:
        return placeholder
    amount = float(value)  # type: ignore[arg-type]
    if amount >= 1000:
        return f"{amount / 1000:.1f}K IOPS"
    return f"{amount:.0f} IOPS"


def text(value: object, fallback: str = DASH) -> str:
    """Format any value as display text, mapping absence onto ``fallback``."""
    if isinstance(value, Unavailable) or value is None or value == "":
        return fallback
    return str(value)
