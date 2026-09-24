"""NVIDIA GPU back-end, driven by ``nvidia-smi``.

Why the binary and not NVML bindings?
-------------------------------------
``pynvml`` is faster and richer, but it is an optional dependency that must match
the installed driver.  ``nvidia-smi`` ships *with* the driver, so if the driver
works the query works.  For a tool that must not crash on unusual systems, that
trade is worth the process-spawn cost.

Cost management
---------------
Measured on the development machine, an ``nvidia-smi`` query costs roughly 16 ms
of fixed NVML initialisation plus about 1 ms per requested field.  Asking for
everything on every sample cost 42 ms.  Two changes bring that down:

1. **Identity is queried once.**  Model, driver version, VBIOS, UUID, compute
   capability and PCIe link do not change while the machine is running, so they
   are fetched on the first sample and cached.  The per-sample query carries only
   telemetry: 42 ms becomes about 25 ms.
2. **The process list is staggered.**  Enumerating processes holding VRAM needs a
   second invocation (~22 ms).  That set changes far more slowly than
   utilisation, so it is refreshed every fourth sample.

Two parsing hazards are handled explicitly, both learned from real output:

* Unsupported fields come back as ``[N/A]``, not as blanks.  A laptop GPU with
  no controllable fan reports ``[N/A]`` for ``fan.speed``; treating that as 0
  would tell the user their fan had stopped.
* ``--query-compute-apps`` includes the full process command line, which
  routinely contains commas.  Splitting the CSV row on every comma corrupts both
  the name and the memory figure, so the row is split from the ends in.
"""

from __future__ import annotations

from app.collectors.gpu.base import (
    GpuBackend,
    parse_optional_float,
    parse_optional_text,
)
from app.models.base import Reason, Unavailable
from app.models.gpu import GpuDevice, GpuProcess, GpuSample, GpuVendor
from app.utils.shell import run

MIB = 1024 * 1024

#: Immutable adapter identity, queried once per session.
_STATIC_FIELDS = (
    "index", "name", "driver_version", "memory.total", "enforced.power.limit",
    "vbios_version", "pci.bus_id", "uuid", "compute_cap",
    "pcie.link.gen.max", "pcie.link.width.max",
)

#: Live telemetry, queried on every sample.
_DYNAMIC_FIELDS = (
    "index", "utilization.gpu", "utilization.memory", "memory.used",
    "memory.free", "temperature.gpu", "clocks.current.graphics",
    "clocks.current.memory", "clocks.max.graphics", "power.draw",
    "enforced.power.limit", "fan.speed", "pstate", "utilization.encoder",
    "utilization.decoder", "clocks_throttle_reasons.active",
)

#: How many telemetry samples pass between process-list refreshes.
_PROCESS_REFRESH_EVERY = 4

#: Bit meanings of ``clocks_throttle_reasons.active``, per the NVML headers.
_THROTTLE_BITS: tuple[tuple[int, str], ...] = (
    (0x0000000000000001, "GPU idle"),
    (0x0000000000000002, "Applications clocks setting"),
    (0x0000000000000004, "Software power cap"),
    (0x0000000000000008, "Hardware slowdown"),
    (0x0000000000000010, "Sync boost"),
    (0x0000000000000020, "Software thermal slowdown"),
    (0x0000000000000040, "Hardware thermal slowdown"),
    (0x0000000000000080, "Hardware power brake"),
    (0x0000000000000100, "Display clock setting"),
)

_NOT_REPORTED = Unavailable(Reason.NOT_EXPOSED, "not reported by this adapter")


class NvidiaBackend(GpuBackend):
    """Reads NVIDIA adapters via batched ``nvidia-smi`` queries."""

    vendor_name = "NVIDIA"

    def __init__(self, capabilities) -> None:
        super().__init__(capabilities)
        self._devices: dict[int, GpuDevice] | None = None
        self._sample_index = 0
        self._process_cache: dict[int, list[GpuProcess]] = {}

    def available(self) -> bool:
        """True when the driver is loaded and ``nvidia-smi`` is installed."""
        return bool(self.capabilities.nvidia)

    def sample(self) -> list[GpuSample]:
        """Read live telemetry for every NVIDIA adapter."""
        devices = self._identify()
        if not devices:
            return []
        rows = self._query(_DYNAMIC_FIELDS)
        processes = self._processes_cached()
        samples: list[GpuSample] = []
        for field in rows:
            index = int(parse_optional_float(field.get("index")) or 0)
            device = devices.get(index)
            if device is None:
                continue
            samples.append(self._build_sample(device, field, processes.get(index, [])))
        return samples

    # ----------------------------------------------------------------- identity

    def _identify(self) -> dict[int, GpuDevice]:
        """Query and cache immutable adapter identity."""
        if self._devices is not None:
            return self._devices
        devices: dict[int, GpuDevice] = {}
        for field in self._query(_STATIC_FIELDS):
            index = int(parse_optional_float(field.get("index")) or 0)
            total_mib = parse_optional_float(field.get("memory.total"))
            width = parse_optional_text(field.get("pcie.link.width.max"))
            devices[index] = GpuDevice(
                index=index,
                name=parse_optional_text(field.get("name")) or "NVIDIA GPU",
                vendor=GpuVendor.NVIDIA,
                driver_version=self._text(field.get("driver_version")),
                driver_name="nvidia (proprietary)",
                vram_total_bytes=int(total_mib * MIB) if total_mib else _NOT_REPORTED,
                pci_bus_id=self._text(field.get("pci.bus_id")),
                uuid=self._text(field.get("uuid")),
                vbios_version=self._text(field.get("vbios_version")),
                compute_capability=self._text(field.get("compute_cap")),
                max_power_w=self._number(field.get("enforced.power.limit")),
                pcie_gen=self._text(field.get("pcie.link.gen.max")),
                pcie_width=f"x{width}" if width else _NOT_REPORTED,
            )
        if devices:
            self._devices = devices
        return devices

    # ------------------------------------------------------------------ querying

    def _query(self, fields: tuple[str, ...]) -> list[dict[str, str]]:
        """Run one ``nvidia-smi`` query and return a mapping per adapter row."""
        result = run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            timeout=6.0,
        )
        if not result.ok:
            return []
        rows: list[dict[str, str]] = []
        for line in result.lines:
            cells = [cell.strip() for cell in line.split(",")]
            if len(cells) != len(fields):
                self._log.debug("Unexpected nvidia-smi row width: %r", line)
                continue
            rows.append(dict(zip(fields, cells, strict=True)))
        return rows

    def _build_sample(
        self, device: GpuDevice, field: dict[str, str], processes: list[GpuProcess]
    ) -> GpuSample:
        """Assemble a sample from one telemetry row."""
        used_mib = parse_optional_float(field.get("memory.used"))
        free_mib = parse_optional_float(field.get("memory.free"))
        return GpuSample(
            device=device,
            utilisation=self._number(field.get("utilization.gpu")),
            memory_utilisation=self._number(field.get("utilization.memory")),
            vram_used_bytes=(
                int(used_mib * MIB) if used_mib is not None else _NOT_REPORTED
            ),
            vram_free_bytes=(
                int(free_mib * MIB) if free_mib is not None else _NOT_REPORTED
            ),
            encoder_utilisation=self._number(field.get("utilization.encoder")),
            decoder_utilisation=self._number(field.get("utilization.decoder")),
            core_clock_mhz=self._number(field.get("clocks.current.graphics")),
            memory_clock_mhz=self._number(field.get("clocks.current.memory")),
            max_core_clock_mhz=self._number(field.get("clocks.max.graphics")),
            temperature=self._number(field.get("temperature.gpu")),
            # Laptop adapters frequently have no independently controllable fan;
            # the placeholder becomes an explanation rather than a zero.
            fan_percent=self._number(
                field.get("fan.speed"),
                Unavailable(Reason.NOT_EXPOSED, "no adapter-controlled fan"),
            ),
            power_w=self._number(field.get("power.draw")),
            power_limit_w=self._number(field.get("enforced.power.limit")),
            voltage_v=Unavailable(
                Reason.NOT_EXPOSED, "nvidia-smi does not expose core voltage"
            ),
            performance_state=self._text(field.get("pstate")),
            throttle_reasons=self._decode_throttle(
                field.get("clocks_throttle_reasons.active", "")
            ),
            processes=tuple(processes),
        )

    # ----------------------------------------------------------------- processes

    def _processes_cached(self) -> dict[int, list[GpuProcess]]:
        """Return the process list, refreshing it only every few samples."""
        if self._sample_index % _PROCESS_REFRESH_EVERY == 0:
            self._process_cache = self._query_processes()
        self._sample_index += 1
        return self._process_cache

    def _query_processes(self) -> dict[int, list[GpuProcess]]:
        """Enumerate processes holding GPU memory, keyed by adapter index.

        ``nvidia-smi`` does not report the owning adapter in this query mode, so
        on a multi-GPU machine every process is attributed to adapter 0.  That is
        a limitation of the CSV interface rather than something to work around,
        and the single-adapter case it serves correctly is by far the common one.
        """
        result = run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=6.0,
        )
        if not result.ok:
            return {}
        processes = [
            parsed
            for parsed in (self._parse_process_row(ln) for ln in result.lines)
            if parsed is not None
        ]
        return {0: processes} if processes else {}

    @staticmethod
    def _parse_process_row(line: str) -> GpuProcess | None:
        """Parse a compute-app row whose middle field may contain commas.

        The row is ``pid, process_name, used_memory``.  Because a Chromium or
        Electron command line contains many commas, the row is split from the
        outside in: the first comma ends the PID, the last begins the memory
        figure, and everything between is the name -- commas included.
        """
        head, sep, rest = line.partition(",")
        if not sep:
            return None
        name_part, sep, tail = rest.rpartition(",")
        if not sep:
            name_part, tail = rest, ""
        try:
            pid = int(head.strip())
        except ValueError:
            return None
        used_mib = parse_optional_float(tail)
        raw_name = name_part.strip()
        # Command lines can be kilobytes long; keep the executable's basename.
        display = raw_name.split()[0] if raw_name else "unknown"
        return GpuProcess(
            pid=pid,
            name=display.rsplit("/", 1)[-1] or display,
            used_vram_bytes=int(used_mib * MIB) if used_mib is not None else None,
            kind="compute",
        )

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _decode_throttle(raw: str) -> tuple[str, ...]:
        """Decode the active-throttle-reason bitmask into readable labels."""
        token = raw.strip()
        if not token or token.startswith("["):
            return ()
        try:
            mask = int(token, 0)
        except ValueError:
            return ()
        if mask == 0:
            return ()
        reasons = [label for bit, label in _THROTTLE_BITS if mask & bit]
        # "GPU idle" alone is not a throttle worth alarming the user about.
        return () if reasons == ["GPU idle"] else tuple(reasons)

    @staticmethod
    def _number(raw: str | None, fallback: Unavailable = _NOT_REPORTED):
        """Parse a numeric field, mapping vendor placeholders to ``fallback``."""
        value = parse_optional_float(raw)
        return fallback if value is None else value

    @staticmethod
    def _text(raw: str | None, fallback: Unavailable = _NOT_REPORTED):
        """Parse a text field, mapping vendor placeholders to ``fallback``."""
        value = parse_optional_text(raw)
        return fallback if value is None else value
