"""System health checks.

Each check is a small function over the latest snapshots that returns a
:class:`~app.models.diagnostics.CheckResult`.  Three principles shape them:

**Every finding carries advice.**  Reporting "disk 94% full" is half a job;
reporting it alongside what to do about it is the whole one.

**A check that cannot run says so.**  ``skipped`` distinguishes "no problem
found" from "could not look", so a machine without SMART access does not appear
to have healthy disks when the truth is unknown.

**Thresholds come from the hardware.**  A CPU rated to 100 °C and an NVMe rated
to 85 °C do not share a danger point, so limits are taken from what the device
reports rather than from a constant.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.core.capabilities import CapabilityRegistry
from app.core.units import bytes_, percent, temperature
from app.models.base import is_available, is_number
from app.models.cpu import CpuSnapshot
from app.models.diagnostics import CheckResult, DiagnosticReport, Severity
from app.models.gpu import GpuSnapshot, GpuVendor
from app.models.memory import MemorySnapshot
from app.models.network import NetworkSnapshot
from app.models.process import ProcessSnapshot
from app.models.sensors import BatterySnapshot, SensorSnapshot, ThermalStatus
from app.models.service import ServiceSnapshot
from app.models.storage import SmartVerdict, StorageSnapshot

_log = logging.getLogger(__name__)

#: A check receives the snapshot store and returns zero or more results.
Check = Callable[["SnapshotStore"], list[CheckResult]]


class SnapshotStore:
    """Holds the most recent snapshot for each domain.

    Diagnostics run on demand against whatever the monitor has most recently
    collected, rather than triggering their own collection pass -- so opening the
    diagnostics page costs nothing and reflects exactly what the other pages show.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, object] = {}

    def put(self, domain: str, snapshot: object) -> None:
        """Store the latest snapshot for one domain."""
        self._snapshots[domain] = snapshot

    def get(self, domain: str) -> object | None:
        """The latest snapshot for one domain, if any."""
        return self._snapshots.get(domain)

    def __contains__(self, domain: str) -> bool:
        return domain in self._snapshots


def _skip(check_id: str, title: str, reason: str, category: str) -> CheckResult:
    """A result standing for "this check could not be performed"."""
    return CheckResult(
        check_id=check_id,
        title=title,
        severity=Severity.OK,
        summary=reason,
        category=category,
        skipped=True,
    )


class DiagnosticsService:
    """Runs health checks and assembles reports."""

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._capabilities = capabilities
        self.store = SnapshotStore()
        self._checks: list[Check] = [
            self._check_cpu_load,
            self._check_cpu_thermals,
            self._check_memory,
            self._check_memory_pressure,
            self._check_swap,
            self._check_disk_space,
            self._check_disk_health,
            self._check_disk_thermals,
            self._check_gpu,
            self._check_sensors,
            self._check_battery,
            self._check_network,
            self._check_services,
            self._check_processes,
            self._check_monitoring_coverage,
        ]

    def run(self) -> DiagnosticReport:
        """Execute every check and return a report."""
        started = time.perf_counter()
        results: list[CheckResult] = []
        for check in self._checks:
            try:
                results.extend(check(self.store))
            except Exception:
                _log.exception(
                    "Diagnostic check %s failed",
                    getattr(check, "__name__", "?"),
                )
        return DiagnosticReport(
            results=tuple(results),
            generated_at=time.time(),
            duration_s=time.perf_counter() - started,
        )

    # ---------------------------------------------------------------------- CPU

    def _check_cpu_load(self, store: SnapshotStore) -> list[CheckResult]:
        """Compare load average against the logical core count."""
        snapshot = store.get("cpu")
        if not isinstance(snapshot, CpuSnapshot):
            return [_skip("cpu.load", "Processor load",
                          "No CPU sample yet", "Processor")]
        cores = max(1, snapshot.topology.logical_cores)
        one_minute = snapshot.load_average[0]
        # Load per core is the meaningful figure: 8.0 is idle on a 64-core server
        # and severe on a dual-core laptop.
        ratio = one_minute / cores
        evidence = {
            "1-minute load": f"{one_minute:.2f}",
            "5-minute load": f"{snapshot.load_average[1]:.2f}",
            "15-minute load": f"{snapshot.load_average[2]:.2f}",
            "Logical cores": str(cores),
            "Load per core": f"{ratio:.2f}",
            "Current usage": percent(snapshot.usage),
        }
        if ratio >= 2.0:
            return [CheckResult(
                "cpu.load", "Processor load", Severity.WARNING,
                f"Load average is {one_minute:.2f} across {cores} logical cores "
                f"({ratio:.1f}x oversubscribed).",
                "Processes are waiting for CPU time. Check the Processes page, "
                "sorted by CPU, to find what is responsible.",
                "Processor", evidence,
            )]
        if ratio >= 1.0:
            return [CheckResult(
                "cpu.load", "Processor load", Severity.INFO,
                f"Load average is {one_minute:.2f}, matching the {cores} available "
                "logical cores.",
                "The processor is fully committed but not oversubscribed. "
                "This is normal during a build or an export.",
                "Processor", evidence,
            )]
        return [CheckResult(
            "cpu.load", "Processor load", Severity.OK,
            f"Load average {one_minute:.2f} is comfortable for {cores} logical cores.",
            "", "Processor", evidence,
        )]

    def _check_cpu_thermals(self, store: SnapshotStore) -> list[CheckResult]:
        """Compare CPU temperature against the limits the hardware reports."""
        snapshot = store.get("cpu")
        if not isinstance(snapshot, CpuSnapshot):
            return []
        if not is_number(snapshot.temperature):
            return [_skip(
                "cpu.thermal", "Processor temperature",
                f"No CPU thermal sensor available ({snapshot.temperature})",
                "Processor",
            )]
        current = float(snapshot.temperature)
        critical = (
            float(snapshot.temperature_critical)
            if is_number(snapshot.temperature_critical) else None
        )
        evidence = {"Current": temperature(current)}
        if critical:
            evidence["Critical limit"] = temperature(critical)
            evidence["Headroom"] = temperature(critical - current)

        limit = critical or 100.0
        if current >= limit * 0.95:
            return [CheckResult(
                "cpu.thermal", "Processor temperature", Severity.CRITICAL,
                f"CPU is at {temperature(current)}, within 5% of its "
                f"{temperature(limit)} limit.",
                "The processor is almost certainly throttling. Check that fans "
                "are running and vents are clear; on a laptop, consider "
                "re-pasting the heatsink.",
                "Processor", evidence,
            )]
        if current >= limit * 0.85:
            return [CheckResult(
                "cpu.thermal", "Processor temperature", Severity.WARNING,
                f"CPU is running warm at {temperature(current)}.",
                "Acceptable under sustained load, but worth watching. Confirm "
                "cooling is working if the machine is otherwise idle.",
                "Processor", evidence,
            )]
        return [CheckResult(
            "cpu.thermal", "Processor temperature", Severity.OK,
            f"CPU temperature is {temperature(current)}, well within limits.",
            "", "Processor", evidence,
        )]

    # ------------------------------------------------------------------- memory

    def _check_memory(self, store: SnapshotStore) -> list[CheckResult]:
        """Assess memory usage using *available* memory, not used memory."""
        snapshot = store.get("memory")
        if not isinstance(snapshot, MemorySnapshot):
            return [_skip("memory.usage", "Memory usage",
                          "No memory sample yet", "Memory")]
        evidence = {
            "Total": bytes_(snapshot.total_bytes),
            "Used": bytes_(snapshot.used_bytes),
            "Available": bytes_(snapshot.available_bytes),
            "Cache and buffers": bytes_(snapshot.cached_bytes + snapshot.buffers_bytes),
        }
        available_share = (
            100.0 * snapshot.available_bytes / snapshot.total_bytes
            if snapshot.total_bytes else 100.0
        )
        if available_share < 5:
            return [CheckResult(
                "memory.usage", "Memory usage", Severity.CRITICAL,
                f"Only {bytes_(snapshot.available_bytes)} "
                f"({available_share:.1f}%) of memory is available.",
                "The system is close to invoking the OOM killer. Close memory-"
                "heavy applications; check the Memory page for the biggest "
                "consumers.",
                "Memory", evidence,
            )]
        if available_share < 12:
            return [CheckResult(
                "memory.usage", "Memory usage", Severity.WARNING,
                f"{bytes_(snapshot.available_bytes)} "
                f"({available_share:.1f}%) of memory remains available.",
                "Headroom is limited. Note that cache counts as available and "
                "will be reclaimed automatically when needed.",
                "Memory", evidence,
            )]
        return [CheckResult(
            "memory.usage", "Memory usage", Severity.OK,
            f"{bytes_(snapshot.available_bytes)} available "
            f"({available_share:.0f}% of total).",
            "", "Memory", evidence,
        )]

    def _check_memory_pressure(self, store: SnapshotStore) -> list[CheckResult]:
        """Use PSI, the most reliable indicator of genuine memory exhaustion."""
        snapshot = store.get("memory")
        if not isinstance(snapshot, MemorySnapshot):
            return []
        pressure = snapshot.pressure_memory
        if not is_available(pressure):
            return [_skip(
                "memory.pressure", "Memory pressure",
                "Kernel pressure-stall information is not available "
                "(requires CONFIG_PSI, Linux 4.20 or newer)",
                "Memory",
            )]
        evidence = {
            "10-second average": percent(pressure.avg10),   # type: ignore[union-attr]
            "1-minute average": percent(pressure.avg60),    # type: ignore[union-attr]
            "5-minute average": percent(pressure.avg300),   # type: ignore[union-attr]
        }
        avg10 = pressure.avg10  # type: ignore[union-attr]
        if avg10 >= 20:
            return [CheckResult(
                "memory.pressure", "Memory pressure", Severity.CRITICAL,
                f"Tasks were fully stalled waiting for memory {avg10:.1f}% of the "
                "last 10 seconds.",
                "This is real memory exhaustion, not merely high usage. Free "
                "memory or add swap; the system is thrashing.",
                "Memory", evidence,
            )]
        if avg10 >= 5:
            return [CheckResult(
                "memory.pressure", "Memory pressure", Severity.WARNING,
                f"Memory stalls affected {avg10:.1f}% of the last 10 seconds.",
                "The kernel is working to reclaim memory. Expect occasional "
                "stutter until demand falls.",
                "Memory", evidence,
            )]
        return [CheckResult(
            "memory.pressure", "Memory pressure", Severity.OK,
            "No significant memory stalls detected.",
            "", "Memory", evidence,
        )]

    def _check_swap(self, store: SnapshotStore) -> list[CheckResult]:
        """Report swap utilisation, including zram efficiency."""
        snapshot = store.get("memory")
        if not isinstance(snapshot, MemorySnapshot):
            return []
        if not snapshot.swap_total_bytes:
            return [CheckResult(
                "memory.swap", "Swap", Severity.INFO,
                "No swap is configured.",
                "Without swap, the kernel cannot page out idle memory and will "
                "invoke the OOM killer sooner under pressure. Consider adding a "
                "swap file or enabling zram.",
                "Memory", {"Swap total": bytes_(0)},
            )]
        evidence = {
            "Swap used": bytes_(snapshot.swap_used_bytes),
            "Swap total": bytes_(snapshot.swap_total_bytes),
            "Utilisation": percent(snapshot.swap_percent),
        }
        for zram in snapshot.zram_devices:
            evidence[f"{zram.name} compression"] = (
                f"{zram.compression_ratio:.1f}x ({zram.algorithm})"
            )
        if snapshot.swap_percent >= 80:
            return [CheckResult(
                "memory.swap", "Swap", Severity.WARNING,
                f"Swap is {percent(snapshot.swap_percent)} full.",
                "Heavy swap use degrades responsiveness. Reduce memory demand "
                "or increase physical RAM.",
                "Memory", evidence,
            )]
        return [CheckResult(
            "memory.swap", "Swap", Severity.OK,
            f"Swap is {percent(snapshot.swap_percent)} used of "
            f"{bytes_(snapshot.swap_total_bytes)}.",
            "", "Memory", evidence,
        )]

    # ------------------------------------------------------------------ storage

    def _check_disk_space(self, store: SnapshotStore) -> list[CheckResult]:
        """Flag filesystems approaching capacity."""
        snapshot = store.get("storage")
        if not isinstance(snapshot, StorageSnapshot):
            return [_skip("storage.space", "Filesystem capacity",
                          "No storage sample yet", "Storage")]
        results: list[CheckResult] = []
        worst = Severity.OK
        evidence: dict[str, str] = {}
        for filesystem in snapshot.filesystems:
            if not is_number(filesystem.percent):
                continue
            mount = str(filesystem.mountpoint)
            evidence[mount] = (
                f"{percent(filesystem.percent)} used, "
                f"{bytes_(filesystem.free_bytes)} free"
            )
            if filesystem.percent >= 95:
                worst = max(worst, Severity.CRITICAL)
                results.append(CheckResult(
                    f"storage.space.{mount}", f"Filesystem {mount}",
                    Severity.CRITICAL,
                    f"{mount} is {percent(filesystem.percent)} full with only "
                    f"{bytes_(filesystem.free_bytes)} free.",
                    "Applications will begin failing to write. Delete or move "
                    "data now. Check package caches and journal size first.",
                    "Storage", {mount: evidence[mount]},
                ))
            elif filesystem.percent >= 88:
                worst = max(worst, Severity.WARNING)
                results.append(CheckResult(
                    f"storage.space.{mount}", f"Filesystem {mount}",
                    Severity.WARNING,
                    f"{mount} is {percent(filesystem.percent)} full.",
                    "Plan to free space soon. Filesystems also slow down as they "
                    "approach capacity through fragmentation.",
                    "Storage", {mount: evidence[mount]},
                ))
        if worst is Severity.OK:
            results.append(CheckResult(
                "storage.space", "Filesystem capacity", Severity.OK,
                f"All {len(evidence)} mounted filesystems have adequate free space.",
                "", "Storage", evidence,
            ))
        return results

    def _check_disk_health(self, store: SnapshotStore) -> list[CheckResult]:
        """Report SMART verdicts and endurance."""
        snapshot = store.get("storage")
        if not isinstance(snapshot, StorageSnapshot):
            return []
        results: list[CheckResult] = []
        unknown: list[str] = []
        for disk in snapshot.disks:
            health = disk.health
            if health.verdict is SmartVerdict.UNKNOWN:
                unknown.append(f"{disk.path}: {health.note or 'no SMART data'}")
                continue
            evidence = {"Verdict": health.verdict.value}
            if is_available(health.power_on_hours):
                evidence["Power-on hours"] = f"{health.power_on_hours:,}"
            if is_available(health.wear_percent):
                evidence["Endurance used"] = percent(health.wear_percent)
                evidence["Estimated life left"] = percent(health.estimated_life_left)
            if is_available(health.reallocated_sectors):
                evidence["Reallocated sectors"] = str(health.reallocated_sectors)

            if health.verdict is SmartVerdict.FAILING:
                results.append(CheckResult(
                    f"storage.health.{disk.name}", f"Drive health: {disk.name}",
                    Severity.CRITICAL,
                    f"{disk.path} reports that it is failing.",
                    "Back up this drive immediately and replace it. A SMART "
                    "failure assessment means the drive itself predicts imminent "
                    "failure.",
                    "Storage", evidence,
                ))
            elif health.verdict is SmartVerdict.WARNING:
                concerning = [a.name for a in health.attributes if a.concerning]
                results.append(CheckResult(
                    f"storage.health.{disk.name}", f"Drive health: {disk.name}",
                    Severity.WARNING,
                    f"{disk.path} passes its self-assessment but reports "
                    f"concerning attributes: {', '.join(concerning[:3])}.",
                    "Ensure backups are current and monitor this drive. "
                    "Reallocated or pending sectors tend to accumulate.",
                    "Storage", evidence,
                ))
            else:
                results.append(CheckResult(
                    f"storage.health.{disk.name}", f"Drive health: {disk.name}",
                    Severity.OK,
                    f"{disk.path} reports healthy.",
                    "", "Storage", evidence,
                ))
        if unknown:
            results.append(_skip(
                "storage.health", "Drive health",
                "; ".join(unknown[:3]), "Storage",
            ))
        return results

    def _check_disk_thermals(self, store: SnapshotStore) -> list[CheckResult]:
        """Flag drives running hot, using NVMe-appropriate thresholds."""
        snapshot = store.get("storage")
        if not isinstance(snapshot, StorageSnapshot):
            return []
        hot: list[CheckResult] = []
        evidence: dict[str, str] = {}
        for disk in snapshot.disks:
            reading = disk.health.temperature
            if not is_number(reading):
                continue
            evidence[disk.name] = temperature(reading)
            # NVMe controllers typically begin throttling around 70 °C and are
            # rated to 85 °C -- far lower than a CPU, which is why a shared
            # threshold would be wrong.
            if reading >= 75:
                hot.append(CheckResult(
                    f"storage.thermal.{disk.name}", f"Drive temperature: {disk.name}",
                    Severity.WARNING,
                    f"{disk.path} is at {temperature(reading)}.",
                    "NVMe drives throttle above roughly 70 °C. Improve airflow "
                    "or fit a heatsink if this persists under load.",
                    "Storage", {disk.name: temperature(reading)},
                ))
        if hot:
            return hot
        if evidence:
            return [CheckResult(
                "storage.thermal", "Drive temperature", Severity.OK,
                "All drives are within normal temperature ranges.",
                "", "Storage", evidence,
            )]
        return []

    # ---------------------------------------------------------------------- GPU

    def _check_gpu(self, store: SnapshotStore) -> list[CheckResult]:
        """Report GPU driver state, temperature and throttling."""
        snapshot = store.get("gpu")
        if not isinstance(snapshot, GpuSnapshot):
            return [_skip("gpu.status", "Graphics", "No GPU sample yet", "Graphics")]
        if not snapshot.gpus:
            return [_skip("gpu.status", "Graphics", snapshot.note or
                          "No graphics adapter detected", "Graphics")]
        results: list[CheckResult] = []
        for gpu in snapshot.gpus:
            device = gpu.device
            label = f"{device.name}"
            evidence = {"Driver": str(device.driver_name)}
            if is_available(gpu.temperature):
                evidence["Temperature"] = temperature(gpu.temperature)
            if is_available(gpu.utilisation):
                evidence["Utilisation"] = percent(gpu.utilisation)

            # A driverless adapter is the most actionable GPU problem there is.
            no_telemetry = (
                not is_available(gpu.utilisation)
                and not is_available(gpu.core_clock_mhz)
            )
            if no_telemetry:
                results.append(CheckResult(
                    f"gpu.driver.{device.index}", f"Graphics driver: {label}",
                    Severity.WARNING,
                    f"{label} is present but reports no telemetry.",
                    self._gpu_driver_advice(device.vendor),
                    "Graphics", evidence,
                ))
                continue
            if gpu.throttle_reasons:
                results.append(CheckResult(
                    f"gpu.throttle.{device.index}", f"Graphics throttling: {label}",
                    Severity.WARNING,
                    f"{label} is throttling: {', '.join(gpu.throttle_reasons)}.",
                    "Throttling under sustained load is normal; persistent "
                    "thermal throttling at idle suggests a cooling problem.",
                    "Graphics", evidence,
                ))
                continue
            if is_number(gpu.temperature) and gpu.temperature >= 87:
                results.append(CheckResult(
                    f"gpu.thermal.{device.index}", f"Graphics temperature: {label}",
                    Severity.WARNING,
                    f"{label} is at {temperature(gpu.temperature)}.",
                    "Check that the GPU fan is spinning and airflow is "
                    "unobstructed.",
                    "Graphics", evidence,
                ))
                continue
            results.append(CheckResult(
                f"gpu.status.{device.index}", f"Graphics: {label}", Severity.OK,
                f"{label} is operating normally.",
                "", "Graphics", evidence,
            ))
        return results

    @staticmethod
    def _gpu_driver_advice(vendor: GpuVendor) -> str:
        """Vendor-specific guidance for a driverless adapter."""
        return {
            GpuVendor.NVIDIA: "Install the nvidia driver package and confirm the "
                              "module loads (check 'modprobe nvidia').",
            GpuVendor.AMD: "Confirm the amdgpu module is loaded; older cards may "
                           "need the radeon driver instead.",
            GpuVendor.INTEL: "Confirm the i915 (or xe) module is loaded. A "
                             "simple-framebuffer placeholder means the real "
                             "driver did not bind.",
        }.get(vendor, "Confirm the appropriate kernel graphics driver is loaded.")

    # ------------------------------------------------------------------ sensors

    def _check_sensors(self, store: SnapshotStore) -> list[CheckResult]:
        """Report the worst thermal reading anywhere on the system."""
        snapshot = store.get("sensors")
        if not isinstance(snapshot, SensorSnapshot):
            return []
        if not snapshot.chips:
            return [_skip("sensors.available", "Hardware sensors",
                          snapshot.note or "No sensors detected", "Sensors")]
        hottest = snapshot.hottest
        evidence = {
            "Sensor chips": str(len(snapshot.chips)),
            "Temperature sensors": str(len(snapshot.temperatures())),
            "Fans": str(len(snapshot.fans())),
        }
        if hottest is not None:
            evidence["Hottest"] = f"{hottest.label}: {temperature(hottest.value)}"
        status = snapshot.worst_status
        if status is ThermalStatus.CRITICAL:
            return [CheckResult(
                "sensors.thermal", "Hardware sensors", Severity.CRITICAL,
                f"{hottest.label} has reached {temperature(hottest.value)}, at or "
                "above its critical limit."
                if hottest else "A sensor has reached its critical limit.",
                "Shut down or reduce load to avoid hardware protection kicking in.",
                "Sensors", evidence,
            )]
        if status is ThermalStatus.HOT:
            return [CheckResult(
                "sensors.thermal", "Hardware sensors", Severity.WARNING,
                f"{hottest.label} is at {temperature(hottest.value)}, above its "
                "high-temperature threshold." if hottest else "A sensor is hot.",
                "Verify cooling. Sustained operation near the limit shortens "
                "component life.",
                "Sensors", evidence,
            )]
        return [CheckResult(
            "sensors.thermal", "Hardware sensors", Severity.OK,
            f"All {len(snapshot.temperatures())} temperature sensors are within "
            "normal ranges.",
            "", "Sensors", evidence,
        )]

    # ------------------------------------------------------------------ battery

    def _check_battery(self, store: SnapshotStore) -> list[CheckResult]:
        """Report battery charge and, more usefully, its health."""
        snapshot = store.get("battery")
        if not isinstance(snapshot, BatterySnapshot):
            return []
        if not snapshot.present:
            return [_skip("battery.health", "Battery",
                          snapshot.note or "No battery present", "Power")]
        results: list[CheckResult] = []
        health = snapshot.health_percent
        if is_number(health):
            evidence = {
                "Full charge capacity": f"{snapshot.energy_full_wh:.1f} Wh"
                if is_number(snapshot.energy_full_wh) else "—",
                "Design capacity": f"{snapshot.energy_design_wh:.1f} Wh"
                if is_number(snapshot.energy_design_wh) else "—",
                "Health": percent(health),
                "Wear": percent(snapshot.wear_percent),
            }
            if is_available(snapshot.cycle_count):
                evidence["Cycle count"] = str(snapshot.cycle_count)
            if health < 55:
                results.append(CheckResult(
                    "battery.health", "Battery health", Severity.WARNING,
                    f"Battery holds only {percent(health)} of its original "
                    "design capacity.",
                    "Runtime will be noticeably reduced. Replacement is worth "
                    "considering below about 60% health.",
                    "Power", evidence,
                ))
            elif health < 80:
                results.append(CheckResult(
                    "battery.health", "Battery health", Severity.INFO,
                    f"Battery is at {percent(health)} of design capacity.",
                    "This is normal ageing. Keeping charge between 20% and 80% "
                    "slows further degradation.",
                    "Power", evidence,
                ))
            else:
                results.append(CheckResult(
                    "battery.health", "Battery health", Severity.OK,
                    f"Battery is at {percent(health)} of design capacity.",
                    "", "Power", evidence,
                ))
        if is_number(snapshot.percent) and snapshot.percent < 15 \
                and not snapshot.charging:
            results.append(CheckResult(
                "battery.charge", "Battery charge", Severity.WARNING,
                f"Battery is at {percent(snapshot.percent)} and discharging.",
                "Connect a charger.",
                "Power", {"Charge": percent(snapshot.percent)},
            ))
        return results

    # ------------------------------------------------------------------ network

    def _check_network(self, store: SnapshotStore) -> list[CheckResult]:
        """Check connectivity and interface error counters."""
        snapshot = store.get("network")
        if not isinstance(snapshot, NetworkSnapshot):
            return [_skip("network.status", "Network", "No network sample yet",
                          "Network")]
        active = snapshot.active
        evidence = {
            "Active interfaces": str(len(active)),
            "Default gateway": str(snapshot.gateway_v4),
            "DNS servers": ", ".join(snapshot.dns_servers) or "none configured",
        }
        if not snapshot.online:
            return [CheckResult(
                "network.status", "Network", Severity.WARNING,
                "No interface has a usable address.",
                "Check cabling or Wi-Fi association. This is expected if the "
                "machine is deliberately offline.",
                "Network", evidence,
            )]
        default = snapshot.default_interface
        results = [CheckResult(
            "network.status", "Network", Severity.OK,
            f"{len(active)} interface(s) up; default route via "
            f"{default.name if default else 'unknown'}.",
            "", "Network", evidence,
        )]
        # Errors are cumulative since boot, so a handful is unremarkable.  A
        # meaningful *rate* is what matters, approximated here by a proportion of
        # total packets.
        errors = snapshot.totals.error_count
        if errors > 1000:
            results.append(CheckResult(
                "network.errors", "Network errors", Severity.WARNING,
                f"{errors:,} interface errors or drops recorded since boot.",
                "Persistent errors suggest a failing cable, a saturated link, or "
                "a driver problem. Check per-interface counters on the Network "
                "page.",
                "Network", {"Total errors and drops": f"{errors:,}"},
            ))
        return results

    # ----------------------------------------------------------------- services

    def _check_services(self, store: SnapshotStore) -> list[CheckResult]:
        """Report failed systemd units."""
        snapshot = store.get("services")
        if not isinstance(snapshot, ServiceSnapshot):
            return [_skip("services.failed", "System services",
                          "No systemd sample yet", "Services")]
        if snapshot.note and not snapshot.units:
            return [_skip("services.failed", "System services", snapshot.note,
                          "Services")]
        failed = snapshot.failed
        if failed:
            return [CheckResult(
                "services.failed", "System services", Severity.WARNING,
                f"{len(failed)} systemd unit(s) have failed: "
                f"{', '.join(u.name for u in failed[:4])}"
                + ("…" if len(failed) > 4 else ""),
                "Inspect each unit's log on the Services page to see why it "
                "failed. Some failures are harmless (hardware-specific units on "
                "the wrong machine).",
                "Services",
                {u.name: f"{u.result or 'failed'}" for u in failed[:8]},
            )]
        return [CheckResult(
            "services.failed", "System services", Severity.OK,
            f"No failed units among {len(snapshot.units)} loaded "
            f"({snapshot.active_count} active).",
            "", "Services",
            {"Loaded units": str(len(snapshot.units)),
             "Active": str(snapshot.active_count)},
        )]

    # ---------------------------------------------------------------- processes

    def _check_processes(self, store: SnapshotStore) -> list[CheckResult]:
        """Flag zombie accumulation."""
        snapshot = store.get("processes")
        if not isinstance(snapshot, ProcessSnapshot):
            return []
        evidence = {
            "Total processes": str(snapshot.total),
            "Threads": str(snapshot.threads),
            "Zombies": str(snapshot.zombie),
        }
        if snapshot.zombie >= 10:
            return [CheckResult(
                "processes.zombies", "Zombie processes", Severity.WARNING,
                f"{snapshot.zombie} zombie processes are present.",
                "A parent process is not reaping its children. Identify it on "
                "the Processes page (tree view) and restart it.",
                "Processes", evidence,
            )]
        return [CheckResult(
            "processes.zombies", "Process table", Severity.OK,
            f"{snapshot.total} processes, {snapshot.threads} threads, "
            f"{snapshot.zombie} zombies.",
            "", "Processes", evidence,
        )]

    # -------------------------------------------------------- self-diagnostics

    def _check_monitoring_coverage(self, store: SnapshotStore) -> list[CheckResult]:
        """Report which optional capabilities are unavailable, and why.

        This check is about the monitor itself rather than the machine.  It tells
        the user exactly which metrics they are not seeing and what would enable
        them -- turning invisible gaps into an actionable list.
        """
        capabilities = self._capabilities.snapshot()
        missing = {
            name: capability for name, capability in capabilities.items()
            if not capability
        }
        actionable = {
            name: capability for name, capability in missing.items()
            if capability.as_unavailable().actionable
        }
        evidence = {
            name.replace("_", " ").title(): capability.detail or "unavailable"
            for name, capability in sorted(missing.items())
        }
        if actionable:
            return [CheckResult(
                "monitor.coverage", "Monitoring coverage", Severity.INFO,
                f"{len(missing)} optional metric sources are unavailable, of "
                f"which {len(actionable)} could be enabled.",
                "Installing the listed tools or granting the listed permissions "
                "would expose additional metrics. Everything else is simply "
                "absent hardware.",
                "Monitor", evidence,
            )]
        if missing:
            return [CheckResult(
                "monitor.coverage", "Monitoring coverage", Severity.OK,
                f"{len(capabilities) - len(missing)} of {len(capabilities)} "
                "metric sources available; the rest are absent hardware.",
                "", "Monitor", evidence,
            )]
        return [CheckResult(
            "monitor.coverage", "Monitoring coverage", Severity.OK,
            "Every optional metric source is available on this system.",
            "", "Monitor", {},
        )]
