"""Battery collector.

``psutil.sensors_battery()`` supplies only percentage, time remaining and the
AC-connected flag.  Everything that makes a battery page worth having -- design
capacity, cycle count, wear, instantaneous power draw -- comes from
``/sys/class/power_supply/BAT*``, which is read directly here.

The kernel is inconsistent about units across drivers: some batteries report
energy (µWh) and power (µW), others report charge (µAh) and current (µA).  Both
are handled, converting charge-based readings to energy using the present
voltage, so the UI has one consistent set of units to display.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.sensors import BatterySnapshot
from app.utils import sysfs
from app.utils.shell import run

_SUPPLY = sysfs.SYS / "class/power_supply"

MICRO = 1_000_000.0

#: How long a power-profile reading stays valid.  ``powerprofilesctl`` is a
#: Python script that round-trips over D-Bus and costs about 90 ms -- more than
#: the rest of the battery sample combined.  The profile only changes when the
#: user changes it, so re-reading it on every sample is pure waste.
_PROFILE_TTL = 60.0


class BatteryCollector(Collector[BatterySnapshot]):
    """Samples battery charge, health and power flow."""

    domain = "battery"
    title = "Battery"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._profile: object = None
        self._profile_read_at = 0.0

    def _collect(self) -> BatterySnapshot:
        node = self._find_battery()
        if node is None:
            capability = self.capabilities.battery
            return BatterySnapshot(
                timestamp=time.monotonic(),
                present=False,
                note=str(capability.as_unavailable()),
            )

        voltage = sysfs.read_int(node / "voltage_now", scale=MICRO)
        energy_now, energy_full, energy_design = self._read_capacity(node, voltage)
        power, current = self._read_power_flow(node, voltage)
        status = sysfs.read_text(node / "status")

        return BatterySnapshot(
            timestamp=time.monotonic(),
            present=True,
            percent=self._read_percent(node, energy_now, energy_full),
            status=status or Unavailable(Reason.NOT_EXPOSED, "status not reported"),
            plugged_in=self._read_ac_online(),
            seconds_remaining=self._estimate_remaining(
                status, energy_now, energy_full, power
            ),
            energy_now_wh=energy_now if energy_now is not None else self._no_energy(),
            energy_full_wh=(
                energy_full if energy_full is not None else self._no_energy()
            ),
            energy_design_wh=(
                energy_design if energy_design is not None
                else Unavailable(Reason.NOT_EXPOSED, "design capacity not published")
            ),
            power_now_w=power if power is not None else Unavailable(
                Reason.NOT_EXPOSED, "instantaneous power not reported"
            ),
            voltage_now_v=voltage if voltage is not None else Unavailable(
                Reason.NOT_EXPOSED, "voltage not reported"
            ),
            voltage_design_v=sysfs.read_int(node / "voltage_min_design", scale=MICRO),
            current_now_a=current if current is not None else Unavailable(
                Reason.NOT_EXPOSED, "current not reported"
            ),
            cycle_count=self._read_cycles(node),
            capacity_level=sysfs.read_text(node / "capacity_level"),
            technology=sysfs.read_text(node / "technology"),
            manufacturer=sysfs.read_text(node / "manufacturer"),
            model=sysfs.read_text(node / "model_name"),
            serial=sysfs.read_text(node / "serial_number"),
            temperature=self._read_temperature(node),
            power_profile=self._read_power_profile(),
        )

    def _empty(self) -> BatterySnapshot:
        return BatterySnapshot(present=False, note="Battery collection failed")

    # ---------------------------------------------------------------- discovery

    @staticmethod
    def _find_battery() -> Path | None:
        """Locate the first power supply of type ``Battery``."""
        for supply in sysfs.glob("class/power_supply/*"):
            if sysfs.read_text(supply / "type") == "Battery":
                return supply
        return None

    @staticmethod
    def _read_ac_online():
        """Whether an AC adapter is connected and supplying power."""
        for supply in sysfs.glob("class/power_supply/*"):
            kind = sysfs.read_text(supply / "type")
            if kind in ("Mains", "USB", "USB_PD", "USB_PD_DRP"):
                online = sysfs.read_int(supply / "online")
                if online is not None and online == 1:
                    return True
        # Distinguish "no adapter connected" from "cannot tell".
        if sysfs.glob("class/power_supply/A*"):
            return False
        return Unavailable(Reason.NOT_EXPOSED, "no AC adapter node found")

    # ----------------------------------------------------------------- capacity

    def _read_capacity(
        self, node: Path, voltage: float | None
    ) -> tuple[float | None, float | None, float | None]:
        """Present, full and design capacity in watt-hours.

        Energy-reporting batteries are read directly.  Charge-reporting ones are
        converted using the present voltage, which is how ``upower`` and the
        kernel's own ``power_supply`` helpers do it.  Without the conversion the
        page would show amp-hours labelled as watt-hours -- a silent unit error.
        """
        energy_now = sysfs.read_int(node / "energy_now", scale=MICRO)
        energy_full = sysfs.read_int(node / "energy_full", scale=MICRO)
        energy_design = sysfs.read_int(node / "energy_full_design", scale=MICRO)
        if energy_now is not None or energy_full is not None:
            return energy_now, energy_full, energy_design

        charge_now = sysfs.read_int(node / "charge_now", scale=MICRO)
        charge_full = sysfs.read_int(node / "charge_full", scale=MICRO)
        charge_design = sysfs.read_int(node / "charge_full_design", scale=MICRO)
        if voltage:
            return (
                charge_now * voltage if charge_now is not None else None,
                charge_full * voltage if charge_full is not None else None,
                charge_design * voltage if charge_design is not None else None,
            )
        return None, None, None

    def _read_percent(
        self, node: Path, energy_now: float | None, energy_full: float | None
    ):
        """Charge percentage.

        The kernel's own ``capacity`` file is preferred because some firmware
        applies smoothing that users see elsewhere in their desktop; the energy
        ratio is the fallback.
        """
        capacity = sysfs.read_int(node / "capacity")
        if capacity is not None:
            return float(capacity)
        if energy_now is not None and energy_full:
            return min(100.0, 100.0 * energy_now / energy_full)
        battery = self.hwmon.battery()
        if battery is not None:
            return float(getattr(battery, "percent", 0.0))
        return Unavailable(Reason.NOT_EXPOSED, "charge level not reported")

    def _read_power_flow(
        self, node: Path, voltage: float | None
    ) -> tuple[float | None, float | None]:
        """Instantaneous power in watts and current in amps."""
        power = sysfs.read_int(node / "power_now", scale=MICRO)
        current = sysfs.read_int(node / "current_now", scale=MICRO)
        if power is None and current is not None and voltage:
            power = current * voltage
        if current is None and power is not None and voltage:
            current = power / voltage
        return power, current

    @staticmethod
    def _read_cycles(node: Path):
        """Charge cycle count.

        Many batteries report 0 rather than omitting the file when the firmware
        does not track cycles; 0 is treated as "not reported" because a battery
        that has genuinely never been cycled is not a case worth optimising for.
        """
        cycles = sysfs.read_int(node / "cycle_count")
        if cycles is None:
            return Unavailable(Reason.NOT_EXPOSED, "cycle count not published")
        if cycles <= 0:
            return Unavailable(
                Reason.NOT_EXPOSED, "firmware does not track cycle count"
            )
        return int(cycles)

    @staticmethod
    def _read_temperature(node: Path):
        """Battery temperature, reported by the kernel in tenths of a degree."""
        value = sysfs.read_int(node / "temp", scale=10.0)
        if value is None:
            return Unavailable(Reason.NO_HARDWARE, "no battery thermistor")
        return value

    def _estimate_remaining(
        self,
        status: str | None,
        energy_now: float | None,
        energy_full: float | None,
        power: float | None,
    ):
        """Estimate seconds until empty (discharging) or full (charging).

        Computed from the present power flow rather than taken from the firmware,
        because firmware estimates are frequently absent and, when present, are
        often wildly optimistic right after a state change.
        """
        if not status or power is None or power <= 0:
            return Unavailable(
                Reason.NOT_EXPOSED, "no power flow from which to estimate"
            )
        state = status.lower()
        if state == "discharging" and energy_now is not None:
            return (energy_now / power) * 3600.0
        if state == "charging" and energy_now is not None and energy_full is not None:
            deficit = max(0.0, energy_full - energy_now)
            return (deficit / power) * 3600.0
        if state == "full":
            return Unavailable(Reason.NOT_EXPOSED, "battery is full")
        return Unavailable(Reason.NOT_EXPOSED, "not charging or discharging")

    @staticmethod
    def _no_energy() -> Unavailable:
        """Explanation used when neither energy nor charge could be read."""
        return Unavailable(Reason.NOT_EXPOSED, "capacity not published by firmware")

    def _read_power_profile(self):
        """Active platform power profile, cached for :data:`_PROFILE_TTL`.

        The kernel's ``platform_profile`` node is preferred because it is a
        single cheap file read; only when the platform driver does not publish it
        is ``powerprofilesctl`` consulted, and then the answer is cached.
        """
        profile = sysfs.read_text(sysfs.SYS / "firmware/acpi/platform_profile")
        if profile:
            return profile

        now = time.monotonic()
        if self._profile is not None and now - self._profile_read_at < _PROFILE_TTL:
            return self._profile

        result = run(["powerprofilesctl", "get"], timeout=3.0)
        value = result.stdout.strip() if result.ok else ""
        self._profile = value or Unavailable(
            Reason.NOT_EXPOSED, "no platform profile or power-profiles-daemon"
        )
        self._profile_read_at = now
        return self._profile
