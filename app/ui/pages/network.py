"""Network page: interfaces, throughput, sockets and on-demand probes."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.collectors.network import NetworkCollector
from app.core.units import bytes_, count, percent, rate, text
from app.models.base import is_number
from app.models.network import InterfaceKind, LatencyResult, NetworkSnapshot
from app.ui.pages.base import Page
from app.ui.widgets.card import Card
from app.ui.widgets.chart import LiveChart
from app.ui.widgets.stat import Badge, Divider, KeyValueTable, StatRow
from app.ui.widgets.table import Column, DataTable


class NetworkPage(Page):
    """Interfaces, traffic, connections and latency."""

    domain = "network"
    title = "Network"
    icon = "◎"
    section = "Monitor"
    description = "Interfaces, throughput, sockets and latency"

    def build_ui(self) -> None:
        """Assemble the page."""
        accent = self.context.colour("network")
        self._accent = accent
        self._build_summary(accent)
        self._build_interfaces(accent)
        self._build_connections(accent)
        self._build_tools(accent)
        self.add_stretch()
        self._interface_sections: dict[str, _InterfaceSection] = {}

    def _build_summary(self, accent: str) -> None:
        """Aggregate throughput and the traffic chart."""
        card = Card("Traffic", theme=self.theme, accent=accent)
        self._stats = StatRow(theme=self.theme, columns=4)
        for key, label in (
            ("down", "Download"), ("up", "Upload"),
            ("rx_packets", "Packets in"), ("tx_packets", "Packets out"),
            ("total_rx", "Received total"), ("total_tx", "Sent total"),
            ("errors", "Errors and drops"), ("status", "Connectivity"),
        ):
            self._stats.add(key, label)
        card.add(self._stats)

        window = self.context.config.settings.charts.history_seconds
        self._chart = LiveChart(
            theme=self.theme, window_seconds=window, y_range=None,
            y_label="B/s", height=170,
        )
        self._chart.add_series("network.download", self.palette_tokens.series[2])
        self._chart.add_series("network.upload", self.palette_tokens.series[1])
        card.add(self._chart)
        self.add(card)

    def _build_interfaces(self, accent: str) -> None:
        """Per-interface detail sections."""
        self.add_section("Interfaces")
        self._interface_host = QWidget(self)
        self._interface_layout = QVBoxLayout(self._interface_host)
        self._interface_layout.setContentsMargins(0, 0, 0, 0)
        self._interface_layout.setSpacing(self.metrics.space_4)
        self.add(self._interface_host)

    def _build_connections(self, accent: str) -> None:
        """Socket table with a listening-only filter."""
        card = Card(
            "Connections", theme=self.theme,
            subtitle="Sockets owned by other users need elevated privileges "
                     "to attribute",
            accent=accent,
        )
        controls = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by address, port or process…")
        self._search.setObjectName("SearchField")
        controls.addWidget(self._search, 1)

        self._listening_only = QPushButton("Listening only")
        self._listening_only.setCheckable(True)
        controls.addWidget(self._listening_only)
        card.add_layout(controls)

        columns: list[Column] = [
            Column("proto", "Protocol", lambda c: c.protocol, width=80),
            Column("local", "Local address",
                   lambda c: f"{c.local_address}:{c.local_port}",
                   width=190, mono=True),
            Column("remote", "Remote address",
                   lambda c: (
                       f"{c.remote_address}:{c.remote_port}"
                       if c.remote_address else "—"
                   ), width=190, mono=True),
            Column("state", "State", lambda c: c.status, width=110,
                   colour=lambda c: (
                       self.palette_tokens.success if c.status == "ESTABLISHED"
                       else self.palette_tokens.info if c.is_listening
                       else None
                   )),
            Column("pid", "PID", lambda c: c.pid or 0,
                   display=lambda v: str(v) if v else "—", width=70, mono=True),
            Column("process", "Process", lambda c: text(c.process_name), width=0),
        ]
        self._connections = DataTable(columns, theme=self.theme, stretch_column=5)
        self._connections.setMinimumHeight(int(self.metrics.row_height * 10))
        self._search.textChanged.connect(self._connections.proxy.set_search)
        self._listening_only.toggled.connect(self._apply_connection_filter)
        card.add(self._connections)

        self._protocol_stats = KeyValueTable(
            theme=self.theme, columns=3, label_width=130
        )
        for key, label in (
            ("established", "TCP established"), ("listening", "TCP listening"),
            ("time_wait", "TCP time-wait"), ("tcp_total", "TCP total"),
            ("udp", "UDP sockets"), ("unix", "Unix sockets"),
            ("retransmits", "TCP retransmits"),
        ):
            self._protocol_stats.add_row(key, label)
        card.add(Divider())
        card.add(self._protocol_stats)
        self.add(card)

    def _build_tools(self, accent: str) -> None:
        """Latency probe and routing details."""
        grid = self.add_grid(2)

        routing = Card("Routing and DNS", theme=self.theme, accent=accent)
        self._routing = KeyValueTable(theme=self.theme, label_width=140)
        for key, label in (
            ("hostname", "Hostname"), ("gateway4", "IPv4 gateway"),
            ("gateway6", "IPv6 gateway"), ("dns", "DNS servers"),
            ("default", "Default interface"),
        ):
            self._routing.add_row(key, label)
        routing.add(self._routing)
        grid.addWidget(routing, 0, 0)

        probe = Card(
            "Latency probe", theme=self.theme,
            subtitle="Manual only — this is the sole feature that contacts a "
                     "remote host",
            accent=accent,
        )
        row = QHBoxLayout()
        self._probe_target = QLineEdit(
            str(self.context.config.get("privacy.latency_target", "1.1.1.1"))
        )
        self._probe_target.setPlaceholderText("Host or IP address")
        row.addWidget(self._probe_target, 1)
        self._probe_button = QPushButton("Run probe")
        self._probe_button.setProperty("variant", "primary")
        self._probe_button.clicked.connect(self._run_probe)
        row.addWidget(self._probe_button)
        probe.add_layout(row)

        self._probe_notice = QLabel(
            "Latency probes are disabled in Privacy settings. Enable them there "
            "to use this tool."
        )
        self._probe_notice.setWordWrap(True)
        self._probe_notice.setStyleSheet(
            f"color: {self.palette_tokens.warning};"
        )
        probe.add(self._probe_notice)

        self._probe_results = KeyValueTable(theme=self.theme, label_width=140)
        for key, label in (
            ("target", "Target"), ("reachable", "Reachable"),
            ("avg", "Average"), ("min", "Minimum"), ("max", "Maximum"),
            ("jitter", "Jitter (mdev)"), ("loss", "Packet loss"),
        ):
            self._probe_results.add_row(key, label)
        probe.add(self._probe_results)
        grid.addWidget(probe, 0, 1)
        self._sync_probe_availability()

    # ------------------------------------------------------------------ updates

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        """Apply a network snapshot."""
        if not isinstance(snapshot, NetworkSnapshot):
            return
        history = self.context.history
        for metric in ("network.download", "network.upload"):
            self._chart.update_series(metric, history.series(metric))
        if not self.is_visible_page:
            return

        totals = snapshot.totals
        palette = self.palette_tokens
        self._stats["down"].set_value(rate(totals.download_bytes_per_s))
        self._stats["up"].set_value(rate(totals.upload_bytes_per_s))
        self._stats["rx_packets"].set_value(f"{totals.rx_packets_per_s:,.0f}/s")
        self._stats["tx_packets"].set_value(f"{totals.tx_packets_per_s:,.0f}/s")
        self._stats["total_rx"].set_value(bytes_(totals.total_rx_bytes))
        self._stats["total_tx"].set_value(bytes_(totals.total_tx_bytes))
        self._stats["errors"].set_value(
            count(totals.error_count),
            colour=palette.warning if totals.error_count > 1000 else None,
        )
        self._stats["status"].set_value(
            "Online" if snapshot.online else "Offline",
            colour=palette.success if snapshot.online else palette.danger,
        )

        for interface in snapshot.interfaces:
            section = self._interface_sections.get(interface.name)
            if section is None:
                section = _InterfaceSection(self, interface, self._accent)
                self._interface_sections[interface.name] = section
                self._interface_layout.addWidget(section.card)
            section.update(interface)
        present = {i.name for i in snapshot.interfaces}
        for name, section in self._interface_sections.items():
            section.card.setVisible(name in present)

        self._connections.set_rows(snapshot.connections)
        protocols = snapshot.protocols
        self._protocol_stats.update_many({
            "established": str(protocols.tcp_established),
            "listening": str(protocols.tcp_listening),
            "time_wait": str(protocols.tcp_time_wait),
            "tcp_total": str(protocols.tcp_total),
            "udp": str(protocols.udp_total),
            "unix": str(protocols.unix_total),
            "retransmits": count(protocols.tcp_retransmits),
        })

        default = snapshot.default_interface
        self._routing.update_many({
            "hostname": snapshot.hostname,
            "gateway4": snapshot.gateway_v4,
            "gateway6": snapshot.gateway_v6,
            "dns": ", ".join(snapshot.dns_servers) or "—",
            "default": default.name if default else "—",
        })

    def _apply_connection_filter(self, listening_only: bool) -> None:
        """Restrict the socket table to listening sockets."""
        self._connections.proxy.set_predicate(
            (lambda c: c.is_listening) if listening_only else None
        )

    # ------------------------------------------------------------------- probes

    def _sync_probe_availability(self) -> None:
        """Enable the probe controls only when the user has opted in."""
        allowed = bool(self.context.config.get("privacy.allow_latency_probes", False))
        self._probe_button.setEnabled(allowed)
        self._probe_target.setEnabled(allowed)
        self._probe_notice.setVisible(not allowed)

    def _run_probe(self) -> None:
        """Run a latency probe on a worker thread.

        Pinging blocks for seconds, so it must never run on the GUI thread. The
        probe is only ever started by this button -- never automatically.
        """
        target = self._probe_target.text().strip()
        if not target:
            return
        collector = self.context.monitor.collector_of("network", NetworkCollector)
        if collector is None:
            return
        self._probe_button.setEnabled(False)
        self._probe_button.setText("Probing…")
        self.context.config.set("privacy.latency_target", target)

        task = _ProbeTask(collector, target)
        task.signals.finished.connect(self._on_probe_finished)
        QThreadPool.globalInstance().start(task)

    def _on_probe_finished(self, result: LatencyResult) -> None:
        """Display probe results on the GUI thread."""
        self._probe_button.setEnabled(True)
        self._probe_button.setText("Run probe")
        self._probe_results.update_many({
            "target": result.target,
            "reachable": (
                "Yes" if result.reachable else f"No — {result.note or 'no reply'}"
            ),
            "avg": _milliseconds(result.avg_ms),
            "min": _milliseconds(result.min_ms),
            "max": _milliseconds(result.max_ms),
            "jitter": _milliseconds(result.stddev_ms),
            "loss": percent(result.packet_loss_percent),
        })
        self.status_message.emit(
            f"Probe to {result.target}: "
            + (f"{result.avg_ms:.1f} ms average" if result.reachable
               else "unreachable")
        )

    def on_shown(self) -> None:
        """Refresh on entry and re-check the privacy opt-in."""
        super().on_shown()
        self._sync_probe_availability()
        self.refresh()


def _milliseconds(value: object) -> str:
    """Format a latency figure."""
    return f"{value:.2f} ms" if is_number(value) else "—"


class _ProbeSignals(QObject):
    """Signals for :class:`_ProbeTask`, which cannot itself be a ``QObject``."""

    finished = Signal(object)


class _ProbeTask(QRunnable):
    """Runs one latency probe off the GUI thread."""

    def __init__(self, collector, target: str) -> None:
        super().__init__()
        self._collector = collector
        self._target = target
        self.signals = _ProbeSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        """Execute the probe and emit the result."""
        try:
            result = self._collector.ping(self._target)
        except Exception:
            from app.models.network import LatencyResult

            result = LatencyResult(self._target, False, note="probe failed")
        self.signals.finished.emit(result)


class _InterfaceSection:
    """Card and widgets for one network interface."""

    def __init__(self, page: NetworkPage, interface, accent: str) -> None:
        theme = page.theme
        self._page = page
        self.card = Card(
            interface.name, theme=theme,
            subtitle=interface.kind.value, accent=accent,
        )
        self._state_badge = Badge("", theme=theme)
        self.card.add_action(self._state_badge)

        self._stats = StatRow(theme=theme, columns=4)
        for key, label in (
            ("down", "Download"), ("up", "Upload"),
            ("rx", "Received"), ("tx", "Sent"),
        ):
            self._stats.add(key, label, size="small")
        self.card.add(self._stats)

        self._details = KeyValueTable(theme=theme, columns=2, label_width=120)
        for key, label in (
            ("ipv4", "IPv4 address"), ("ipv6", "IPv6 address"),
            ("mac", "Hardware address"), ("mtu", "MTU"),
            ("speed", "Link speed"), ("duplex", "Duplex"),
            ("driver", "Driver"), ("errors", "Errors / drops"),
        ):
            self._details.add_row(key, label)
        self.card.add(self._details)

        self._wireless = KeyValueTable(theme=theme, columns=2, label_width=120)
        for key, label in (
            ("ssid", "Network"), ("signal", "Signal"),
            ("quality", "Quality"), ("channel", "Channel"),
            ("frequency", "Frequency"), ("bitrate", "Link rate"),
        ):
            self._wireless.add_row(key, label)
        self._wireless.setVisible(False)
        self.card.add(self._wireless)

    def update(self, interface) -> None:
        """Refresh from an interface snapshot."""
        theme = self._page.theme
        counters = interface.counters
        self._state_badge.set_state(
            ("Default route" if interface.is_default
             else "Up" if interface.is_up else "Down"),
            theme.palette.accent if interface.is_default
            else theme.palette.success if interface.is_up
            else theme.palette.text_subtle,
        )
        self._stats["down"].set_value(rate(counters.download_bytes_per_s))
        self._stats["up"].set_value(rate(counters.upload_bytes_per_s))
        self._stats["rx"].set_value(bytes_(counters.total_rx_bytes))
        self._stats["tx"].set_value(bytes_(counters.total_tx_bytes))

        speed = interface.speed_mbps
        self._details.update_many({
            "ipv4": interface.ipv4 or "—",
            "ipv6": interface.ipv6 or "—",
            "mac": interface.mac,
            "mtu": interface.mtu,
            "speed": f"{speed} Mbps" if isinstance(speed, int) else speed,
            "duplex": interface.duplex,
            "driver": interface.driver,
            "errors": (
                f"{counters.rx_errors + counters.tx_errors} errors, "
                f"{counters.rx_dropped + counters.tx_dropped} dropped"
            ),
        })

        wireless = interface.wireless
        if wireless is not None and interface.kind is InterfaceKind.WIRELESS:
            self._wireless.setVisible(True)
            self._wireless.update_many({
                "ssid": wireless.ssid,
                "signal": (
                    f"{wireless.signal_dbm:.0f} dBm"
                    if is_number(wireless.signal_dbm)
                    else wireless.signal_dbm
                ),
                "quality": percent(wireless.quality_percent),
                "channel": wireless.channel,
                "frequency": (
                    f"{wireless.frequency_mhz:.0f} MHz"
                    if is_number(wireless.frequency_mhz)
                    else wireless.frequency_mhz
                ),
                "bitrate": (
                    f"{wireless.bitrate_mbps:.0f} Mbps"
                    if is_number(wireless.bitrate_mbps)
                    else wireless.bitrate_mbps
                ),
            })
        else:
            self._wireless.setVisible(False)
