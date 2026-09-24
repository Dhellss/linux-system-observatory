"""Runtime capability detection.

Rationale
---------
The specification for this application lists many metrics that simply do not
exist on every machine: NVIDIA clocks on an AMD laptop, SMART attributes behind
a USB bridge, package power on a kernel without ``intel-rapl``.  Probing for
these repeatedly is wasteful, and branching on ``try/except`` at every call site
is unreadable.

Instead, capabilities are probed **once** at start-up into an immutable
registry.  Collectors consult the registry to decide what to attempt, and the UI
consults it to decide which panels to show at all.  Each capability records not
just presence but the *reason* for absence, which flows straight into the
``Unavailable`` values the user sees.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import cached_property

from app.models.base import Reason, Unavailable
from app.utils import sysfs
from app.utils.shell import has_tool

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Capability:
    """The result of probing for one optional platform feature."""

    name: str
    available: bool
    reason: Reason | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.available

    def as_unavailable(self) -> Unavailable:
        """Express this capability's absence as a displayable value."""
        return Unavailable(self.reason or Reason.UNSUPPORTED, self.detail)


def _tool(name: str, package_hint: str = "") -> Capability:
    """Probe for a command-line utility on ``PATH``."""
    if has_tool(name):
        return Capability(name, True)
    hint = f" (install {package_hint})" if package_hint else ""
    detail = f"{name} not found{hint}"
    return Capability(name, False, Reason.NO_TOOL, detail)


def _path(name: str, path: str, reason: Reason = Reason.UNSUPPORTED) -> Capability:
    """Probe for the existence of a sysfs/procfs path."""
    if sysfs.exists(path):
        return Capability(name, True)
    return Capability(name, False, reason, f"{path} is absent")


def _any_glob(
    name: str, pattern: str, reason: Reason = Reason.NOT_EXPOSED
) -> Capability:
    """Probe for at least one match of a sysfs glob."""
    if sysfs.glob(pattern):
        return Capability(name, True)
    return Capability(name, False, reason, f"no match for /sys/{pattern}")


class CapabilityRegistry:
    """Lazily-probed, cached view of what this machine can report.

    Every property is a :func:`functools.cached_property`, so the underlying
    filesystem or ``PATH`` probe happens at most once per process.  The registry
    is created during bootstrap and injected wherever it is needed; nothing
    constructs its own.
    """

    # ---------------------------------------------------------------- privileges

    @cached_property
    def root(self) -> bool:
        """True when running with an effective UID of 0."""
        return os.geteuid() == 0

    @cached_property
    def polkit(self) -> Capability:
        """``pkexec`` availability, used to escalate systemd unit actions."""
        return _tool("pkexec", "polkit")

    # -------------------------------------------------------------------- CPU

    @cached_property
    def cpu_frequency(self) -> Capability:
        """Per-core frequency scaling information from ``cpufreq``."""
        return _any_glob("cpu_frequency", "devices/system/cpu/cpu0/cpufreq")

    @cached_property
    def cpu_governor(self) -> Capability:
        """Scaling governor selection (absent on some virtualised kernels)."""
        return _path(
            "cpu_governor",
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
        )

    @cached_property
    def cpu_turbo(self) -> Capability:
        """Intel ``no_turbo`` / AMD ``boost`` toggle."""
        for candidate in (
            "/sys/devices/system/cpu/intel_pstate/no_turbo",
            "/sys/devices/system/cpu/cpufreq/boost",
        ):
            if sysfs.exists(candidate):
                return Capability("cpu_turbo", True, detail=candidate)
        return Capability(
            "cpu_turbo", False, Reason.NOT_EXPOSED, "no turbo/boost control node"
        )

    @cached_property
    def rapl_power(self) -> Capability:
        """Running Average Power Limit energy counters (Intel and modern AMD).

        Note that since CVE-2020-8694 most distributions restrict these
        counters to root, which is reported as a permission problem rather than
        missing hardware -- a meaningful distinction for the user.
        """
        domains = sysfs.glob("class/powercap/intel-rapl:*/energy_uj")
        if not domains:
            return Capability(
                "rapl_power", False, Reason.NO_DRIVER, "intel-rapl powercap absent"
            )
        if sysfs.read_int(domains[0]) is None:
            return Capability(
                "rapl_power", False, Reason.PERMISSION,
                "energy_uj is root-only on this kernel",
            )
        return Capability("rapl_power", True)

    # ----------------------------------------------------------------- memory

    @cached_property
    def zram(self) -> Capability:
        """Compressed RAM block devices."""
        return _any_glob("zram", "block/zram*", Reason.NO_HARDWARE)

    @cached_property
    def psi(self) -> Capability:
        """Kernel Pressure Stall Information (``CONFIG_PSI``, Linux 4.20+)."""
        return _path("psi", "/proc/pressure/memory", Reason.UNSUPPORTED)

    @cached_property
    def hugepages(self) -> Capability:
        """Huge page pools."""
        return _path("hugepages", "/sys/kernel/mm/hugepages", Reason.UNSUPPORTED)

    # -------------------------------------------------------------------- GPU

    @cached_property
    def nvidia(self) -> Capability:
        """NVIDIA management interface via ``nvidia-smi``."""
        if not has_tool("nvidia-smi"):
            return Capability(
                "nvidia", False, Reason.NO_DRIVER, "nvidia-smi not installed"
            )
        if not sysfs.exists("/proc/driver/nvidia/version"):
            return Capability(
                "nvidia", False, Reason.NO_DRIVER, "nvidia kernel module not loaded"
            )
        return Capability("nvidia", True)

    @cached_property
    def amdgpu(self) -> Capability:
        """AMD GPUs exposing the ``amdgpu`` hwmon/sysfs interface."""
        return _any_glob("amdgpu", "class/drm/card*/device/hwmon/hwmon*/temp1_input",
                         Reason.NO_DRIVER)

    @cached_property
    def intel_gpu(self) -> Capability:
        """Intel integrated graphics exposing ``i915``/``xe`` frequency nodes."""
        for pattern in ("class/drm/card*/gt_cur_freq_mhz",
                        "class/drm/card*/gt/gt0/rps_cur_freq_mhz"):
            if sysfs.glob(pattern):
                return Capability("intel_gpu", True)
        return Capability("intel_gpu", False, Reason.NO_DRIVER, "no i915/xe freq node")

    @cached_property
    def glxinfo(self) -> Capability:
        """OpenGL renderer strings."""
        return _tool("glxinfo", "mesa-utils / mesa-demos")

    @cached_property
    def vulkaninfo(self) -> Capability:
        """Vulkan device enumeration."""
        return _tool("vulkaninfo", "vulkan-tools")

    # ---------------------------------------------------------------- storage

    @cached_property
    def smartctl(self) -> Capability:
        """SMART/NVMe health attributes.

        ``smartctl`` requires raw device access, so an unprivileged session gets
        a permission reason rather than a missing-tool reason.
        """
        cap = _tool("smartctl", "smartmontools")
        if cap and not self.root:
            return Capability(
                "smartctl", False, Reason.PERMISSION,
                "SMART data requires root; run with pkexec or add a sudo rule",
            )
        return cap

    @cached_property
    def lsblk(self) -> Capability:
        """Block device topology in JSON form."""
        return _tool("lsblk", "util-linux")

    # ---------------------------------------------------------------- network

    @cached_property
    def ping(self) -> Capability:
        """ICMP latency probe."""
        return _tool("ping", "iputils")

    @cached_property
    def ss(self) -> Capability:
        """Socket statistics, used to attribute connections to processes."""
        return _tool("ss", "iproute2")

    @cached_property
    def resolvectl(self) -> Capability:
        """systemd-resolved DNS configuration query."""
        return _tool("resolvectl", "systemd-resolved")

    # ----------------------------------------------------------------- system

    @cached_property
    def systemd(self) -> Capability:
        """A live systemd instance (absent in containers and on non-systemd distros)."""
        if not has_tool("systemctl"):
            return Capability("systemd", False, Reason.NO_TOOL, "systemctl not found")
        if not sysfs.exists("/run/systemd/system"):
            return Capability(
                "systemd", False, Reason.UNSUPPORTED, "system not booted with systemd"
            )
        return Capability("systemd", True)

    @cached_property
    def journal(self) -> Capability:
        """The systemd journal."""
        return _tool("journalctl", "systemd")

    @cached_property
    def dmi(self) -> Capability:
        """DMI/SMBIOS tables describing motherboard and firmware."""
        return _path("dmi", "/sys/class/dmi/id", Reason.UNSUPPORTED)

    @cached_property
    def lspci(self) -> Capability:
        """PCI device enumeration."""
        return _tool("lspci", "pciutils")

    @cached_property
    def lsusb(self) -> Capability:
        """USB device enumeration."""
        return _tool("lsusb", "usbutils")

    @cached_property
    def upower(self) -> Capability:
        """UPower daemon, the richest source of battery health data."""
        return _tool("upower", "upower")

    @cached_property
    def battery(self) -> Capability:
        """At least one power supply of type ``Battery``."""
        for supply in sysfs.glob("class/power_supply/*"):
            if sysfs.read_text(supply / "type") == "Battery":
                return Capability("battery", True, detail=supply.name)
        return Capability("battery", False, Reason.NO_HARDWARE, "no battery present")

    @cached_property
    def hwmon(self) -> Capability:
        """Hardware monitoring chips (temperatures, fans, voltages)."""
        return _any_glob("hwmon", "class/hwmon/hwmon*", Reason.NO_HARDWARE)

    # -------------------------------------------------------------- reporting

    def snapshot(self) -> dict[str, Capability]:
        """Probe and return every capability, for diagnostics and reports."""
        result: dict[str, Capability] = {}
        for name in sorted(dir(type(self))):
            if name.startswith("_") or name in ("snapshot", "summary"):
                continue
            value = getattr(self, name)
            if isinstance(value, Capability):
                result[name] = value
        return result

    def summary(self) -> str:
        """Single-line summary used in the start-up log."""
        caps = self.snapshot()
        present = sorted(k for k, v in caps.items() if v)
        return f"{len(present)}/{len(caps)} capabilities: {', '.join(present)}"
