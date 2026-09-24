"""systemd unit collector and controller.

``systemctl`` is invoked with ``--output=json`` where available (systemd 246+),
falling back to the legacy column format.  The JSON path matters: the table
output is aligned with spaces, localised, and truncates long descriptions with an
ellipsis, all of which make it a poor parsing target.

Unit *actions* (start, stop, enable) are separated from unit *reading* by
intent.  Reading is safe and automatic; acting is privileged, requires explicit
user confirmation in the UI, and is routed through ``pkexec`` so the desktop's own
authentication dialog handles the escalation rather than this application
handling credentials itself -- which it must never do.
"""

from __future__ import annotations

import json
import re
import time

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.service import BootTiming, ServiceSnapshot, ServiceUnit, UnitState
from app.utils.shell import run

#: Unit properties fetched in one batched ``systemctl show`` call.
_SHOW_PROPERTIES = (
    "Id", "Description", "ActiveState", "SubState", "LoadState",
    "UnitFileState", "MainPID", "MemoryCurrent", "CPUUsageNSec", "TasksCurrent",
    "NRestarts", "ActiveEnterTimestampMonotonic", "Result", "FragmentPath",
)

#: Actions permitted through :meth:`ServiceCollector.control`.  An allow-list,
#: not a deny-list: an arbitrary verb must never reach the command line.
_ALLOWED_ACTIONS = frozenset(
    {"start", "stop", "restart", "reload", "enable", "disable"}
)

#: Unit names are validated against this before ever being passed to systemctl.
_UNIT_NAME = re.compile(r"^[A-Za-z0-9:_.\\@-]+\.[a-z]+$")


class ServiceCollector(Collector[ServiceSnapshot]):
    """Lists systemd units and performs privileged unit actions on request."""

    domain = "services"
    title = "Services"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._boot: BootTiming | None = None
        self._json_supported: bool | None = None

    def _collect(self) -> ServiceSnapshot:
        capability = self.capabilities.systemd
        if not capability:
            return ServiceSnapshot(
                timestamp=time.monotonic(), note=str(capability.as_unavailable())
            )
        units = self._list_units()
        return ServiceSnapshot(
            timestamp=time.monotonic(),
            units=units,
            boot=self._read_boot_timing(),
            note="" if units else "No systemd units reported",
        )

    def _empty(self) -> ServiceSnapshot:
        return ServiceSnapshot(note="Service enumeration failed")

    # ------------------------------------------------------------------- listing

    def _list_units(self) -> tuple[ServiceUnit, ...]:
        """Enumerate all units, then enrich them with resource usage."""
        summaries = self._list_unit_summaries()
        if not summaries:
            return ()
        details = self._show_units([name for name, _ in summaries])
        enablement = self._list_enablement()

        units: list[ServiceUnit] = []
        for name, description in summaries:
            detail = details.get(name, {})
            units.append(
                ServiceUnit(
                    name=name,
                    description=detail.get("Description") or description,
                    state=UnitState.parse(detail.get("ActiveState", "")),
                    sub_state=detail.get("SubState", ""),
                    load_state=detail.get("LoadState", ""),
                    enablement=enablement.get(name) or detail.get("UnitFileState")
                    or Unavailable(Reason.NOT_EXPOSED, "no unit file on disk"),
                    main_pid=self._positive_int(detail.get("MainPID")),
                    memory_bytes=self._resource_int(detail.get("MemoryCurrent")),
                    cpu_seconds=self._nanoseconds(detail.get("CPUUsageNSec")),
                    tasks=self._resource_int(detail.get("TasksCurrent")),
                    restart_count=self._positive_int(
                        detail.get("NRestarts"), zero_ok=True
                    ),
                    since=self._monotonic_timestamp(
                        detail.get("ActiveEnterTimestampMonotonic")
                    ),
                    result=detail.get("Result") or None,
                    fragment_path=detail.get("FragmentPath") or None,
                )
            )
        return tuple(sorted(units, key=lambda u: u.name))

    def _list_unit_summaries(self) -> list[tuple[str, str]]:
        """Names and descriptions of all loaded units.

        ``--all`` is required to see inactive and failed units; without it a
        failed service simply vanishes from the list, which is the opposite of
        what a monitoring tool should do.
        """
        if self._json_supported is not False:
            result = run(
                ["systemctl", "list-units", "--all", "--no-pager",
                 "--no-legend", "--output=json",
                 "--type=service,timer,socket,mount,target"],
                timeout=15.0,
            )
            if result.ok and result.stdout.strip().startswith("["):
                self._json_supported = True
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    payload = []
                return [
                    (entry.get("unit", ""), entry.get("description", ""))
                    for entry in payload
                    if entry.get("unit")
                ]
            self._json_supported = False

        # Legacy fallback: whitespace-aligned columns.
        result = run(
            ["systemctl", "list-units", "--all", "--no-pager", "--no-legend",
             "--plain", "--type=service,timer,socket,mount,target"],
            timeout=15.0,
        )
        summaries: list[tuple[str, str]] = []
        for line in result.lines:
            fields = line.split(maxsplit=4)
            if len(fields) >= 5 and "." in fields[0]:
                summaries.append((fields[0], fields[4]))
        return summaries

    def _show_units(self, names: list[str]) -> dict[str, dict[str, str]]:
        """Fetch properties for many units in as few calls as possible.

        ``systemctl show`` accepts multiple units and emits blank-line-separated
        blocks.  Batching matters: 300 units at one subprocess each would take
        many seconds, while a handful of batched calls take a fraction of one.
        The batch size is bounded so the argument list cannot exceed ``ARG_MAX``.
        """
        properties: dict[str, dict[str, str]] = {}
        batch_size = 120
        for start in range(0, len(names), batch_size):
            batch = names[start:start + batch_size]
            result = run(
                ["systemctl", "show", "--no-pager",
                 f"--property={','.join(_SHOW_PROPERTIES)}", *batch],
                timeout=20.0,
            )
            if not result.stdout:
                continue
            for block in result.stdout.split("\n\n"):
                parsed: dict[str, str] = {}
                for line in block.splitlines():
                    key, sep, value = line.partition("=")
                    if sep:
                        parsed[key.strip()] = value.strip()
                unit_id = parsed.get("Id")
                if unit_id:
                    properties[unit_id] = parsed
        return properties

    def _list_enablement(self) -> dict[str, str]:
        """Enablement state for every unit file on disk."""
        result = run(
            ["systemctl", "list-unit-files", "--no-pager", "--no-legend", "--plain"],
            timeout=15.0,
        )
        states: dict[str, str] = {}
        for line in result.lines:
            fields = line.split()
            if len(fields) >= 2:
                states[fields[0]] = fields[1]
        return states

    # -------------------------------------------------------------- boot timing

    def _read_boot_timing(self) -> BootTiming:
        """Boot performance from ``systemd-analyze``, measured once per session."""
        if self._boot is not None:
            return self._boot
        result = run(["systemd-analyze", "time"], timeout=10.0)
        timing: dict[str, float] = {}
        total: float | None = None
        if result.ok:
            # "Startup finished in 4.5s (firmware) + 3.1s (loader) + ... = 21.3s"
            for label, key in (
                ("firmware", "firmware"), ("loader", "loader"),
                ("kernel", "kernel"), ("initrd", "initrd"),
                ("userspace", "userspace"),
            ):
                pattern = r"([\d.]+)(m?s)\s*\(" + label + r"\)"
                if match := re.search(pattern, result.stdout):
                    timing[key] = self._to_seconds(match.group(1), match.group(2))
            if match := re.search(r"=\s*([\d.]+)(m?s)", result.stdout):
                total = self._to_seconds(match.group(1), match.group(2))

        self._boot = BootTiming(
            firmware_s=timing.get("firmware"),
            loader_s=timing.get("loader"),
            kernel_s=timing.get("kernel"),
            initrd_s=timing.get("initrd"),
            userspace_s=timing.get("userspace"),
            total_s=total,
            slowest=self._read_blame(),
        )
        return self._boot

    @staticmethod
    def _to_seconds(value: str, unit: str) -> float:
        """Convert a systemd duration token to seconds."""
        amount = float(value)
        return amount / 1000.0 if unit == "ms" else amount

    def _read_blame(self, limit: int = 15) -> tuple[tuple[str, float], ...]:
        """The slowest units to initialise, from ``systemd-analyze blame``."""
        result = run(["systemd-analyze", "blame", "--no-pager"], timeout=10.0)
        if not result.ok:
            return ()
        entries: list[tuple[str, float]] = []
        for line in result.lines[:limit]:
            fields = line.split(maxsplit=1)
            if len(fields) != 2:
                continue
            seconds = self._parse_blame_duration(fields[0])
            if seconds is not None:
                entries.append((fields[1], seconds))
        return tuple(entries)

    @staticmethod
    def _parse_blame_duration(token: str) -> float | None:
        """Parse compound durations such as ``1min 2.345s`` or ``890ms``."""
        total = 0.0
        matched = False
        for amount, unit in re.findall(r"([\d.]+)(min|ms|s)", token):
            matched = True
            value = float(amount)
            total += {"min": value * 60, "s": value, "ms": value / 1000}[unit]
        return total if matched else None

    # ---------------------------------------------------------------- conversion

    @staticmethod
    def _positive_int(raw: str | None, *, zero_ok: bool = False):
        """Parse an integer property, treating 0 as absent unless allowed."""
        if not raw:
            return Unavailable(Reason.NOT_EXPOSED, "not reported")
        try:
            value = int(raw)
        except ValueError:
            return Unavailable(Reason.NOT_EXPOSED, "not reported")
        if value == 0 and not zero_ok:
            return Unavailable(Reason.NOT_EXPOSED, "no main process")
        return value

    @staticmethod
    def _resource_int(raw: str | None):
        """Parse a resource counter.

        systemd returns the unsigned 64-bit maximum to mean "not accounted",
        which would otherwise render as 16 exbibytes of memory usage.
        """
        if not raw:
            return Unavailable(Reason.NOT_EXPOSED, "accounting disabled")
        try:
            value = int(raw)
        except ValueError:
            return Unavailable(Reason.NOT_EXPOSED, "accounting disabled")
        if value < 0 or value >= 2**64 - 1:
            return Unavailable(
                Reason.NOT_EXPOSED, "resource accounting not enabled for this unit"
            )
        return value

    @staticmethod
    def _nanoseconds(raw: str | None):
        """Convert a nanosecond counter to seconds."""
        if not raw:
            return Unavailable(Reason.NOT_EXPOSED, "CPU accounting disabled")
        try:
            value = int(raw)
        except ValueError:
            return Unavailable(Reason.NOT_EXPOSED, "CPU accounting disabled")
        if value <= 0 or value >= 2**64 - 1:
            return Unavailable(Reason.NOT_EXPOSED, "CPU accounting disabled")
        return value / 1_000_000_000.0

    @staticmethod
    def _monotonic_timestamp(raw: str | None):
        """Convert a systemd monotonic microsecond stamp to a Unix timestamp.

        systemd reports these relative to boot, so the boot time is added to give
        an absolute moment the UI can format as a date.
        """
        if not raw:
            return Unavailable(Reason.NOT_EXPOSED, "never activated")
        try:
            micros = int(raw)
        except ValueError:
            return Unavailable(Reason.NOT_EXPOSED, "never activated")
        if micros <= 0:
            return Unavailable(Reason.NOT_EXPOSED, "never activated")
        import psutil

        return psutil.boot_time() + micros / 1_000_000.0

    # ------------------------------------------------------------------- actions

    def dependencies(self, unit: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Forward and reverse dependencies of ``unit``.

        Returns ``(requires, required_by)``; either may be empty.
        """
        if not self._valid_unit(unit):
            return (), ()
        forward = run(
            ["systemctl", "list-dependencies", "--no-pager", "--plain", unit],
            timeout=10.0,
        )
        reverse = run(
            ["systemctl", "list-dependencies", "--reverse",
             "--no-pager", "--plain", unit],
            timeout=10.0,
        )

        def parse(result) -> tuple[str, ...]:
            names = [
                line.strip().lstrip("●○├─└│ ").strip()
                for line in result.lines[1:]
            ]
            return tuple(n for n in names if n and "." in n)

        return parse(forward), parse(reverse)

    def control(self, unit: str, action: str) -> tuple[bool, str]:
        """Perform a privileged unit action.

        Safety measures, in order:

        1. ``action`` must be in an allow-list -- an arbitrary verb can never
           reach the command line.
        2. ``unit`` must match a strict unit-name pattern, so no option or shell
           metacharacter can be smuggled through as a unit name.
        3. Escalation goes through ``pkexec``, which shows the desktop's own
           authentication dialog.  This application never sees, stores or
           forwards the user's password.

        The *decision* to act is the UI's: it must obtain explicit confirmation
        before calling this method.
        """
        if action not in _ALLOWED_ACTIONS:
            return False, f"Unsupported action: {action}"
        if not self._valid_unit(unit):
            return False, f"Refusing to act on suspicious unit name: {unit!r}"
        if not self.capabilities.systemd:
            return False, "systemd is not available on this system"

        argv = ["systemctl", action, unit]
        if not self.capabilities.root:
            if not self.capabilities.polkit:
                return False, (
                    "This action needs administrator rights, but pkexec "
                    "(polkit) is not installed"
                )
            argv = ["pkexec", *argv]

        result = run(argv, timeout=45.0)
        if result.ok:
            return True, f"{action.title()} succeeded for {unit}"
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit status {result.returncode}"
        # pkexec exits 126 when the user dismisses the authentication dialog.
        if result.returncode == 126:
            message = "Authentication was cancelled"
        return False, f"{action.title()} failed for {unit}: {message}"

    @staticmethod
    def _valid_unit(unit: str) -> bool:
        """Validate a unit name before it is passed to ``systemctl``."""
        return bool(unit) and len(unit) <= 256 and bool(_UNIT_NAME.match(unit))
