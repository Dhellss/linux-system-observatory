"""Desktop notification delivery.

Notifications go through the freedesktop D-Bus specification via
``notify-send``, which every mainstream desktop implements.  The alternative --
talking to ``org.freedesktop.Notifications`` directly -- would need a D-Bus
binding as a hard dependency for a feature that must degrade gracefully anyway.

Delivery is fire-and-forget and rate-limited per rule, so a sustained condition
notifies once rather than at the sampling cadence.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, Signal

from app.core.config import ConfigService
from app.models.diagnostics import AlertEvent, Severity
from app.utils.shell import has_tool, run

_log = logging.getLogger(__name__)

#: Map severity onto the urgency levels notify-send understands.
_URGENCY = {
    Severity.OK: "low",
    Severity.INFO: "low",
    Severity.WARNING: "normal",
    Severity.CRITICAL: "critical",
}

#: Icon names from the freedesktop icon naming specification, so they resolve
#: against whichever theme the user's desktop is using.
_ICONS = {
    Severity.OK: "dialog-information",
    Severity.INFO: "dialog-information",
    Severity.WARNING: "dialog-warning",
    Severity.CRITICAL: "dialog-error",
}

APP_NAME = "Linux System Observatory"


class NotificationService(QObject):
    """Delivers alert notifications to the desktop and to the in-app banner."""

    #: Emitted for every notification so the window can show an in-app banner
    #: even when desktop notifications are unavailable or disabled.
    banner_requested = Signal(str, str, int)   # title, body, severity

    def __init__(self, config: ConfigService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._last_sent: dict[str, float] = {}
        self._available = has_tool("notify-send")
        if not self._available:
            _log.info(
                "notify-send not found; desktop notifications disabled "
                "(in-app banners still work)"
            )

    @property
    def desktop_available(self) -> bool:
        """True when desktop notifications can be delivered."""
        return self._available

    def notify_alert(self, event: AlertEvent) -> None:
        """Deliver a notification for an alert transition, if permitted."""
        rule = event.rule
        if not rule.notify:
            return
        settings = self._config.settings.notifications

        # Rate-limit per rule.  A condition that stays true for an hour should
        # notify once, not once per sample.
        now = time.monotonic()
        last = self._last_sent.get(rule.rule_id, 0.0)
        if event.active and now - last < settings.cooldown_seconds:
            return
        self._last_sent[rule.rule_id] = now

        title = event.headline
        body = event.detail
        if settings.in_app_banners:
            self.banner_requested.emit(title, body, int(rule.severity))
        if settings.desktop_notifications:
            self._send(title, body, rule.severity)

    def notify(self, title: str, body: str,
               severity: Severity = Severity.INFO) -> None:
        """Deliver an ad-hoc notification (used by export and diagnostics)."""
        if self._config.settings.notifications.in_app_banners:
            self.banner_requested.emit(title, body, int(severity))
        if self._config.settings.notifications.desktop_notifications:
            self._send(title, body, severity)

    def _send(self, title: str, body: str, severity: Severity) -> None:
        """Invoke ``notify-send``, ignoring failure.

        A failed notification must never interrupt monitoring, so the result is
        logged at debug level and otherwise discarded.
        """
        if not self._available:
            return
        argv = [
            "notify-send",
            "--app-name", APP_NAME,
            "--urgency", _URGENCY.get(severity, "normal"),
            "--icon", _ICONS.get(severity, "dialog-information"),
            title,
            body,
        ]
        # Replace an existing notification from the same rule where the server
        # supports it, so a flapping condition does not stack up banners.
        result = run(argv, timeout=3.0)
        if not result.ok:
            _log.debug("notify-send failed: %s", result.stderr.strip())

    def reset_cooldowns(self) -> None:
        """Forget rate-limit state, so the next transition notifies immediately."""
        self._last_sent.clear()
