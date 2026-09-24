"""Exception hierarchy for the application.

Collectors are expected to *return* :class:`~app.models.base.Unavailable`
rather than raise for routine absence.  These exceptions cover genuine
programming or environment faults that should be logged and surfaced.
"""

from __future__ import annotations


class ObservatoryError(Exception):
    """Base class for all application-specific errors."""


class ConfigurationError(ObservatoryError):
    """Raised when persisted settings cannot be read or are malformed."""


class DependencyError(ObservatoryError):
    """Raised by the DI container when a service cannot be resolved."""


class CollectorError(ObservatoryError):
    """Raised when a collector fails in a way it cannot describe as absence."""

    def __init__(self, collector: str, message: str) -> None:
        super().__init__(f"[{collector}] {message}")
        self.collector = collector


class PrivilegedActionError(ObservatoryError):
    """Raised when an action requiring elevated privileges was refused."""
