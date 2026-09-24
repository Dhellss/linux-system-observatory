"""Foundational value types shared by every monitoring model.

The central idea in this module is :class:`Unavailable`.  On Linux, whether a
metric exists depends on the kernel version, the distribution, the hardware and
the permissions of the running user.  Rather than raising -- or worse,
fabricating a plausible-looking zero -- collectors return an ``Unavailable``
value carrying a human-readable *reason*.  The UI renders that reason directly,
so the user learns *why* a number is missing instead of seeing a silent gap.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias, TypeGuard, TypeVar

T = TypeVar("T")


class Reason(enum.Enum):
    """Why a metric could not be produced.

    Kept as an enum (rather than free-form strings) so the UI can style
    different classes of absence differently -- a missing permission is
    actionable, absent hardware is not.
    """

    NO_HARDWARE = "No supporting hardware detected"
    NO_DRIVER = "Required driver or kernel module not loaded"
    NO_TOOL = "Required command-line utility is not installed"
    PERMISSION = "Insufficient permissions"
    UNSUPPORTED = "Not supported on this platform or kernel"
    NOT_EXPOSED = "Hardware does not expose this metric"
    ERROR = "Collection failed"
    PENDING = "Not yet sampled"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Sentinel standing in for a value that cannot be obtained.

    Instances are falsy, so ``if value:`` guards read naturally, and they
    stringify to an explanation suitable for direct display.

    Examples
    --------
    >>> u = Unavailable(Reason.NO_TOOL, "smartctl")
    >>> bool(u)
    False
    >>> str(u)
    'Required command-line utility is not installed: smartctl'
    """

    reason: Reason = Reason.PENDING
    detail: str = ""

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        if self.detail:
            return f"{self.reason.value}: {self.detail}"
        return self.reason.value

    @property
    def short(self) -> str:
        """A compact label for dense table cells and stat tiles."""
        return "Unavailable"

    @property
    def actionable(self) -> bool:
        """True when the user could plausibly fix the cause themselves."""
        return self.reason in (Reason.NO_TOOL, Reason.PERMISSION, Reason.NO_DRIVER)


#: A value that is either present, explained-absent, or plainly absent.
#:
#: ``None`` is included deliberately.  Three states occur in practice:
#:
#: * a real reading,
#: * an :class:`Unavailable` carrying the reason it could not be taken,
#: * a bare ``None``, where the source simply had nothing to say and no
#:   reason worth surfacing (an optional latency figure on an idle disk).
#:
#: :func:`is_available`, :func:`is_number` and every formatter in
#: :mod:`app.core.units` treat all three consistently, so callers never need to
#: distinguish them unless they want the explanation.
#:
#: Annotated as a ``TypeAlias`` so type checkers treat it as a generic alias
#: rather than a module-level variable; without the annotation mypy reports
#: "Variable is not valid as a type" at every one of its several hundred uses.
Maybe: TypeAlias = T | Unavailable | None

#: Convenience singletons for the most common cases.
PENDING: Final = Unavailable(Reason.PENDING)
NO_HARDWARE: Final = Unavailable(Reason.NO_HARDWARE)


def value_or(value: Maybe[T], fallback: T) -> T:
    """Return ``value`` unless it is absent, else ``fallback``.

    Useful in aggregation paths (sums, averages) where an absent reading should
    simply not contribute rather than poison the whole calculation.  Both forms
    of absence -- :class:`Unavailable` and a bare ``None`` -- map to
    ``fallback``.
    """
    if value is None or isinstance(value, Unavailable):
        return fallback
    return value


def is_available(value: Any) -> bool:
    """True when ``value`` is a real reading rather than an absence marker."""
    return not isinstance(value, Unavailable) and value is not None


def is_number(value: Any) -> TypeGuard[float]:
    """True when ``value`` is a usable numeric reading.

    Declared as a :class:`~typing.TypeGuard` so that a type checker narrows a
    ``Maybe[float]`` to ``float`` inside the guarded branch — which is what
    makes arithmetic on a guarded reading type-check without a cast.

    Preferred over a bare ``is_number(value)`` availability test, which
    is subtly wrong: sysfs and psutil both hand back plain integers for some
    readings, and a float-only check silently classifies an integer temperature
    or a whole-number percentage as *absent*.  ``bool`` is excluded because it is
    a subclass of ``int`` and a flag is not a measurement, and non-finite floats
    are excluded because a NaN from a flaky sensor is absence too.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Base class for every collector result.

    Carries the monotonic timestamp at which the sample was taken.  Monotonic
    time is deliberate: wall-clock adjustments (NTP steps, suspend/resume)
    must not make rate calculations produce negative deltas.
    """

    timestamp: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        """Seconds elapsed since this snapshot was taken."""
        return max(0.0, time.monotonic() - self.timestamp)
