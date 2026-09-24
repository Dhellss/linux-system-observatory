"""CPU telemetry collector.

Sources, and why each was chosen
--------------------------------
``/proc/stat``
    Read directly rather than through ``psutil.cpu_times_percent`` so that the
    per-core breakdown and the aggregate come from a *single* read of the same
    file.  Mixing sources produces cores that sum to more than the total.
``/sys/devices/system/cpu/cpu*/cpufreq``
    Per-core frequency.  ``scaling_cur_freq`` reflects the governor's request;
    on Intel with ``intel_pstate`` this is the only figure available without
    reading MSRs, so it is what we report.
``/proc/cpuinfo``
    Model, flags, microcode.  Parsed once -- it is static and surprisingly
    expensive on high-core-count machines.
``intel-rapl`` powercap
    Package power, derived from the energy counter's delta.  Root-only on most
    modern kernels, which the capability registry reports as a permission issue.
"""

from __future__ import annotations

import os
import platform
import re
import sys
import time
from typing import TypedDict

import psutil

from app.collectors.base import Collector
from app.models.base import Maybe, Reason, Unavailable
from app.models.cpu import CoreSample, CpuSnapshot, CpuTimes, CpuTopology
from app.utils import sysfs
from app.utils.shell import run

_CPU_DIR = sysfs.SYS / "devices/system/cpu"

#: ``/proc/stat`` field order for the ``cpu`` lines (Linux 2.6.33+).
_STAT_FIELDS = (
    "user", "nice", "system", "idle", "iowait",
    "irq", "softirq", "steal", "guest", "guest_nice",
)


def _optional_int(value: float | None) -> int | None:
    """Narrow an optional counter to ``int``, as the model declares it."""
    return None if value is None else int(value)


class _Thermal(TypedDict):
    """CPU temperature reading with the limits the hardware advertises."""

    package: Maybe[float]
    high: Maybe[float]
    critical: Maybe[float]
    per_core: dict[int, float]


class CpuCollector(Collector[CpuSnapshot]):
    """Samples processor load, frequency, thermals and topology."""

    domain = "cpu"
    title = "Processor"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._topology: CpuTopology | None = None
        self._previous_stat: dict[str, tuple[int, ...]] = {}
        self._core_temp_map: dict[int, str] = {}

    # ---------------------------------------------------------------- collection

    def _collect(self) -> CpuSnapshot:
        now = time.monotonic()
        topology = self._get_topology()
        # /proc/stat is read exactly once per sample: the aggregate, the
        # per-core breakdown and the scalar counters must all describe the same
        # instant, and re-reading a 13-line file 12 times a minute is waste.
        stat_lines = sysfs.read_lines(sysfs.PROC / "stat")
        aggregate, per_core = self._parse_proc_stat(stat_lines)
        stat_extras = self._parse_stat_extras(stat_lines, now)
        temps = self._read_temperatures()
        frequencies = self._read_frequencies(topology.logical_cores)

        cores = tuple(
            CoreSample(
                index=index,
                usage=per_core.get(index, CpuTimes()).busy,
                frequency_mhz=frequencies.get(index, Unavailable(
                    Reason.NOT_EXPOSED, "cpufreq not available for this core"
                )),
                package=self._package_of(topology, index),
                core_id=self._core_of(topology, index),
                temperature=temps["per_core"].get(index) or Unavailable(
                    Reason.NOT_EXPOSED, "no per-core thermal zone"
                ),
                online=index in per_core,
            )
            for index in range(topology.logical_cores)
        )

        min_freq, max_freq = self._read_frequency_limits()
        return CpuSnapshot(
            timestamp=now,
            usage=aggregate.busy,
            times=aggregate,
            cores=cores,
            frequency_mhz=(
                sum(f for f in frequencies.values()) / len(frequencies)
                if frequencies else self._psutil_frequency()
            ),
            frequency_min_mhz=min_freq,
            frequency_max_mhz=max_freq,
            governor=self._read_governor(),
            available_governors=self._read_available_governors(),
            driver=sysfs.read_text(_CPU_DIR / "cpu0/cpufreq/scaling_driver")
            or Unavailable(Reason.NOT_EXPOSED, "no scaling driver reported"),
            turbo_enabled=self._read_turbo(),
            temperature=temps["package"],
            temperature_high=temps["high"],
            temperature_critical=temps["critical"],
            package_power_w=self._read_package_power(now),
            load_average=self._read_load_average(),
            context_switches_per_s=stat_extras.get("ctxt"),
            interrupts_per_s=stat_extras.get("intr"),
            forks_per_s=stat_extras.get("processes"),
            processes_running=_optional_int(
                stat_extras.get("procs_running_abs")
            ),
            processes_blocked=_optional_int(
                stat_extras.get("procs_blocked_abs")
            ),
            pressure_some=self._read_cpu_pressure(),
            topology=topology,
        )

    def _empty(self) -> CpuSnapshot:
        return CpuSnapshot(topology=self._topology or CpuTopology())

    # ------------------------------------------------------------- /proc/stat

    def _parse_proc_stat(
        self, lines: list[str]
    ) -> tuple[CpuTimes, dict[int, CpuTimes]]:
        """Convert cumulative jiffies from ``/proc/stat`` into percentages.

        The kernel reports monotonically increasing counters, so a percentage is
        only meaningful as a delta between two reads.  Both the aggregate and
        every core are derived from the same read for internal consistency.
        """
        aggregate = CpuTimes()
        per_core: dict[int, CpuTimes] = {}
        for line in lines:
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            label = parts[0]
            try:
                fields = [int(v) for v in parts[1:11]]
            except ValueError:
                continue
            # Older kernels publish fewer columns: `steal` arrived in 2.6.11,
            # `guest` in 2.6.24 and `guest_nice` in 2.6.33.  Padding to the full
            # width keeps the field names aligned with their values; without it
            # the share lookup below would raise KeyError and blank the whole
            # CPU page on such a kernel.
            fields.extend([0] * (len(_STAT_FIELDS) - len(fields)))
            values = tuple(fields[:len(_STAT_FIELDS)])
            times = self._delta_to_percent(label, values)
            if label == "cpu":
                aggregate = times
            else:
                try:
                    per_core[int(label[3:])] = times
                except ValueError:
                    continue
        return aggregate, per_core

    def _delta_to_percent(self, key: str, values: tuple[int, ...]) -> CpuTimes:
        """Convert a jiffy tuple into interval percentages."""
        previous = self._previous_stat.get(key)
        self._previous_stat[key] = values
        if previous is None or len(previous) != len(values):
            return CpuTimes(idle=100.0)
        deltas = [
            max(0, new - old)
            for new, old in zip(values, previous, strict=True)
        ]
        total = sum(deltas)
        if total <= 0:
            # No jiffies elapsed: report the previous shape rather than a divide
            # by zero.  Happens when polled faster than the kernel tick.
            return CpuTimes(idle=100.0)
        share = {
            name: 100.0 * d / total
            for name, d in zip(_STAT_FIELDS, deltas, strict=True)
        }
        return CpuTimes(
            user=share["user"],
            nice=share["nice"],
            system=share["system"],
            idle=share["idle"],
            iowait=share["iowait"],
            irq=share["irq"],
            softirq=share["softirq"],
            steal=share["steal"],
            guest=share["guest"] + share["guest_nice"],
        )

    def _parse_stat_extras(self, lines: list[str], now: float) -> dict[str, float]:
        """Rate-convert the scalar counters at the foot of ``/proc/stat``."""
        extras: dict[str, float] = {}
        for line in lines:
            key, _, raw = line.partition(" ")
            raw = raw.strip()
            if key in ("ctxt", "intr", "processes"):
                try:
                    counter = float(raw.split()[0])
                except (ValueError, IndexError):
                    continue
                extras[key] = self.rates.rate(f"stat.{key}", counter, now)
            elif key == "procs_running":
                extras["procs_running_abs"] = int(float(raw or 0))
            elif key == "procs_blocked":
                extras["procs_blocked_abs"] = int(float(raw or 0))
        return extras

    # ------------------------------------------------------------- frequencies

    def _read_frequencies(self, core_count: int) -> dict[int, float]:
        """Per-core current frequency in MHz, from cpufreq where available."""
        if not self.capabilities.cpu_frequency:
            return {}
        result: dict[int, float] = {}
        for index in range(core_count):
            base = _CPU_DIR / f"cpu{index}/cpufreq"
            # scaling_cur_freq is the governor's view; cpuinfo_cur_freq is the
            # hardware's but is often root-only.  Prefer whichever is readable.
            for node in ("scaling_cur_freq", "cpuinfo_cur_freq"):
                khz = sysfs.read_int(base / node)
                if khz is not None:
                    result[index] = khz / 1000.0
                    break
        return result

    def _read_frequency_limits(self) -> tuple[float | Unavailable, float | Unavailable]:
        """Policy minimum and maximum frequency in MHz."""
        base = _CPU_DIR / "cpu0/cpufreq"
        minimum = sysfs.read_int(base / "cpuinfo_min_freq", scale=1000)
        maximum = sysfs.read_int(base / "cpuinfo_max_freq", scale=1000)
        if minimum is None or maximum is None:
            try:
                freq = psutil.cpu_freq()
            except (OSError, AttributeError):
                freq = None
            if freq:
                minimum = minimum if minimum is not None else freq.min or None
                maximum = maximum if maximum is not None else freq.max or None
        absent = Unavailable(Reason.NOT_EXPOSED, "cpufreq limits not published")
        return (
            minimum if minimum is not None else absent,
            maximum if maximum is not None else absent,
        )

    def _psutil_frequency(self) -> float | Unavailable:
        """Fallback aggregate frequency when sysfs offers nothing."""
        try:
            freq = psutil.cpu_freq()
        except (OSError, AttributeError):
            freq = None
        if freq and freq.current:
            return float(freq.current)
        return self.capabilities.cpu_frequency.as_unavailable()

    def _read_governor(self) -> str | Unavailable:
        """Active scaling governor."""
        value = sysfs.read_text(_CPU_DIR / "cpu0/cpufreq/scaling_governor")
        if value:
            return value
        return self.capabilities.cpu_governor.as_unavailable()

    def _read_available_governors(self) -> tuple[str, ...]:
        """Governors this kernel offers."""
        raw = sysfs.read_text(_CPU_DIR / "cpu0/cpufreq/scaling_available_governors")
        return tuple(raw.split()) if raw else ()

    def _read_turbo(self) -> bool | Unavailable:
        """Turbo/boost state.

        Intel exposes an inverted ``no_turbo`` flag while AMD and the generic
        cpufreq path expose ``boost``; both are normalised to "turbo enabled".
        """
        no_turbo = sysfs.read_int(_CPU_DIR / "intel_pstate/no_turbo")
        if no_turbo is not None:
            return no_turbo == 0
        boost = sysfs.read_int(_CPU_DIR / "cpufreq/boost")
        if boost is not None:
            return boost == 1
        return self.capabilities.cpu_turbo.as_unavailable()

    # ------------------------------------------------------------- temperatures

    def _read_temperatures(self) -> _Thermal:
        """Package and per-core temperatures with their advertised limits.

        Sensor naming is not standardised, so several known-good chip names are
        tried in priority order before falling back to any thermal zone.
        """
        absent = Unavailable(Reason.NO_HARDWARE, "no CPU thermal sensor found")
        result: _Thermal = {
            "package": absent, "high": absent, "critical": absent, "per_core": {}
        }
        # Priority order reflects reliability: coretemp/k10temp are the real
        # on-die sensors; acpitz is a motherboard proxy and often lags.
        # The targeted chip reader is used rather than a full sensor scan: the
        # CPU collector runs every second and must not pay to read the NVMe,
        # battery and Wi-Fi sensors it has no use for.
        for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal",
                     "cpu-thermal", "acpitz", "thermal_zone0"):
            readings = self.hwmon.chip(chip)
            if not readings:
                continue
            per_core: dict[int, float] = {}
            package: float | None = None
            high: float | None = None
            critical: float | None = None
            for entry in readings:
                label = (entry.label or "").lower()
                if match := re.search(r"core\s*(\d+)", label):
                    per_core[int(match.group(1))] = entry.current
                is_package = (
                    not label
                    or any(m in label for m in ("package", "tctl", "tdie"))
                )
                if is_package and package is None:
                    package = entry.current
                    high = entry.high
                    critical = entry.critical
            if package is None and per_core:
                package = max(per_core.values())
            if package is None and readings:
                package = readings[0].current
                high, critical = readings[0].high, readings[0].critical
            if package is not None:
                result["package"] = package
                result["high"] = high if high else absent
                result["critical"] = critical if critical else absent
                result["per_core"] = self._expand_core_temps(per_core)
                break
        return result

    def _expand_core_temps(self, per_core: dict[int, float]) -> dict[int, float]:
        """Map physical-core temperatures onto every logical CPU.

        ``coretemp`` reports one sensor per *physical* core, but the UI shows one
        tile per *logical* CPU.  SMT siblings share a die area and therefore a
        temperature, so the reading is propagated to both.
        """
        topology = self._topology
        if not topology or not topology.packages:
            return per_core
        expanded: dict[int, float] = {}
        for cores in topology.packages.values():
            for core_id, logical_cpus in cores.items():
                if core_id in per_core:
                    for cpu in logical_cpus:
                        expanded[cpu] = per_core[core_id]
        return expanded or per_core

    # -------------------------------------------------------------------- power

    def _read_package_power(self, now: float) -> float | Unavailable:
        """Package power in watts, differentiated from the RAPL energy counter.

        ``energy_uj`` is a monotonic microjoule counter that wraps at
        ``max_energy_range_uj``.  Power is its derivative; a wrap shows up as a
        negative delta, which :class:`RateTracker` already reports as zero.
        """
        if not self.capabilities.rapl_power:
            return self.capabilities.rapl_power.as_unavailable()
        domains = sysfs.glob("class/powercap/intel-rapl:*/energy_uj")
        total = 0.0
        found = False
        for node in domains:
            # Only top-level package domains; sub-domains double-count.
            if node.parent.name.count(":") != 1:
                continue
            micro_joules = sysfs.read_int(node)
            if micro_joules is None:
                continue
            found = True
            total += self.rates.rate(f"rapl.{node.parent.name}", micro_joules, now)
        if not found:
            return Unavailable(Reason.PERMISSION, "RAPL counters unreadable")
        return total / 1_000_000.0  # microjoules/s -> watts

    # ------------------------------------------------------------------- system

    def _read_load_average(self) -> tuple[float, float, float]:
        """1/5/15-minute load averages."""
        try:
            one, five, fifteen = os.getloadavg()
            return (one, five, fifteen)
        except OSError:
            return (0.0, 0.0, 0.0)

    def _read_cpu_pressure(self) -> float | Unavailable:
        """10-second CPU pressure-stall average."""
        if not self.capabilities.psi:
            return self.capabilities.psi.as_unavailable()
        for line in sysfs.read_lines(sysfs.PROC / "pressure/cpu"):
            if line.startswith("some"):
                for token in line.split():
                    if token.startswith("avg10="):
                        try:
                            return float(token.split("=", 1)[1])
                        except ValueError:
                            break
        return Unavailable(Reason.NOT_EXPOSED, "CPU pressure not reported")

    # ----------------------------------------------------------------- topology

    def _get_topology(self) -> CpuTopology:
        """Build the static topology description once and cache it."""
        if self._topology is None:
            self._topology = self._build_topology()
        return self._topology

    def _build_topology(self) -> CpuTopology:
        """Assemble topology from ``/proc/cpuinfo`` and the sysfs topology tree."""
        cpuinfo = self._parse_cpuinfo()
        logical = psutil.cpu_count(logical=True) or 1
        physical = psutil.cpu_count(logical=False)

        # sysfs is authoritative for the SMT map; /proc/cpuinfo's core id field
        # is ambiguous on multi-socket systems.
        packages: dict[int, dict[int, list[int]]] = {}
        for index in range(logical):
            base = _CPU_DIR / f"cpu{index}/topology"
            package = sysfs.read_int(base / "physical_package_id")
            core = sysfs.read_int(base / "core_id")
            if package is None or core is None:
                continue
            cores = packages.setdefault(int(package), {})
            cores.setdefault(int(core), []).append(index)

        threads_per_core = None
        if packages:
            sibling_counts = {
                len(cpus) for cores in packages.values() for cpus in cores.values()
            }
            if len(sibling_counts) == 1:
                threads_per_core = sibling_counts.pop()
        elif physical:
            threads_per_core = max(1, logical // physical)

        return CpuTopology(
            model=cpuinfo.get("model name", platform.processor() or "Unknown"),
            vendor=cpuinfo.get("vendor_id", "Unknown"),
            architecture=platform.machine() or "Unknown",
            byte_order=(
                "Little Endian" if sys.byteorder == "little" else "Big Endian"
            ),
            sockets=len(packages) or 1,
            physical_cores=physical or sum(len(c) for c in packages.values()) or None,
            logical_cores=logical,
            threads_per_core=threads_per_core,
            packages=packages,
            cache=self._read_cache_topology(),
            flags=tuple(sorted(cpuinfo.get("flags", "").split())),
            stepping=cpuinfo.get("stepping"),
            family=cpuinfo.get("cpu family"),
            microcode=cpuinfo.get("microcode"),
            bogomips=float(cpuinfo["bogomips"]) if "bogomips" in cpuinfo else None,
            numa_nodes=len(sysfs.glob("devices/system/node/node*")) or None,
            virtualisation=self._detect_virtualisation(cpuinfo),
        )

    def _parse_cpuinfo(self) -> dict[str, str]:
        """First processor block of ``/proc/cpuinfo`` as a mapping."""
        info: dict[str, str] = {}
        for line in sysfs.read_lines(sysfs.PROC / "cpuinfo"):
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key = key.strip()
            if key in info:  # reached the second processor block
                break
            info[key] = value.strip()
        return info

    def _read_cache_topology(self) -> dict[str, str]:
        """Cache sizes per level from the sysfs cache index nodes."""
        cache: dict[str, str] = {}
        for index_dir in sysfs.glob("devices/system/cpu/cpu0/cache/index*"):
            level = sysfs.read_text(index_dir / "level")
            kind = sysfs.read_text(index_dir / "type") or ""
            size = sysfs.read_text(index_dir / "size")
            if not level or not size:
                continue
            suffix = {"Data": "d", "Instruction": "i"}.get(kind, "")
            cache[f"L{level}{suffix}"] = size
        return cache

    def _detect_virtualisation(self, cpuinfo: dict[str, str]) -> str | Unavailable:
        """Identify the hypervisor, or report bare metal."""
        # systemd-detect-virt deliberately exits 1 when it finds no
        # virtualisation, so the exit status cannot be used as a success test --
        # only the absence of output means the probe genuinely failed.
        result = run(["systemd-detect-virt"], timeout=2.0)
        value = result.stdout.strip()
        if value:
            return "None (bare metal)" if value == "none" else value
        if "hypervisor" in cpuinfo.get("flags", ""):
            return "Virtualised (hypervisor flag present)"
        return Unavailable(Reason.NO_TOOL, "systemd-detect-virt unavailable")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _package_of(topology: CpuTopology, cpu: int) -> int | Unavailable:
        """Which physical package a logical CPU belongs to."""
        for package, cores in topology.packages.items():
            for cpus in cores.values():
                if cpu in cpus:
                    return package
        return Unavailable(Reason.NOT_EXPOSED, "topology unavailable")

    @staticmethod
    def _core_of(topology: CpuTopology, cpu: int) -> int | Unavailable:
        """Which physical core a logical CPU belongs to."""
        for cores in topology.packages.values():
            for core_id, cpus in cores.items():
                if cpu in cpus:
                    return core_id
        return Unavailable(Reason.NOT_EXPOSED, "topology unavailable")
