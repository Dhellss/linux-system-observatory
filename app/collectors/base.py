"""The collector contract.

A collector is a small, stateless-as-possible object that turns one source of
kernel telemetry into one immutable snapshot.  Three rules keep the layer clean:

1. **No Qt.**  Collectors are plain Python, so the entire data layer can be
   exercised in a headless test without a display server.
2. **Never raise for absence.**  Missing hardware, absent tools and denied
   permissions are returned as ``Unavailable`` values inside an otherwise valid
   snapshot.  Only genuine bugs propagate, and :meth:`Collector.collect` catches
   even those so one broken collector cannot stop the others.
3. **Own your deltas.**  Most interesting Linux metrics are monotonic counters;
   converting them to rates requires remembering the previous read.  That state
   lives in the collector via :class:`RateTracker`, not in the UI.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from app.core.capabilities import CapabilityRegistry
from app.models.base import Snapshot
from app.utils.hwmon import HwmonProvider

_log = logging.getLogger(__name__)

S = TypeVar("S", bound=Snapshot)


@dataclass(slots=True)
class _CounterState:
    """Previous reading of one monotonic counter."""

    value: float
    timestamp: float


class RateTracker:
    """Converts monotonic counters into per-second rates.

    Handles the three ways counter arithmetic goes wrong in practice:

    * **First sample** -- there is no previous value, so the rate is zero rather
      than the absolute counter value (which would render as a huge false spike).
    * **Counter reset** -- an interface goes down, a device is re-plugged, or a
      32-bit counter wraps.  A negative delta is reported as zero instead of a
      negative rate.
    * **Irregular intervals** -- the scheduler may be late under load, so rates
      divide by the *measured* elapsed time, never the configured interval.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, _CounterState] = {}

    def rate(self, key: str, value: float, now: float | None = None) -> float:
        """Return the per-second rate of change of the counter named ``key``."""
        moment = now if now is not None else time.monotonic()
        previous = self._state.get(key)
        self._state[key] = _CounterState(value, moment)
        if previous is None:
            return 0.0
        elapsed = moment - previous.timestamp
        if elapsed <= 0:
            return 0.0
        delta = value - previous.value
        if delta < 0:  # counter reset or wrap
            return 0.0
        return delta / elapsed

    def delta(self, key: str, value: float, now: float | None = None) -> float:
        """Return the raw increase of ``key`` since the previous sample."""
        moment = now if now is not None else time.monotonic()
        previous = self._state.get(key)
        self._state[key] = _CounterState(value, moment)
        if previous is None:
            return 0.0
        return max(0.0, value - previous.value)

    def elapsed(self, key: str) -> float:
        """Seconds since ``key`` was last recorded, or 0 if never."""
        state = self._state.get(key)
        return time.monotonic() - state.timestamp if state else 0.0

    def forget(self, prefix: str) -> None:
        """Drop every counter whose key starts with ``prefix``.

        Called when a device disappears so its stale counters cannot produce a
        bogus rate if a device of the same name reappears later.
        """
        for key in [k for k in self._state if k.startswith(prefix)]:
            del self._state[key]

    def reset(self) -> None:
        """Forget every counter."""
        self._state.clear()


class Collector(abc.ABC, Generic[S]):
    """Base class for every telemetry source.

    Subclasses implement :meth:`_collect` and declare their identity through the
    class variables below.  The public :meth:`collect` wrapper adds uniform error
    handling and timing instrumentation.
    """

    #: Stable identifier used as a signal key, settings key and log prefix.
    domain: ClassVar[str] = "unknown"
    #: Human-readable name for diagnostics output.
    title: ClassVar[str] = "Unknown collector"
    #: When true, the scheduler samples once at start-up and then rarely.
    mostly_static: ClassVar[bool] = False

    def __init__(
        self,
        capabilities: CapabilityRegistry,
        hwmon: HwmonProvider | None = None,
    ) -> None:
        self.capabilities = capabilities
        # A shared provider is injected in production so collectors on different
        # cadences do not each pay for a full hwmon scan; the default keeps
        # collectors independently constructible in tests.
        self.hwmon = hwmon or HwmonProvider()
        self.rates = RateTracker()
        self._log = logging.getLogger(f"collector.{self.domain}")
        self._failures = 0
        self._last_duration = 0.0
        self._primed = False

    # ------------------------------------------------------------------ contract

    @abc.abstractmethod
    def _collect(self) -> S:
        """Produce one snapshot.  Implemented by subclasses."""

    def _empty(self) -> S:
        """A neutral snapshot returned when collection fails outright.

        Subclasses should override this so the UI always receives a
        well-formed object of the right type rather than ``None``.
        """
        raise NotImplementedError

    # -------------------------------------------------------------------- driver

    def collect(self) -> S | None:
        """Sample this domain, converting any unexpected error into ``None``.

        A collector that starts failing is logged loudly the first few times and
        quietly thereafter, so a permanently broken sensor does not fill the log
        file at the sampling cadence.
        """
        started = time.perf_counter()
        try:
            snapshot = self._collect()
        except Exception:
            self._failures += 1
            if self._failures <= 3:
                self._log.exception("Collection failed (failure %d)", self._failures)
            elif self._failures % 100 == 0:
                self._log.warning("Still failing after %d attempts", self._failures)
            try:
                return self._empty()
            except NotImplementedError:
                return None
        else:
            self._failures = 0
            self._primed = True
            return snapshot
        finally:
            self._last_duration = time.perf_counter() - started

    def prime(self) -> None:
        """Take a throwaway sample to initialise delta state.

        Rate-based collectors return zeros on their first call.  Priming during
        start-up means the first sample the *user* sees already has real rates.
        """
        try:
            self._collect()
        except Exception:
            self._log.debug("Priming failed; first sample will read zero")

    # ---------------------------------------------------------------- reporting

    @property
    def last_duration_ms(self) -> float:
        """Duration of the most recent collection, in milliseconds.

        Surfaced in the diagnostics page so an expensive collector is visible
        rather than merely suspected.
        """
        return self._last_duration * 1000.0

    @property
    def healthy(self) -> bool:
        """True when the collector is not in a failure streak."""
        return self._failures == 0

    @property
    def primed(self) -> bool:
        """True once at least one successful sample has been taken."""
        return self._primed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} domain={self.domain!r}>"
