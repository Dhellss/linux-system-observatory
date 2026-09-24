"""Thermal, fan, voltage and power sensor collector.

Sensor data on Linux is a patchwork.  ``psutil`` gives a tidy view of
temperatures and fans but nothing else; hwmon exposes voltages, currents and
power, but with opaque chip names and raw integer units.  This collector uses
both: psutil (through the shared cache) for temperatures and fans, and a direct
hwmon walk for the electrical sensors it does not cover.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.collectors.base import Collector
from app.models.sensors import Sensor, SensorChip, SensorKind, SensorSnapshot
from app.utils import sysfs

#: Friendly names for chips whose kernel identifiers are cryptic.
_CHIP_LABELS = {
    "coretemp": "Intel CPU package",
    "k10temp": "AMD CPU die",
    "zenpower": "AMD CPU (zenpower)",
    "acpitz": "ACPI thermal zone",
    "nvme": "NVMe controller",
    "amdgpu": "AMD graphics",
    "nouveau": "NVIDIA graphics (nouveau)",
    "iwlwifi": "Intel Wi-Fi radio",
    "BAT0": "Battery",
    "thinkpad": "ThinkPad embedded controller",
    "dell_smm": "Dell embedded controller",
    "asus": "ASUS embedded controller",
    "nct6775": "Nuvoton Super-I/O",
    "it87": "ITE Super-I/O",
}

#: hwmon file prefixes mapped to their sensor kind, unit and scale divisor.
_HWMON_KINDS: tuple[tuple[str, SensorKind, str, float], ...] = (
    ("in", SensorKind.VOLTAGE, "V", 1000.0),
    ("curr", SensorKind.CURRENT, "A", 1000.0),
    ("power", SensorKind.POWER, "W", 1_000_000.0),
    ("energy", SensorKind.ENERGY, "J", 1_000_000.0),
    ("humidity", SensorKind.HUMIDITY, "%", 1000.0),
)


class SensorCollector(Collector[SensorSnapshot]):
    """Samples every hardware monitoring sensor the system exposes."""

    domain = "sensors"
    title = "Sensors"

    def _collect(self) -> SensorSnapshot:
        chips: dict[str, list[Sensor]] = {}

        for chip, readings in self.hwmon.temperatures().items():
            bucket = chips.setdefault(chip, [])
            for index, reading in enumerate(readings):
                bucket.append(
                    Sensor(
                        key=f"{chip}.temp.{index}",
                        label=reading.label or f"Temperature {index + 1}",
                        kind=SensorKind.TEMPERATURE,
                        value=reading.current,
                        unit="°C",
                        high=reading.high,
                        critical=reading.critical,
                        chip=chip,
                    )
                )

        for chip, readings in self.hwmon.fans().items():
            bucket = chips.setdefault(chip, [])
            for index, reading in enumerate(readings):
                bucket.append(
                    Sensor(
                        key=f"{chip}.fan.{index}",
                        label=reading.label or f"Fan {index + 1}",
                        kind=SensorKind.FAN,
                        value=reading.current,
                        unit="RPM",
                        chip=chip,
                    )
                )

        # Electrical sensors, which psutil does not expose at all.
        for chip, sensors in self._read_electrical().items():
            chips.setdefault(chip, []).extend(sensors)

        assembled = tuple(
            SensorChip(
                name=chip,
                label=self._label_for(chip),
                sensors=tuple(sensors),
            )
            for chip, sensors in sorted(chips.items())
            if sensors
        )
        return SensorSnapshot(
            timestamp=time.monotonic(),
            chips=assembled,
            note="" if assembled else str(self.capabilities.hwmon.as_unavailable()),
        )

    def _empty(self) -> SensorSnapshot:
        return SensorSnapshot(note="Sensor collection failed")

    # ---------------------------------------------------------------- electrical

    def _read_electrical(self) -> dict[str, list[Sensor]]:
        """Walk hwmon for voltage, current, power and energy sensors."""
        result: dict[str, list[Sensor]] = {}
        for hwmon_dir in sysfs.glob("class/hwmon/hwmon*"):
            chip = sysfs.read_text(hwmon_dir / "name") or hwmon_dir.name
            sensors: list[Sensor] = []
            for prefix, kind, unit, scale in _HWMON_KINDS:
                sensors.extend(
                    self._read_group(hwmon_dir, chip, prefix, kind, unit, scale)
                )
            if sensors:
                result.setdefault(chip, []).extend(sensors)
        return result

    def _read_group(
        self,
        hwmon_dir: Path,
        chip: str,
        prefix: str,
        kind: SensorKind,
        unit: str,
        scale: float,
    ) -> list[Sensor]:
        """Read every numbered sensor of one kind from a hwmon chip."""
        sensors: list[Sensor] = []
        for node in sorted(hwmon_dir.glob(f"{prefix}[0-9]*_input")):
            stem = node.name.rsplit("_", 1)[0]
            value = sysfs.read_int(node, scale=scale)
            if value is None:
                continue
            label = sysfs.read_text(hwmon_dir / f"{stem}_label") or stem
            sensors.append(
                Sensor(
                    key=f"{chip}.{stem}",
                    label=label,
                    kind=kind,
                    value=value,
                    unit=unit,
                    minimum=sysfs.read_int(hwmon_dir / f"{stem}_min", scale=scale),
                    maximum=sysfs.read_int(hwmon_dir / f"{stem}_max", scale=scale),
                    critical=sysfs.read_int(hwmon_dir / f"{stem}_crit", scale=scale),
                    chip=chip,
                )
            )
        return sensors

    @staticmethod
    def _label_for(chip: str) -> str:
        """Friendly display name for a chip identifier."""
        for marker, label in _CHIP_LABELS.items():
            if chip.startswith(marker):
                return label
        return chip.replace("_", " ").title()
