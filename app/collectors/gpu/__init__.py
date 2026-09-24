"""Multi-vendor GPU collector.

The collector owns a list of back-ends and simply concatenates whatever each one
reports.  A hybrid laptop with Intel integrated and NVIDIA discrete graphics
yields two adapters from two back-ends with no special-casing anywhere, and a
headless server with no display adapter at all yields an empty snapshot carrying
an explanatory note rather than an error.
"""

from __future__ import annotations

import time

from app.collectors.base import Collector
from app.collectors.gpu.amd import AmdBackend
from app.collectors.gpu.base import GpuBackend
from app.collectors.gpu.intel import IntelBackend
from app.collectors.gpu.nvidia import NvidiaBackend
from app.collectors.gpu.stack import GraphicsStackProbe
from app.models.gpu import GpuSnapshot

__all__ = ["AmdBackend", "GpuBackend", "GpuCollector", "IntelBackend", "NvidiaBackend"]


class GpuCollector(Collector[GpuSnapshot]):
    """Aggregates every available GPU vendor back-end into one snapshot."""

    domain = "gpu"
    title = "Graphics"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        # Discrete adapters are listed before integrated ones so that the
        # dashboard's "primary GPU" summary picks the one the user cares about.
        self._backends: list[GpuBackend] = [
            NvidiaBackend(capabilities),
            AmdBackend(capabilities),
            IntelBackend(capabilities, self.hwmon),
        ]
        self._stack = GraphicsStackProbe(capabilities)
        self._active: list[GpuBackend] | None = None

    def _collect(self) -> GpuSnapshot:
        samples = []
        for backend in self._active_backends():
            samples.extend(backend.safe_sample())
        # Re-index so the UI sees a contiguous 0..n-1 sequence even when two
        # back-ends each numbered their own adapters from zero.
        renumbered = tuple(
            sample.__class__(**{**_as_dict(sample), "device": _reindex(sample, index)})
            for index, sample in enumerate(samples)
        )
        return GpuSnapshot(
            timestamp=time.monotonic(),
            gpus=renumbered,
            stack=self._stack.probe(),
            note="" if renumbered else self._no_gpu_note(),
        )

    def _empty(self) -> GpuSnapshot:
        return GpuSnapshot(note="GPU collection failed; see the application log")

    def _active_backends(self) -> list[GpuBackend]:
        """Back-ends whose hardware is present, determined once.

        Availability is a hardware fact, so probing it on every poll would waste
        a sysfs walk per vendor per sample.  A GPU being hot-plugged mid-session
        is not a case worth paying that cost for.
        """
        if self._active is None:
            self._active = [b for b in self._backends if b.available()]
            names = ", ".join(b.vendor_name for b in self._active) or "none"
            self._log.info("Active GPU back-ends: %s", names)
        return self._active

    def _no_gpu_note(self) -> str:
        """Explain why no adapter could be read."""
        reasons = []
        for backend, capability in (
            ("NVIDIA", self.capabilities.nvidia),
            ("AMD", self.capabilities.amdgpu),
            ("Intel", self.capabilities.intel_gpu),
        ):
            if not capability:
                reasons.append(f"{backend}: {capability.detail}")
        return "No supported graphics adapter detected. " + "; ".join(reasons)


def _as_dict(sample) -> dict:
    """Shallow field mapping of a frozen slots dataclass."""
    from dataclasses import fields

    return {f.name: getattr(sample, f.name) for f in fields(sample)}


def _reindex(sample, index: int):
    """Return this sample's device with its index replaced."""
    from dataclasses import replace

    return replace(sample.device, index=index)
