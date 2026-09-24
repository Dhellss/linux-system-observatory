"""GPU domain models, vendor-neutral by design.

A single :class:`GpuSnapshot` shape covers NVIDIA, AMD and Intel.  Vendor
back-ends fill in what their interface exposes and leave the rest as
``Unavailable``, so the GPU page is written once rather than three times.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot, is_number


class GpuVendor(enum.Enum):
    """Recognised GPU vendors."""

    NVIDIA = "NVIDIA"
    AMD = "AMD"
    INTEL = "Intel"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class GpuProcess:
    """A process holding GPU resources."""

    pid: int
    name: str
    used_vram_bytes: Maybe[int] = None
    kind: str = "graphics"   # "graphics" | "compute"


@dataclass(frozen=True, slots=True)
class GraphicsStack:
    """Host graphics environment -- probed once, shared by every GPU.

    This belongs to the session rather than to any single adapter, which is why
    it is a separate structure: on a hybrid laptop the OpenGL renderer string
    describes whichever GPU the compositor chose, not both.
    """

    session_type: Maybe[str] = None       # "wayland" | "x11"
    compositor: Maybe[str] = None
    opengl_renderer: Maybe[str] = None
    opengl_version: Maybe[str] = None
    opengl_vendor: Maybe[str] = None
    glsl_version: Maybe[str] = None
    vulkan_api_version: Maybe[str] = None
    vulkan_devices: tuple[str, ...] = ()
    display_server_version: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """Static identity of one graphics adapter."""

    index: int
    name: str
    vendor: GpuVendor
    driver_version: Maybe[str] = None
    driver_name: Maybe[str] = None
    vram_total_bytes: Maybe[int] = None
    pci_bus_id: Maybe[str] = None
    uuid: Maybe[str] = None
    vbios_version: Maybe[str] = None
    compute_capability: Maybe[str] = None
    max_power_w: Maybe[float] = None
    pcie_gen: Maybe[str] = None
    pcie_width: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class GpuSample:
    """Live telemetry for one adapter."""

    device: GpuDevice
    utilisation: Maybe[float] = None
    memory_utilisation: Maybe[float] = None
    vram_used_bytes: Maybe[int] = None
    vram_free_bytes: Maybe[int] = None
    encoder_utilisation: Maybe[float] = None
    decoder_utilisation: Maybe[float] = None
    core_clock_mhz: Maybe[float] = None
    memory_clock_mhz: Maybe[float] = None
    max_core_clock_mhz: Maybe[float] = None
    temperature: Maybe[float] = None
    hotspot_temperature: Maybe[float] = None
    memory_temperature: Maybe[float] = None
    fan_percent: Maybe[float] = None
    fan_rpm: Maybe[float] = None
    power_w: Maybe[float] = None
    power_limit_w: Maybe[float] = None
    voltage_v: Maybe[float] = None
    performance_state: Maybe[str] = None
    throttle_reasons: tuple[str, ...] = ()
    processes: tuple[GpuProcess, ...] = ()

    @property
    def vram_percent(self) -> Maybe[float]:
        """VRAM utilisation as a percentage, when both figures are known."""
        total = self.device.vram_total_bytes
        used = self.vram_used_bytes
        if isinstance(total, int) and isinstance(used, int) and total:
            return 100.0 * used / total
        return self.memory_utilisation


@dataclass(frozen=True, slots=True)
class GpuSnapshot(Snapshot):
    """All adapters plus the shared graphics stack."""

    gpus: tuple[GpuSample, ...] = ()
    stack: GraphicsStack = field(default_factory=GraphicsStack)
    #: Populated when no adapter could be read at all, explaining why.
    note: str = ""

    @property
    def primary(self) -> GpuSample | None:
        """The busiest adapter, used for dashboard summaries."""
        candidates = [g for g in self.gpus if is_number(g.utilisation)]
        if candidates:
            return max(candidates, key=lambda g: g.utilisation)  # type: ignore[arg-type,return-value]
        return self.gpus[0] if self.gpus else None
