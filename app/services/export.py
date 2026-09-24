"""Report and data export.

Four formats, each with a clear purpose:

``CSV``
    Metric history for a spreadsheet or a plotting script.
``JSON``
    A complete machine-readable snapshot, for scripting and bug reports.
``Markdown``
    A readable report to paste into an issue tracker or a wiki.
``HTML``
    A self-contained, printable document.  HTML is generated rather than PDF
    because producing real PDF needs a heavyweight dependency, whereas every
    Linux desktop can print an HTML file to PDF from a browser -- the same result
    without the dependency.

Everything is written locally.  No exporter contacts the network.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import platform
import time
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path

from app.core.units import bytes_, duration, percent, temperature
from app.models.base import Unavailable
from app.models.diagnostics import DiagnosticReport, Severity

_log = logging.getLogger(__name__)

APP_NAME = "Linux System Observatory"


def _plain(value: object) -> object:
    """Recursively convert a snapshot into JSON-serialisable primitives.

    ``Unavailable`` becomes a structured object rather than ``null``, so a
    consumer of the JSON can tell "no GPU fan on this hardware" apart from
    "field omitted".  That distinction is the whole point of the sentinel and it
    should survive export.
    """
    if isinstance(value, Unavailable):
        return {
            "available": False,
            "reason": value.reason.name.lower(),
            "explanation": str(value),
        }
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        # fields() + getattr, deliberately not asdict(): asdict recurses into
        # nested dataclasses itself, which would convert an Unavailable into a
        # plain {reason, detail} mapping before the branch above ever sees it —
        # silently discarding the structured absence marker that makes the
        # export useful.
        return {
            field.name: _plain(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class ExportService:
    """Writes reports and metric history to disk."""

    def __init__(self, history=None, diagnostics=None) -> None:
        self._history = history
        self._diagnostics = diagnostics

    # ---------------------------------------------------------------------- CSV

    def metrics_to_csv(
        self, path: Path, metrics: list[str], *, hours: float = 24.0
    ) -> tuple[bool, str]:
        """Write persisted metric history as CSV.

        Rows are emitted in long form (``timestamp, metric, value``) rather than
        pivoted into a column per metric, because metrics are sampled at
        different cadences and a pivot would be mostly empty cells.
        """
        if self._history is None:
            return False, "History is not available"
        try:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp_iso", "timestamp_unix", "metric", "value"])
                rows = 0
                for metric in metrics:
                    series = self._history.stored_series(metric, hours=hours)
                    for moment, value in series:
                        writer.writerow([
                            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(moment)),
                            f"{moment:.3f}",
                            metric,
                            f"{value:.6g}",
                        ])
                        rows += 1
        except OSError as exc:
            return False, f"Could not write {path.name}: {exc}"
        return True, f"Exported {rows:,} samples across {len(metrics)} metrics"

    def live_series_to_csv(
        self, path: Path, metrics: list[str]
    ) -> tuple[bool, str]:
        """Write the in-memory chart buffers as CSV.

        Used when persistence is disabled but the user still wants the data
        currently on screen.
        """
        if self._history is None:
            return False, "History is not available"
        try:
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["seconds_ago", "metric", "value"])
                rows = 0
                for metric in metrics:
                    times, values = self._history.series(metric).snapshot()
                    for moment, value in zip(times, values, strict=True):
                        writer.writerow([f"{moment:.2f}", metric, f"{value:.6g}"])
                        rows += 1
        except OSError as exc:
            return False, f"Could not write {path.name}: {exc}"
        return True, f"Exported {rows:,} in-memory samples"

    # --------------------------------------------------------------------- JSON

    def snapshots_to_json(
        self, path: Path, snapshots: dict[str, object]
    ) -> tuple[bool, str]:
        """Write every current snapshot as one JSON document."""
        document = {
            "generated_at": time.time(),
            "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "application": APP_NAME,
            "host": platform.node(),
            "kernel": platform.release(),
            "domains": {
                domain: _plain(snapshot) for domain, snapshot in snapshots.items()
            },
        }
        try:
            path.write_text(
                json.dumps(document, indent=2, sort_keys=False), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError) as exc:
            return False, f"Could not write {path.name}: {exc}"
        return True, f"Exported {len(snapshots)} domains to {path.name}"

    # ----------------------------------------------------------------- Markdown

    def report_to_markdown(
        self, path: Path, snapshots: dict[str, object],
        report: DiagnosticReport | None = None,
    ) -> tuple[bool, str]:
        """Write a human-readable hardware and health report."""
        out = io.StringIO()
        out.write(f"# System report — {platform.node()}\n\n")
        out.write(f"*Generated by {APP_NAME} on "
                  f"{time.strftime('%Y-%m-%d at %H:%M:%S')}*\n\n")

        self._markdown_overview(out, snapshots)
        if report is not None:
            self._markdown_diagnostics(out, report)
        self._markdown_hardware(out, snapshots)

        try:
            path.write_text(out.getvalue(), encoding="utf-8")
        except OSError as exc:
            return False, f"Could not write {path.name}: {exc}"
        return True, f"Report written to {path.name}"

    def _markdown_overview(self, out: io.StringIO, snapshots: dict) -> None:
        """Summary table of the headline figures."""
        out.write("## Overview\n\n| Metric | Value |\n| --- | --- |\n")
        system = snapshots.get("system")
        if system is not None:
            out.write(f"| Distribution | {_text(system.os.distribution)} |\n")
            out.write(f"| Kernel | {_text(system.os.kernel)} |\n")
            out.write(f"| Uptime | {duration(system.os.uptime_seconds)} |\n")
            out.write(f"| Desktop | {_text(system.os.desktop_environment)} "
                      f"({_text(system.os.session_type)}) |\n")
        cpu = snapshots.get("cpu")
        if cpu is not None:
            out.write(f"| Processor | {cpu.topology.model} |\n")
            out.write(f"| CPU usage | {percent(cpu.usage)} |\n")
            out.write(f"| CPU temperature | {temperature(cpu.temperature)} |\n")
            out.write(f"| Load average | "
                      f"{', '.join(f'{v:.2f}' for v in cpu.load_average)} |\n")
        memory = snapshots.get("memory")
        if memory is not None:
            out.write(f"| Memory | {bytes_(memory.used_bytes)} of "
                      f"{bytes_(memory.total_bytes)} ({percent(memory.percent)}) |\n")
            out.write(f"| Swap | {bytes_(memory.swap_used_bytes)} of "
                      f"{bytes_(memory.swap_total_bytes)} |\n")
        gpu = snapshots.get("gpu")
        if gpu is not None and gpu.gpus:
            primary = gpu.primary
            out.write(f"| Graphics | {primary.device.name} |\n")
            out.write(f"| GPU usage | {percent(primary.utilisation)} |\n")
        battery = snapshots.get("battery")
        if battery is not None and battery.present:
            out.write(f"| Battery | {percent(battery.percent)} "
                      f"({_text(battery.status)}) |\n")
            out.write(f"| Battery health | {percent(battery.health_percent)} |\n")
        out.write("\n")

    def _markdown_diagnostics(self, out: io.StringIO, report: DiagnosticReport) -> None:
        """Diagnostics section, problems first."""
        out.write("## Diagnostics\n\n")
        marks = {
            Severity.OK: "OK", Severity.INFO: "Notice",
            Severity.WARNING: "Warning", Severity.CRITICAL: "Critical",
        }
        out.write(f"Overall assessment: **{report.worst.label}** "
                  f"({len(report.results)} checks in "
                  f"{report.duration_s * 1000:.0f} ms)\n\n")
        problems = report.problems
        if problems:
            out.write("### Findings\n\n")
            for result in problems:
                out.write(f"#### {marks[result.severity]}: {result.title}\n\n")
                out.write(f"{result.summary}\n\n")
                if result.advice:
                    out.write(f"> **Recommended action:** {result.advice}\n\n")
                if result.evidence:
                    for key, value in result.evidence.items():
                        out.write(f"- {key}: {value}\n")
                    out.write("\n")
        else:
            out.write("No problems were found.\n\n")

        skipped = [r for r in report.results if r.skipped]
        if skipped:
            out.write("### Checks that could not run\n\n")
            for result in skipped:
                out.write(f"- **{result.title}** — {result.summary}\n")
            out.write("\n")

        passed = [r for r in report.results if r.passed]
        if passed:
            out.write("### Checks that passed\n\n")
            for result in passed:
                out.write(f"- **{result.title}** — {result.summary}\n")
            out.write("\n")

    def _markdown_hardware(self, out: io.StringIO, snapshots: dict) -> None:
        """Detailed hardware inventory."""
        cpu = snapshots.get("cpu")
        if cpu is not None:
            topology = cpu.topology
            out.write("## Processor\n\n| Property | Value |\n| --- | --- |\n")
            for label, value in (
                ("Model", topology.model), ("Vendor", topology.vendor),
                ("Architecture", topology.architecture),
                ("Sockets", topology.sockets),
                ("Physical cores", topology.physical_cores),
                ("Logical cores", topology.logical_cores),
                ("Threads per core", topology.threads_per_core),
                ("Governor", cpu.governor), ("Driver", cpu.driver),
                ("Turbo enabled", cpu.turbo_enabled),
                ("Virtualisation", topology.virtualisation),
                ("Microcode", topology.microcode),
            ):
                out.write(f"| {label} | {_text(value)} |\n")
            for level, size in sorted(topology.cache.items()):
                out.write(f"| Cache {level} | {size} |\n")
            out.write("\n")

        storage = snapshots.get("storage")
        if storage is not None and storage.disks:
            out.write("## Storage\n\n")
            out.write("| Device | Type | Capacity | Model | Health | Temperature |\n")
            out.write("| --- | --- | --- | --- | --- | --- |\n")
            for disk in storage.disks:
                out.write(
                    f"| {disk.path} | {disk.kind.value} | "
                    f"{bytes_(disk.size_bytes)} | {_text(disk.model)} | "
                    f"{disk.health.verdict.value} | "
                    f"{temperature(disk.health.temperature)} |\n"
                )
            out.write("\n### Filesystems\n\n")
            out.write("| Mount point | Filesystem | Size | Used | Free |\n")
            out.write("| --- | --- | --- | --- | --- |\n")
            for filesystem in storage.filesystems:
                out.write(
                    f"| {_text(filesystem.mountpoint)} | "
                    f"{_text(filesystem.filesystem)} | "
                    f"{bytes_(filesystem.total_bytes)} | "
                    f"{bytes_(filesystem.used_bytes)} "
                    f"({percent(filesystem.percent)}) | "
                    f"{bytes_(filesystem.free_bytes)} |\n"
                )
            out.write("\n")

        network = snapshots.get("network")
        if network is not None and network.interfaces:
            out.write("## Network\n\n")
            out.write("| Interface | Type | State | IPv4 | MAC |\n")
            out.write("| --- | --- | --- | --- | --- |\n")
            for interface in network.interfaces:
                out.write(
                    f"| {interface.name} | {interface.kind.value} | "
                    f"{'up' if interface.is_up else 'down'} | "
                    f"{interface.ipv4 or '—'} | {_text(interface.mac)} |\n"
                )
            out.write("\n")

        system = snapshots.get("system")
        if system is not None:
            firmware = system.firmware
            out.write("## Firmware\n\n| Property | Value |\n| --- | --- |\n")
            for label, value in (
                ("System", f"{_text(firmware.system_vendor)} "
                           f"{_text(firmware.product_name)}"),
                ("Motherboard", f"{_text(firmware.board_vendor)} "
                                f"{_text(firmware.board_name)}"),
                ("BIOS", f"{_text(firmware.bios_vendor)} "
                         f"{_text(firmware.bios_version)}"),
                ("BIOS date", firmware.bios_date),
                ("Firmware type", firmware.firmware_type),
                ("Secure Boot", firmware.secure_boot),
                ("Chassis", firmware.chassis_type),
            ):
                out.write(f"| {label} | {_text(value)} |\n")
            out.write("\n")
            for heading, devices in system.device_groups:
                if not devices:
                    continue
                out.write(f"## {heading}\n\n")
                for device in devices:
                    detail = f" — {device.detail}" if device.detail else ""
                    out.write(f"- {device.name}{detail}\n")
                out.write("\n")

    # --------------------------------------------------------------------- HTML

    def report_to_html(
        self, path: Path, snapshots: dict[str, object],
        report: DiagnosticReport | None = None,
    ) -> tuple[bool, str]:
        """Write a self-contained, printable HTML report.

        Styling is inlined so the file works when opened directly from disk with
        no network access -- which matters because this application never
        requires the internet.
        """
        markdown_buffer = io.StringIO()
        self._markdown_overview(markdown_buffer, snapshots)
        if report is not None:
            self._markdown_diagnostics(markdown_buffer, report)
        self._markdown_hardware(markdown_buffer, snapshots)
        body = _markdown_to_html(markdown_buffer.getvalue())

        document = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>System report — {html.escape(platform.node())}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 62rem; margin: 2rem auto; padding: 0 1.5rem;
         line-height: 1.6; color: #1a1d21; background: #fff; }}
  h1 {{ font-size: 1.9rem; border-bottom: 2px solid #2563eb; padding-bottom: .4rem; }}
  h2 {{ font-size: 1.35rem; margin-top: 2.2rem; color: #1e40af; }}
  h3 {{ font-size: 1.1rem; margin-top: 1.6rem; }}
  h4 {{ font-size: 1rem; margin-top: 1.2rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: .8rem 0 1.4rem; }}
  th, td {{ text-align: left; padding: .45rem .7rem;
           border-bottom: 1px solid #e5e7eb; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
  tr:hover td {{ background: #f9fafb; }}
  blockquote {{ margin: .8rem 0; padding: .7rem 1rem; background: #fef3c7;
               border-left: 4px solid #f59e0b; }}
  code {{ background: #f3f4f6; padding: .1rem .3rem; border-radius: 3px; }}
  footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e5e7eb;
           color: #6b7280; font-size: .85rem; }}
  @media print {{ body {{ max-width: none; margin: 0; }}
                  h2 {{ page-break-after: avoid; }} }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e5e7eb; }}
    th {{ background: #1f2329; }} td, th {{ border-color: #2a2f37; }}
    tr:hover td {{ background: #1a1e24; }}
    h2 {{ color: #93c5fd; }}
    blockquote {{ background: #3a2f10; border-color: #f59e0b; }}
    code {{ background: #1f2329; }}
  }}
</style></head><body>
<h1>System report — {html.escape(platform.node())}</h1>
<p><em>Generated by {APP_NAME} on
{html.escape(time.strftime('%Y-%m-%d at %H:%M:%S'))}</em></p>
{body}
<footer>Produced locally by {APP_NAME}. No data left this machine.</footer>
</body></html>
"""
        try:
            path.write_text(document, encoding="utf-8")
        except OSError as exc:
            return False, f"Could not write {path.name}: {exc}"
        return True, f"HTML report written to {path.name} (print to PDF from a browser)"


def _text(value: object) -> str:
    """Format a value for a report cell, mapping absence onto an em dash."""
    if isinstance(value, Unavailable) or value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return text if text else "—"


def _markdown_to_html(markdown: str) -> str:
    """Convert the small Markdown subset this module emits into HTML.

    Deliberately not a general Markdown implementation: it handles exactly the
    headings, tables, lists and blockquotes produced above.  Pulling in a
    Markdown library for this would be a dependency for one internal conversion.
    """
    lines = markdown.splitlines()
    out: list[str] = []
    in_table = False
    in_list = False

    def close_blocks() -> None:
        nonlocal in_table, in_list
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False

    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            close_blocks()
            index += 1
            continue

        if stripped.startswith("#"):
            close_blocks()
            level = len(stripped) - len(stripped.lstrip("#"))
            content = _inline(stripped[level:].strip())
            out.append(f"<h{min(level, 6)}>{content}</h{min(level, 6)}>")
            index += 1
            continue

        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            separator = (
                index + 1 < len(lines)
                and set(
                    lines[index + 1].strip().strip("|").replace("|", "")
                ) <= set("- :")
                and "-" in lines[index + 1]
            )
            if not in_table and separator:
                out.append("<table><thead><tr>"
                           + "".join(f"<th>{_inline(c)}</th>" for c in cells)
                           + "</tr></thead><tbody>")
                in_table = True
                index += 2  # skip the separator row
                continue
            if in_table:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells)
                           + "</tr>")
                index += 1
                continue

        if stripped.startswith("> "):
            close_blocks()
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
            index += 1
            continue

        if stripped.startswith("- "):
            if not in_list:
                close_blocks()
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            index += 1
            continue

        close_blocks()
        out.append(f"<p>{_inline(stripped)}</p>")
        index += 1

    close_blocks()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape HTML then apply bold, italic and code spans."""
    escaped = html.escape(text)
    for marker, tag in (("**", "strong"), ("*", "em"), ("`", "code")):
        parts = escaped.split(marker)
        if len(parts) >= 3:
            rebuilt = parts[0]
            for position, part in enumerate(parts[1:], start=1):
                rebuilt += (f"<{tag}>{part}</{tag}>" if position % 2 else part)
            escaped = rebuilt
    return escaped
