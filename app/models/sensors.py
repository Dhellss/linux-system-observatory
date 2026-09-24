"""Thermal, fan, voltage and power sensor models."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.models.base import Maybe, Snapshot, is_number


class SensorKind(enum.Enum):
    """What a sensor measures, used to pick units and iconography."""

    TEMPERATURE = "Temperature"
    FAN = "Fan"
    VOLTAGE = "Voltage"
    CURRENT = "Current"
    POWER = "Power"
    ENERGY = "Energy"
    HUMIDITY = "Humidity"
    OTHER = "Other"


class ThermalStatus(enum.Enum):
    """Banded assessment of a temperature reading against its own limits."""

    NORMAL = "Normal"
    WARM = "Warm"
    HOT = "Hot"
    CRITICAL = "Critical"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class Sensor:
    """One sensor reading with whatever limits the hardware advertises."""

    key: str
    label: str
    kind: SensorKind
    value: Maybe[float]
    unit: str = ""
    high: Maybe[float] = None
    critical: Maybe[float] = None
    minimum: Maybe[float] = None
    maximum: Maybe[float] = None
    #: hwmon chip or psutil group this sensor belongs to.
    chip: str = ""

    @property
    def status(self) -> ThermalStatus:
        """Assess this reading against the limits the hardware reports.

        Thresholds come from the hardware itself where available; a fixed
        80 °C rule would be wrong for both a 105 °C-rated CPU and a 60 °C-rated
        NVMe controller.  Only when no limits are exposed do we fall back to
        conservative defaults.
        """
        if self.kind is not SensorKind.TEMPERATURE or not is_number(self.value):
            return ThermalStatus.UNKNOWN
        critical = self.critical if is_number(self.critical) else None
        high = self.high if is_number(self.high) else None
        if critical and self.value >= critical:
            return ThermalStatus.CRITICAL
        if high and self.value >= high:
            return ThermalStatus.HOT
        if critical and self.value >= critical * 0.85:
            return ThermalStatus.HOT
        if high and self.value >= high * 0.85:
            return ThermalStatus.WARM
        if not high and not critical:
            if self.value >= 90:
                return ThermalStatus.CRITICAL
            if self.value >= 80:
                return ThermalStatus.HOT
            if self.value >= 70:
                return ThermalStatus.WARM
        return ThermalStatus.NORMAL

    @property
    def headroom(self) -> Maybe[float]:
        """Degrees remaining before the critical limit."""
        if is_number(self.value) and is_number(self.critical):
            return self.critical - self.value
        return None


@dataclass(frozen=True, slots=True)
class SensorChip:
    """A hwmon chip and the sensors it exposes."""

    name: str
    label: str
    adapter: Maybe[str] = None
    sensors: tuple[Sensor, ...] = ()

    def of_kind(self, kind: SensorKind) -> list[Sensor]:
        """Sensors of a particular kind on this chip."""
        return [s for s in self.sensors if s.kind is kind]


@dataclass(frozen=True, slots=True)
class SensorSnapshot(Snapshot):
    """One complete sensor sample."""

    chips: tuple[SensorChip, ...] = ()
    note: str = ""

    @property
    def all_sensors(self) -> list[Sensor]:
        """Every sensor across every chip."""
        return [s for chip in self.chips for s in chip.sensors]

    def temperatures(self) -> list[Sensor]:
        """Temperature sensors, hottest first."""
        temps = [s for s in self.all_sensors if s.kind is SensorKind.TEMPERATURE]
        return sorted(
            temps,
            key=lambda s: s.value if is_number(s.value) else -1,
            reverse=True,
        )

    def fans(self) -> list[Sensor]:
        """Fan sensors."""
        return [s for s in self.all_sensors if s.kind is SensorKind.FAN]

    @property
    def hottest(self) -> Sensor | None:
        """The highest temperature reading on the system."""
        temps = self.temperatures()
        return temps[0] if temps else None

    @property
    def worst_status(self) -> ThermalStatus:
        """The most severe thermal status anywhere on the system."""
        order = [
            ThermalStatus.CRITICAL,
            ThermalStatus.HOT,
            ThermalStatus.WARM,
            ThermalStatus.NORMAL,
        ]
        statuses = {s.status for s in self.temperatures()}
        for candidate in order:
            if candidate in statuses:
                return candidate
        return ThermalStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class BatterySnapshot(Snapshot):
    """One complete battery sample.

    Health is the interesting figure and the one most tools omit: the ratio of
    present full-charge capacity to the factory design capacity.  It is the
    difference between "my battery is full" and "my battery is full, and full is
    now 62% of what it once was".
    """

    present: bool = False
    percent: Maybe[float] = None
    status: Maybe[str] = None          # "Charging" | "Discharging" | "Full" | ...
    plugged_in: Maybe[bool] = None
    seconds_remaining: Maybe[float] = None
    energy_now_wh: Maybe[float] = None
    energy_full_wh: Maybe[float] = None
    energy_design_wh: Maybe[float] = None
    power_now_w: Maybe[float] = None
    voltage_now_v: Maybe[float] = None
    voltage_design_v: Maybe[float] = None
    current_now_a: Maybe[float] = None
    cycle_count: Maybe[int] = None
    capacity_level: Maybe[str] = None
    technology: Maybe[str] = None
    manufacturer: Maybe[str] = None
    model: Maybe[str] = None
    serial: Maybe[str] = None
    temperature: Maybe[float] = None
    #: Active power profile from ``power-profiles-daemon`` or the platform driver.
    power_profile: Maybe[str] = None
    note: str = ""

    @property
    def health_percent(self) -> Maybe[float]:
        """Full-charge capacity as a percentage of design capacity."""
        full, design = self.energy_full_wh, self.energy_design_wh
        if is_number(full) and is_number(design) and design > 0:
            return min(100.0, 100.0 * full / design)
        return None

    @property
    def wear_percent(self) -> Maybe[float]:
        """Capacity lost to ageing, as a percentage."""
        health = self.health_percent
        return 100.0 - health if is_number(health) else None

    @property
    def charging(self) -> bool:
        """True while the battery is taking charge."""
        return isinstance(self.status, str) and self.status.lower() == "charging"

    @property
    def rate_label(self) -> str:
        """Direction label appropriate to the current power flow."""
        if self.charging:
            return "Charge rate"
        if isinstance(self.status, str) and self.status.lower() == "full":
            return "Power draw"
        return "Discharge rate"
