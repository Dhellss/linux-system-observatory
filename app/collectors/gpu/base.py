"""The GPU back-end contract.

Three vendors expose completely different interfaces -- NVIDIA a management
binary, AMD a rich sysfs tree, Intel a sparse one -- yet the GPU page should be
written once.  Each back-end therefore implements :class:`GpuBackend` and
returns the shared :class:`~app.models.gpu.GpuSample` shape, filling in what it
can and leaving the rest ``Unavailable``.

Adding a fourth vendor means adding one file and one registry entry; no UI code
changes.
"""

from __future__ import annotations

import abc
import logging

from app.core.capabilities import CapabilityRegistry
from app.models.gpu import GpuSample

_log = logging.getLogger(__name__)


class GpuBackend(abc.ABC):
    """Reads telemetry for one vendor's adapters."""

    #: Display name of the vendor this back-end serves.
    vendor_name: str = "Unknown"

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self.capabilities = capabilities
        self._log = logging.getLogger(f"gpu.{self.vendor_name.lower()}")

    @abc.abstractmethod
    def available(self) -> bool:
        """True when this back-end's hardware and interface are both present."""

    @abc.abstractmethod
    def sample(self) -> list[GpuSample]:
        """Read every adapter this back-end handles."""

    def safe_sample(self) -> list[GpuSample]:
        """Sample this vendor, isolating failures from the other back-ends.

        A hybrid laptop commonly has two vendors present.  If the discrete
        driver misbehaves, the integrated adapter should still be reported.
        """
        if not self.available():
            return []
        try:
            return self.sample()
        except Exception:
            self._log.exception("GPU sampling failed")
            return []


def parse_optional_float(raw: str | None) -> float | None:
    """Parse a numeric field that may be a vendor placeholder.

    Vendors signal "this adapter does not report that" with a variety of
    spellings -- ``[N/A]``, ``N/A``, ``Unknown``, ``[Not Supported]``.  All of
    them must become ``None`` rather than a parse error or, worse, a zero that
    the UI would render as a real measurement.
    """
    if raw is None:
        return None
    token = raw.strip().strip("[]").strip()
    if not token or token.lower() in {
        "n/a", "na", "not supported", "unknown", "not available", "error",
    }:
        return None
    # Strip any trailing unit such as "MiB" or "W" that slipped through.
    cleaned = "".join(c for c in token if c.isdigit() or c in ".-+")
    try:
        return float(cleaned) if cleaned not in ("", "-", "+", ".") else None
    except ValueError:
        return None


def parse_optional_text(raw: str | None) -> str | None:
    """Parse a text field that may be a vendor placeholder."""
    if raw is None:
        return None
    token = raw.strip().strip("[]").strip()
    if not token or token.lower() in {"n/a", "na", "not supported", "unknown"}:
        return None
    return token
