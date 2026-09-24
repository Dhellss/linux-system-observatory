"""Tests for report and data export."""

from __future__ import annotations

import json
import time

from app.models.base import Reason, Unavailable
from app.models.cpu import CpuSnapshot, CpuTopology
from app.models.diagnostics import CheckResult, DiagnosticReport, Severity
from app.models.memory import MemorySnapshot
from app.services.export import ExportService, _markdown_to_html, _plain


def _snapshots() -> dict[str, object]:
    """A small but representative snapshot set."""
    return {
        "cpu": CpuSnapshot(
            usage=37.5,
            temperature=61.0,
            package_power_w=Unavailable(Reason.PERMISSION, "root only"),
            topology=CpuTopology(model="Test CPU", logical_cores=8),
        ),
        "memory": MemorySnapshot(
            total_bytes=16 * 1024**3,
            used_bytes=8 * 1024**3,
            available_bytes=8 * 1024**3,
        ),
    }


def _report() -> DiagnosticReport:
    """A report containing one of each result kind."""
    return DiagnosticReport(
        results=(
            CheckResult("a", "A warning", Severity.WARNING, "Something is off",
                        "Do this about it", "Processor", {"Detail": "value"}),
            CheckResult("b", "Healthy check", Severity.OK, "All good",
                        category="Memory"),
            CheckResult("c", "Skipped check", Severity.OK, "No permission",
                        category="Storage", skipped=True),
        ),
        generated_at=time.time(),
        duration_s=0.01,
    )


class TestPlainConversion:
    """Snapshot to JSON-safe primitives."""

    def test_unavailable_becomes_structured(self) -> None:
        """Absence survives export as data, not as null.

        A consumer can then tell "no sensor on this hardware" apart from
        "field omitted", which a bare null would lose.
        """
        result = _plain(Unavailable(Reason.NO_TOOL, "smartctl"))
        assert result["available"] is False
        assert result["reason"] == "no_tool"
        assert "smartctl" in result["explanation"]

    def test_nested_dataclasses_are_flattened(self) -> None:
        """A snapshot converts recursively."""
        result = _plain(CpuSnapshot(usage=50.0))
        assert result["usage"] == 50.0
        assert isinstance(result["topology"], dict)

    def test_enums_become_their_value(self) -> None:
        """Enums export as readable strings."""
        assert _plain(Severity.WARNING) == 2


class TestJsonExport:
    """The JSON document."""

    def test_writes_valid_json(self, temp_dir) -> None:
        """The document parses and carries the expected structure."""
        path = temp_dir / "out.json"
        ok, _message = ExportService().snapshots_to_json(path, _snapshots())
        assert ok
        document = json.loads(path.read_text())
        assert "cpu" in document["domains"]
        assert document["domains"]["cpu"]["usage"] == 37.5
        assert document["domains"]["cpu"]["package_power_w"]["available"] is False

    def test_reports_an_unwritable_path(self, temp_dir) -> None:
        """A failure is reported, not raised."""
        ok, message = ExportService().snapshots_to_json(
            temp_dir / "nope" / "out.json", _snapshots()
        )
        assert not ok
        assert "Could not write" in message


class TestMarkdownExport:
    """The Markdown report."""

    def test_contains_the_expected_sections(self, temp_dir) -> None:
        """Overview, diagnostics and hardware all appear."""
        path = temp_dir / "report.md"
        ok, _message = ExportService().report_to_markdown(
            path, _snapshots(), _report()
        )
        assert ok
        content = path.read_text()
        assert "## Overview" in content
        assert "## Diagnostics" in content
        assert "## Processor" in content
        assert "Test CPU" in content

    def test_separates_findings_from_skipped_checks(self, temp_dir) -> None:
        """A skipped check is never presented as a pass."""
        path = temp_dir / "report.md"
        ExportService().report_to_markdown(path, _snapshots(), _report())
        content = path.read_text()
        assert "### Findings" in content
        assert "could not run" in content
        assert "Do this about it" in content


class TestHtmlExport:
    """The printable HTML report."""

    def test_is_self_contained(self, temp_dir) -> None:
        """No external resource is referenced, so it works offline."""
        path = temp_dir / "report.html"
        ok, _message = ExportService().report_to_html(path, _snapshots(), _report())
        assert ok
        content = path.read_text()
        assert content.startswith("<!DOCTYPE html>")
        assert "<style>" in content
        assert "http://" not in content.replace("http://www.w3.org", "")
        assert "src=" not in content

    def test_escapes_html_in_content(self, temp_dir) -> None:
        """A device name containing markup cannot break the document."""
        snapshots = _snapshots()
        snapshots["cpu"] = CpuSnapshot(
            topology=CpuTopology(model="<script>alert(1)</script>")
        )
        path = temp_dir / "report.html"
        ExportService().report_to_html(path, snapshots, None)
        content = path.read_text()
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;" in content


class TestMarkdownToHtml:
    """The internal Markdown subset converter."""

    def test_headings(self) -> None:
        """Headings map to the right level."""
        assert "<h2>Title</h2>" in _markdown_to_html("## Title")

    def test_tables(self) -> None:
        """Pipe tables become real table markup."""
        html = _markdown_to_html("| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert "<table>" in html and "<th>A</th>" in html and "<td>1</td>" in html

    def test_lists_and_quotes(self) -> None:
        """Lists and blockquotes are converted."""
        assert "<li>item</li>" in _markdown_to_html("- item")
        assert "<blockquote>" in _markdown_to_html("> note")

    def test_inline_emphasis(self) -> None:
        """Bold spans are converted, and the text is escaped first."""
        assert "<strong>bold</strong>" in _markdown_to_html("**bold**")
