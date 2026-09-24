"""Graphics stack probe: session type, compositor, OpenGL and Vulkan.

This information belongs to the *session*, not to any single adapter: on a hybrid
laptop the OpenGL renderer string names whichever GPU the compositor chose, and
asking each adapter separately would report the same answer three times.

Everything here is effectively static for the life of the session, so it is
probed once and cached.  ``glxinfo`` and ``vulkaninfo`` are slow (tens to
hundreds of milliseconds, because they create a real context) which makes caching
essential rather than merely tidy.
"""

from __future__ import annotations

import logging
import os
import re

from app.core.capabilities import CapabilityRegistry
from app.models.base import Reason, Unavailable
from app.models.gpu import GraphicsStack
from app.utils.shell import run

_log = logging.getLogger(__name__)

#: Environment variables that identify the compositor or desktop, in priority order.
_DESKTOP_VARS = ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP")


class GraphicsStackProbe:
    """Probes the host graphics environment once and caches the result."""

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities
        self._cached: GraphicsStack | None = None

    def probe(self) -> GraphicsStack:
        """Return the graphics stack description, probing on first call."""
        if self._cached is None:
            self._cached = self._build()
        return self._cached

    def _build(self) -> GraphicsStack:
        """Assemble the stack description from the environment and helper tools."""
        session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
        opengl = self._probe_opengl()
        vulkan = self._probe_vulkan()
        return GraphicsStack(
            session_type=self._session_label(session),
            compositor=self._detect_compositor(session),
            opengl_renderer=opengl.get("renderer", self._no_glxinfo()),
            opengl_version=opengl.get("version", self._no_glxinfo()),
            opengl_vendor=opengl.get("vendor", self._no_glxinfo()),
            glsl_version=opengl.get("glsl", self._no_glxinfo()),
            vulkan_api_version=vulkan[0],
            vulkan_devices=vulkan[1],
            display_server_version=self._display_server_version(session),
        )

    # ------------------------------------------------------------------- session

    @staticmethod
    def _session_label(session: str) -> str | Unavailable:
        """Normalise the session type into a display label."""
        return {
            "wayland": "Wayland",
            "x11": "X11",
            "tty": "Console (no display server)",
        }.get(session) or (
            session.title() if session
            else Unavailable(Reason.NOT_EXPOSED, "XDG_SESSION_TYPE not set")
        )

    @staticmethod
    def _detect_compositor(session: str) -> str | Unavailable:
        """Identify the compositor or desktop environment.

        The desktop environment variables are checked first because they are the
        most reliable; ``WAYLAND_DISPLAY`` only confirms *that* a compositor is
        running, not which one.
        """
        for variable in _DESKTOP_VARS:
            if value := os.environ.get(variable, "").strip():
                return value.replace(":", " / ")
        if session == "wayland" and os.environ.get("WAYLAND_DISPLAY"):
            return "Unidentified Wayland compositor"
        return Unavailable(Reason.NOT_EXPOSED, "no desktop environment advertised")

    def _display_server_version(self, session: str) -> str | Unavailable:
        """Version of the running display server, where it can be determined."""
        if session == "x11":
            result = run(["Xorg", "-version"], timeout=2.0)
            blob = result.stdout + result.stderr
            if match := re.search(r"X\.Org X Server ([\d.]+)", blob):
                return f"X.Org {match.group(1)}"
        # Wayland has no protocol version to query; report the socket instead,
        # which at least confirms the compositor connection.
        if session == "wayland" and (display := os.environ.get("WAYLAND_DISPLAY")):
            return f"Wayland socket {display}"
        return Unavailable(Reason.NOT_EXPOSED, "display server version not queryable")

    # -------------------------------------------------------------------- OpenGL

    def _no_glxinfo(self) -> Unavailable:
        """The reason OpenGL details are missing."""
        return self._capabilities.glxinfo.as_unavailable()

    def _probe_opengl(self) -> dict[str, str]:
        """Parse the renderer strings out of ``glxinfo -B``.

        ``-B`` produces a short summary instead of the full extension dump, which
        is both faster to run and far easier to parse reliably.
        """
        if not self._capabilities.glxinfo:
            return {}
        result = run(["glxinfo", "-B"], timeout=6.0)
        if not result.ok:
            return {}
        fields: dict[str, str] = {}
        for line in result.lines:
            if line.startswith("OpenGL renderer string:"):
                fields["renderer"] = line.split(":", 1)[1].strip()
            elif line.startswith("OpenGL version string:"):
                fields["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("OpenGL vendor string:"):
                fields["vendor"] = line.split(":", 1)[1].strip()
            elif line.startswith("OpenGL shading language version string:"):
                fields["glsl"] = line.split(":", 1)[1].strip()
        return fields

    # -------------------------------------------------------------------- Vulkan

    def _probe_vulkan(self) -> tuple[str | Unavailable, tuple[str, ...]]:
        """Enumerate Vulkan devices and the instance API version.

        ``vulkaninfo --summary`` is used because the full output is megabytes of
        extension tables.  Even the summary can take a second to produce, which
        is precisely why this whole probe is cached for the session.
        """
        if not self._capabilities.vulkaninfo:
            reason = self._capabilities.vulkaninfo.as_unavailable()
            return reason, ()
        result = run(["vulkaninfo", "--summary"], timeout=10.0)
        if not result.ok:
            return (
                Unavailable(Reason.ERROR, "vulkaninfo failed; no Vulkan driver?"),
                (),
            )
        api_version: str | Unavailable = Unavailable(
            Reason.NOT_EXPOSED, "API version not reported"
        )
        devices: list[str] = []
        for line in result.lines:
            if "apiVersion" in line and isinstance(api_version, Unavailable):
                if match := re.search(r"=\s*([\d.]+)", line):
                    api_version = match.group(1)
            elif "deviceName" in line:
                name = line.split("=", 1)[-1].strip()
                if name and name not in devices:
                    devices.append(name)
        return api_version, tuple(devices)
