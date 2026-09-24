"""SMART and NVMe health reader.

``smartctl --json`` is used rather than the human-readable output: the text format
differs between ATA and NVMe devices, between smartmontools versions, and with
the system locale.  The JSON schema is stable and documented.

Health data is expensive (each call wakes the device and can take a second on a
spinning disk) and changes on the scale of days, so it is sampled on a long
cadence and cached between reads by the storage collector.
"""

from __future__ import annotations

import json
import logging

from app.models.base import Reason, Unavailable
from app.models.storage import SmartAttribute, SmartHealth, SmartVerdict
from app.utils.shell import run

_log = logging.getLogger(__name__)

#: ATA attributes whose non-zero raw value indicates a real problem.
_CRITICAL_ATA_ATTRIBUTES = {
    5: "Reallocated sectors",
    196: "Reallocation events",
    197: "Current pending sectors",
    198: "Offline uncorrectable sectors",
}


class SmartReader:
    """Reads device health via ``smartctl``, degrading gracefully without it."""

    def __init__(self, capabilities) -> None:
        self._capabilities = capabilities

    def read(self, device_path: str) -> SmartHealth:
        """Read health for one device.

        Returns a :class:`SmartHealth` whose ``verdict`` is ``UNKNOWN`` and whose
        ``note`` explains the obstacle whenever data cannot be obtained -- a
        missing tool, insufficient privileges, or a USB bridge that does not pass
        SMART commands through.
        """
        capability = self._capabilities.smartctl
        if not capability:
            return SmartHealth(note=str(capability.as_unavailable()))

        # --json=c gives compact JSON; -a requests all available data.
        result = run(["smartctl", "--json=c", "-a", device_path], timeout=15.0)
        if not result.stdout:
            return SmartHealth(note="smartctl produced no output")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return SmartHealth(note="smartctl output was not valid JSON")

        # smartctl's exit status is a bitmask, not a simple success flag: bit 0
        # means the command failed, but bits 2+ merely report device warnings.
        # Treating any non-zero status as failure would discard usable data from
        # exactly the failing drives the user most needs to see.
        if result.returncode & 0b11:
            messages = payload.get("smartctl", {}).get("messages", [])
            detail = "; ".join(
                m.get("string", "") for m in messages if m.get("string")
            )
            if "permission" in detail.lower() or "root" in detail.lower():
                return SmartHealth(
                    note=str(Unavailable(Reason.PERMISSION, detail or "access denied"))
                )
            return SmartHealth(note=detail or "smartctl could not query this device")

        return self._parse(payload)

    # ------------------------------------------------------------------ parsing

    def _parse(self, payload: dict) -> SmartHealth:
        """Build a :class:`SmartHealth` from parsed smartctl JSON."""
        status = payload.get("smart_status", {})
        if "passed" in status:
            verdict = SmartVerdict.PASSED if status["passed"] else SmartVerdict.FAILING
        else:
            verdict = SmartVerdict.UNKNOWN

        temperature = payload.get("temperature", {}).get("current")
        power_on = payload.get("power_on_time", {}).get("hours")
        cycles = payload.get("power_cycle_count")

        attributes: list[SmartAttribute] = []
        health = {
            "wear": None, "spare": None, "written": None, "read": None,
            "reallocated": None, "pending": None, "crc": None,
            "unsafe": None, "media": None,
        }

        if nvme := payload.get("nvme_smart_health_information_log"):
            self._parse_nvme(nvme, health, attributes)
            if temperature is None:
                temperature = nvme.get("temperature")
        if ata := payload.get("ata_smart_attributes", {}).get("table"):
            self._parse_ata(ata, health, attributes)

        # A drive can pass its own self-assessment while still reporting
        # reallocated sectors or exhausted endurance.  Downgrade the verdict so
        # the user is warned before the drive itself admits to failing.
        if verdict is SmartVerdict.PASSED and any(a.concerning for a in attributes):
            verdict = SmartVerdict.WARNING

        return SmartHealth(
            verdict=verdict,
            temperature=float(temperature) if temperature is not None else None,
            power_on_hours=int(power_on) if power_on is not None else None,
            power_cycles=int(cycles) if cycles is not None else None,
            wear_percent=health["wear"],
            spare_percent=health["spare"],
            data_written_bytes=health["written"],
            data_read_bytes=health["read"],
            reallocated_sectors=health["reallocated"],
            pending_sectors=health["pending"],
            crc_errors=health["crc"],
            unsafe_shutdowns=health["unsafe"],
            media_errors=health["media"],
            attributes=tuple(attributes),
        )

    def _parse_nvme(
        self, log: dict, health: dict, attributes: list[SmartAttribute]
    ) -> None:
        """Extract the NVMe SMART/health information log page."""
        used = log.get("percentage_used")
        if used is not None:
            health["wear"] = float(used)
        spare = log.get("available_spare")
        if spare is not None:
            health["spare"] = float(spare)
        health["unsafe"] = log.get("unsafe_shutdowns")
        health["media"] = log.get("media_errors")

        # NVMe reports data units of 512,000 bytes (1000 * 512), not 512 -- a
        # factor-of-1000 error that would report terabytes as gigabytes.
        for key, target in (
            ("data_units_written", "written"),
            ("data_units_read", "read"),
        ):
            if (units := log.get(key)) is not None:
                health[target] = int(units) * 512_000

        rows = (
            ("Percentage used", used, "%", (used or 0) >= 90),
            ("Available spare", spare, "%", spare is not None and spare < 10),
            ("Media errors", log.get("media_errors"), "",
             bool(log.get("media_errors"))),
            ("Unsafe shutdowns", log.get("unsafe_shutdowns"), "", False),
            ("Error log entries", log.get("num_err_log_entries"), "", False),
            ("Critical warning", log.get("critical_warning"), "",
             bool(log.get("critical_warning"))),
            ("Controller busy time", log.get("controller_busy_time"), " min", False),
            ("Power cycles", log.get("power_cycles"), "", False),
        )
        for name, value, unit, concerning in rows:
            if value is None:
                continue
            attributes.append(
                SmartAttribute(
                    identifier="NVMe",
                    name=name,
                    value=f"{value}{unit}",
                    concerning=concerning,
                )
            )

    def _parse_ata(
        self, table: list[dict], health: dict, attributes: list[SmartAttribute]
    ) -> None:
        """Extract the classic ATA SMART attribute table."""
        for row in table:
            identifier = row.get("id")
            name = (row.get("name") or "").replace("_", " ").title()
            raw = row.get("raw", {}).get("string", "")
            value = row.get("value")
            worst = row.get("worst")
            threshold = row.get("thresh")

            raw_number = self._leading_int(raw)
            concerning = False
            if identifier in _CRITICAL_ATA_ATTRIBUTES and raw_number:
                concerning = True
            # A normalised value at or below the vendor threshold is the drive
            # telling us this attribute has failed.
            if (
                isinstance(value, int) and isinstance(threshold, int)
                and threshold > 0 and value <= threshold
            ):
                concerning = True

            if identifier == 5:
                health["reallocated"] = raw_number
            elif identifier == 197:
                health["pending"] = raw_number
            elif identifier == 199:
                health["crc"] = raw_number
            elif identifier in (177, 202, 231) and isinstance(value, int):
                # Vendor-specific wear levelling; the normalised value counts
                # down from 100 as endurance is consumed.
                health["wear"] = float(max(0, 100 - value))

            attributes.append(
                SmartAttribute(
                    identifier=str(identifier),
                    name=name,
                    value=str(value) if value is not None else "—",
                    raw=raw or None,
                    threshold=str(threshold) if threshold is not None else None,
                    worst=str(worst) if worst is not None else None,
                    concerning=concerning,
                )
            )

    @staticmethod
    def _leading_int(raw: str) -> int | None:
        """First integer in a SMART raw string, which often has trailing text."""
        digits = ""
        for char in raw.strip():
            if char.isdigit():
                digits += char
            else:
                break
        return int(digits) if digits else None
