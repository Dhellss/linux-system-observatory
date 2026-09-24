"""Network collector: interfaces, throughput, sockets and routing.

Sources
-------
``psutil.net_io_counters(pernic=True)``
    Traffic counters.  Converted to rates by :class:`~app.collectors.base.RateTracker`,
    which is what makes an interface being taken down mid-session safe: the
    counter resets to zero and a negative delta is reported as no traffic rather
    than as a large negative rate.
``/sys/class/net/*``
    Link state, MTU, speed, duplex and driver -- none of which psutil exposes.
``/proc/net/route`` and ``/proc/net/ipv6_route``
    Default gateway, parsed directly.  The hex, little-endian encoding of the
    IPv4 table is handled explicitly.
``ss``
    Socket-to-process attribution, which ``psutil.net_connections`` cannot do
    without elevated privileges for other users' sockets.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import struct
import time
from typing import Any, TypedDict

import psutil

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.network import (
    Address,
    Connection,
    Interface,
    InterfaceCounters,
    InterfaceKind,
    LatencyResult,
    NetworkSnapshot,
    ProtocolStats,
    WirelessInfo,
)
from app.utils import sysfs
from app.utils.shell import run

_NET = sysfs.SYS / "class/net"

#: Interface name prefixes that identify virtual devices.
_VIRTUAL_PREFIXES = (
    "veth", "docker", "br-", "virbr", "vmnet", "tap", "dummy",
    "bond", "team", "macvlan", "ifb", "kube", "cni", "flannel", "cali",
)
_TUNNEL_PREFIXES = ("tun", "tap", "wg", "ppp", "ipsec", "vti", "gre", "sit")


class _Static(TypedDict):
    """Per-interface attributes that do not change while it exists."""

    mac: str | None
    driver: str | None
    wireless: bool
    type: int | None


class NetworkCollector(Collector[NetworkSnapshot]):
    """Samples interfaces, throughput, connections and routing."""

    domain = "network"
    title = "Network"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        self._static_cache: dict[str, _Static] = {}
        self._known_interfaces: set[str] = set()
        #: Connections are enumerated less often than counters: on a busy host
        #: the socket table is large and parsing it every second is wasteful.
        self._connection_interval = 10.0
        self._connections: tuple[Connection, ...] = ()
        self._protocols = ProtocolStats()
        self._connections_read_at = 0.0

    def _collect(self) -> NetworkSnapshot:
        now = time.monotonic()
        counters = self._read_counters(now)
        addresses = self._read_addresses()
        stats = self._read_link_stats()
        default_v4, default_iface = self._read_default_route_v4()

        self._prune_departed(set(counters))

        interfaces: list[Interface] = []
        for name in sorted(counters):
            static = self._static(name)
            kind = self._classify(name, static)
            interfaces.append(
                Interface(
                    name=name,
                    kind=kind,
                    is_up=stats.get(name, (False, None, None))[0],
                    addresses=tuple(addresses.get(name, ())),
                    mac=static["mac"] or Unavailable(
                        Reason.NOT_EXPOSED, "no hardware address"
                    ),
                    mtu=stats.get(name, (False, None, None))[1],
                    speed_mbps=self._link_speed(name),
                    duplex=sysfs.read_text(_NET / name / "duplex") or Unavailable(
                        Reason.NOT_EXPOSED, "duplex not reported"
                    ),
                    driver=static["driver"] or Unavailable(
                        Reason.NOT_EXPOSED, "driver not identified"
                    ),
                    counters=counters[name],
                    wireless=(
                        self._read_wireless(name)
                        if kind is InterfaceKind.WIRELESS else None
                    ),
                    is_default=(name == default_iface),
                )
            )

        self._refresh_connections(now)
        return NetworkSnapshot(
            timestamp=now,
            interfaces=tuple(interfaces),
            totals=self._aggregate(counters, interfaces),
            connections=self._connections,
            protocols=self._protocols,
            gateway_v4=default_v4 or Unavailable(
                Reason.NOT_EXPOSED, "no default IPv4 route"
            ),
            gateway_v6=self._read_default_route_v6(),
            dns_servers=self._read_dns(),
            hostname=socket.gethostname() or Unavailable(Reason.ERROR, "unknown"),
            online=any(
                i.is_up and i.kind is not InterfaceKind.LOOPBACK and i.ipv4
                for i in interfaces
            ),
        )

    def _empty(self) -> NetworkSnapshot:
        return NetworkSnapshot()

    # ----------------------------------------------------------------- counters

    def _read_counters(self, now: float) -> dict[str, InterfaceCounters]:
        """Per-interface traffic counters converted to rates."""
        try:
            raw = psutil.net_io_counters(pernic=True)
        except (OSError, RuntimeError):
            return {}
        result: dict[str, InterfaceCounters] = {}
        # Local alias purely to keep the counter lines below readable.
        def rate(key: str, value: float) -> float:
            return self.rates.rate(key, value, now)

        for name, io in raw.items():
            result[name] = InterfaceCounters(
                download_bytes_per_s=rate(f"net.{name}.rx", io.bytes_recv),
                upload_bytes_per_s=rate(f"net.{name}.tx", io.bytes_sent),
                rx_packets_per_s=rate(f"net.{name}.rxp", io.packets_recv),
                tx_packets_per_s=rate(f"net.{name}.txp", io.packets_sent),
                rx_errors=io.errin,
                tx_errors=io.errout,
                rx_dropped=io.dropin,
                tx_dropped=io.dropout,
                total_rx_bytes=io.bytes_recv,
                total_tx_bytes=io.bytes_sent,
            )
        return result

    def _prune_departed(self, present: set[str]) -> None:
        """Forget counters for interfaces that no longer exist.

        Without this, unplugging a USB adapter and plugging in another that the
        kernel names identically would produce one enormous false spike from the
        stale counter baseline.
        """
        departed = self._known_interfaces - present
        for name in departed:
            self.rates.forget(f"net.{name}.")
            self._static_cache.pop(name, None)
            self._log.debug("Interface %s disappeared; counters reset", name)
        self._known_interfaces = present

    def _aggregate(
        self, counters: dict[str, InterfaceCounters], interfaces: list[Interface]
    ) -> InterfaceCounters:
        """Sum traffic across real interfaces only.

        Loopback is excluded because local IPC traffic would swamp the figure the
        user actually cares about, and virtual bridge devices are excluded
        because they mirror traffic already counted on their members.
        """
        real = [
            i.name for i in interfaces
            if i.kind not in (InterfaceKind.LOOPBACK, InterfaceKind.BRIDGE,
                              InterfaceKind.VIRTUAL)
        ]
        selected = [counters[n] for n in real if n in counters]
        if not selected:
            return InterfaceCounters()
        return InterfaceCounters(
            download_bytes_per_s=sum(c.download_bytes_per_s for c in selected),
            upload_bytes_per_s=sum(c.upload_bytes_per_s for c in selected),
            rx_packets_per_s=sum(c.rx_packets_per_s for c in selected),
            tx_packets_per_s=sum(c.tx_packets_per_s for c in selected),
            rx_errors=sum(c.rx_errors for c in selected),
            tx_errors=sum(c.tx_errors for c in selected),
            rx_dropped=sum(c.rx_dropped for c in selected),
            tx_dropped=sum(c.tx_dropped for c in selected),
            total_rx_bytes=sum(c.total_rx_bytes for c in selected),
            total_tx_bytes=sum(c.total_tx_bytes for c in selected),
        )

    # ---------------------------------------------------------------- addresses

    def _read_addresses(self) -> dict[str, list[Address]]:
        """Configured addresses per interface."""
        try:
            raw = psutil.net_if_addrs()
        except (OSError, RuntimeError):
            return {}
        families = {
            socket.AF_INET: "IPv4",
            socket.AF_INET6: "IPv6",
            getattr(psutil, "AF_LINK", socket.AF_PACKET): "MAC",
        }
        result: dict[str, list[Address]] = {}
        for name, entries in raw.items():
            addresses: list[Address] = []
            for entry in entries:
                family = families.get(entry.family)
                if family is None:
                    continue
                address = entry.address
                # Link-local IPv6 addresses carry a %scope suffix that is not
                # part of the address itself.
                scope = None
                if family == "IPv6" and "%" in address:
                    address, _, scope = address.partition("%")
                addresses.append(
                    Address(
                        family=family,
                        address=address,
                        netmask=entry.netmask,
                        broadcast=entry.broadcast,
                        scope=scope or self._ipv6_scope(address, family),
                    )
                )
            result[name] = addresses
        return result

    @staticmethod
    def _ipv6_scope(address: str, family: str) -> str | None:
        """Classify an address's scope for display."""
        if family != "IPv6":
            return None
        try:
            parsed = ipaddress.IPv6Address(address)
        except ValueError:
            return None
        if parsed.is_link_local:
            return "link-local"
        if parsed.is_private:
            return "unique-local"
        return "global"

    def _read_link_stats(self) -> dict[str, tuple[bool, int | None, str | None]]:
        """Link up/down state and MTU per interface."""
        try:
            raw = psutil.net_if_stats()
        except (OSError, RuntimeError):
            return {}
        return {
            name: (stats.isup, stats.mtu, getattr(stats, "duplex", None))
            for name, stats in raw.items()
        }

    def _link_speed(self, name: str):
        """Negotiated link speed in Mbps.

        Virtual and wireless interfaces report ``-1`` or ``4294967295`` here,
        both of which mean "not applicable" rather than a real speed.
        """
        speed = sysfs.read_int(_NET / name / "speed")
        if speed is None:
            return Unavailable(Reason.NOT_EXPOSED, "link speed not reported")
        value = int(speed)
        if value <= 0 or value >= 0xFFFFFFF:
            return Unavailable(Reason.NOT_EXPOSED, "no negotiated link speed")
        return value

    # ------------------------------------------------------------------- static

    def _static(self, name: str) -> _Static:
        """Static per-interface attributes, cached for the interface's lifetime."""
        if name in self._static_cache:
            return self._static_cache[name]
        kind = sysfs.read_int(_NET / name / "type")
        info: _Static = {
            "mac": sysfs.read_text(_NET / name / "address"),
            "driver": self._driver_of(name),
            "wireless": sysfs.exists(_NET / name / "wireless")
            or sysfs.exists(_NET / name / "phy80211"),
            "type": None if kind is None else int(kind),
        }
        self._static_cache[name] = info
        return info

    @staticmethod
    def _driver_of(name: str) -> str | None:
        """Kernel driver bound to an interface."""
        try:
            link = _NET / name / "device/driver"
            return link.resolve().name if link.exists() else None
        except OSError:
            return None

    def _classify(self, name: str, static: _Static) -> InterfaceKind:
        """Determine an interface's class.

        Detection is by kernel-reported facts first (the ``wireless`` directory,
        the ARPHRD type) and by name only as a last resort, because predictable
        network interface naming means names are no longer reliable indicators.
        """
        if name == "lo" or static["type"] == 772:
            return InterfaceKind.LOOPBACK
        if static["wireless"]:
            return InterfaceKind.WIRELESS
        if sysfs.exists(_NET / name / "bridge"):
            return InterfaceKind.BRIDGE
        if name.startswith(_TUNNEL_PREFIXES):
            return InterfaceKind.TUNNEL
        if name.startswith(_VIRTUAL_PREFIXES):
            return InterfaceKind.VIRTUAL
        if static["type"] == 32:
            return InterfaceKind.BLUETOOTH
        if name.startswith(("wwan", "wwp")) or static["type"] == 530:
            return InterfaceKind.CELLULAR
        if static["type"] == 1:
            return InterfaceKind.ETHERNET
        return InterfaceKind.UNKNOWN

    # ----------------------------------------------------------------- wireless

    def _read_wireless(self, name: str) -> WirelessInfo:
        """Wi-Fi radio details from ``/proc/net/wireless`` and ``iw``."""
        signal: float | None = None
        for line in sysfs.read_lines(sysfs.PROC / "net/wireless"):
            if not line.startswith(f"{name}:"):
                continue
            fields = line.split()
            if len(fields) >= 4:
                # Column 3 is the signal level in dBm, with a trailing dot.
                with contextlib.suppress(ValueError):
                    signal = float(fields[3].rstrip("."))
            break

        ssid = bssid = security = None
        frequency = bitrate = None
        channel = None
        result = run(["iw", "dev", name, "link"], timeout=2.0)
        if result.ok:
            for line in result.lines:
                if line.startswith("Connected to"):
                    bssid = line.split()[2]
                elif line.startswith("SSID:"):
                    ssid = line.split(":", 1)[1].strip()
                elif line.startswith("freq:"):
                    with contextlib.suppress(ValueError):
                        frequency = float(line.split(":", 1)[1].strip())
                elif "tx bitrate:" in line:
                    if match := re.search(r"([\d.]+)\s*MBit/s", line):
                        bitrate = float(match.group(1))
                elif (
                    line.startswith("signal:") and signal is None
                    and (match := re.search(r"(-?\d+)\s*dBm", line))
                ):
                    signal = float(match.group(1))
        if frequency:
            channel = self._channel_of(frequency)

        absent = Unavailable(Reason.NO_TOOL, "install iw for full Wi-Fi details")
        return WirelessInfo(
            ssid=ssid or (absent if not result.ok else Unavailable(
                Reason.NOT_EXPOSED, "not associated"
            )),
            bssid=bssid or absent,
            frequency_mhz=frequency or absent,
            channel=channel or absent,
            signal_dbm=signal if signal is not None else Unavailable(
                Reason.NOT_EXPOSED, "signal strength unavailable"
            ),
            quality_percent=self._signal_quality(signal),
            bitrate_mbps=bitrate or absent,
            security=security or Unavailable(
                Reason.NOT_EXPOSED, "security mode requires NetworkManager"
            ),
        )

    @staticmethod
    def _channel_of(frequency_mhz: float) -> int | None:
        """Convert a centre frequency to a Wi-Fi channel number."""
        freq = int(frequency_mhz)
        if 2412 <= freq <= 2484:
            return 14 if freq == 2484 else (freq - 2407) // 5
        if 5160 <= freq <= 5885:
            return (freq - 5000) // 5
        if 5955 <= freq <= 7115:  # 6 GHz (Wi-Fi 6E)
            return (freq - 5950) // 5
        return None

    @staticmethod
    def _signal_quality(signal_dbm: float | None):
        """Map signal strength in dBm onto a 0-100 quality figure.

        The mapping is linear between -90 dBm (unusable) and -30 dBm
        (excellent), which is the convention NetworkManager and most Wi-Fi
        indicators use.
        """
        if signal_dbm is None:
            return Unavailable(Reason.NOT_EXPOSED, "no signal reading")
        return max(0.0, min(100.0, 2 * (signal_dbm + 100)))

    # ------------------------------------------------------------------ routing

    def _read_default_route_v4(self) -> tuple[str | None, str | None]:
        """Default IPv4 gateway and its interface, from ``/proc/net/route``.

        The table stores addresses as little-endian hex words, so each must be
        unpacked rather than merely converted from hex.
        """
        for line in sysfs.read_lines(sysfs.PROC / "net/route")[1:]:
            fields = line.split()
            if len(fields) < 3:
                continue
            interface, destination, gateway = fields[0], fields[1], fields[2]
            if destination != "00000000":  # not the default route
                continue
            try:
                packed = struct.pack("<L", int(gateway, 16))
                return socket.inet_ntoa(packed), interface
            except (ValueError, struct.error, OSError):
                continue
        return None, None

    def _read_default_route_v6(self):
        """Default IPv6 gateway from ``/proc/net/ipv6_route``."""
        for line in sysfs.read_lines(sysfs.PROC / "net/ipv6_route"):
            fields = line.split()
            if len(fields) < 10:
                continue
            destination, prefix_len, next_hop = fields[0], fields[1], fields[4]
            if destination != "0" * 32 or prefix_len != "00":
                continue
            if next_hop == "0" * 32:
                continue
            try:
                address = ipaddress.IPv6Address(int(next_hop, 16))
            except (ValueError, ipaddress.AddressValueError):
                continue
            return str(address)
        return Unavailable(Reason.NOT_EXPOSED, "no default IPv6 route")

    def _read_dns(self) -> tuple[str, ...]:
        """Configured DNS resolvers.

        ``resolvectl`` is tried first because on a systemd-resolved system
        ``/etc/resolv.conf`` merely points at the local stub listener, which is
        true but useless to the user.
        """
        servers: list[str] = []
        if self.capabilities.resolvectl:
            result = run(["resolvectl", "dns"], timeout=3.0)
            if result.ok:
                for line in result.lines:
                    _, _, addresses = line.partition(":")
                    for token in addresses.split():
                        if self._is_address(token) and token not in servers:
                            servers.append(token)
        if not servers:
            for line in sysfs.read_lines("/etc/resolv.conf"):
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] not in servers:
                        servers.append(parts[1])
        return tuple(servers)

    @staticmethod
    def _is_address(token: str) -> bool:
        """True when ``token`` parses as an IP address."""
        try:
            ipaddress.ip_address(token)
        except ValueError:
            return False
        return True

    # -------------------------------------------------------------- connections

    def _refresh_connections(self, now: float) -> None:
        """Re-read the socket table on its own slower cadence."""
        age = now - self._connections_read_at
        if self._connections and age < self._connection_interval:
            return
        self._connections_read_at = now
        self._connections, self._protocols = self._read_connections()

    def _read_connections(self) -> tuple[tuple[Connection, ...], ProtocolStats]:
        """Enumerate sockets, attributing them to processes where permitted.

        psutil raises ``AccessDenied`` for sockets owned by other users unless
        running as root.  Rather than failing, the collector reports what it can
        see and the UI notes that the list is limited to the current user.
        """
        connections: list[Connection] = []
        tcp_states: dict[str, int] = {}
        udp_count = unix_count = 0
        try:
            entries = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            entries = self._own_connections()
        except (OSError, RuntimeError):
            entries = []

        names: dict[int, str] = {}
        for entry in entries:
            protocol = (
                "tcp" if entry.type == socket.SOCK_STREAM else "udp"
            ) + ("6" if entry.family == socket.AF_INET6 else "")
            status = entry.status if entry.status != "NONE" else "—"
            if protocol.startswith("tcp"):
                tcp_states[status] = tcp_states.get(status, 0) + 1
            else:
                udp_count += 1

            pid = entry.pid
            if pid and pid not in names:
                try:
                    names[pid] = psutil.Process(pid).name()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    names[pid] = "—"

            connections.append(
                Connection(
                    protocol=protocol,
                    local_address=entry.laddr.ip if entry.laddr else "*",
                    local_port=entry.laddr.port if entry.laddr else 0,
                    remote_address=entry.raddr.ip if entry.raddr else None,
                    remote_port=entry.raddr.port if entry.raddr else None,
                    status=status,
                    pid=pid,
                    process_name=names.get(pid) if pid else Unavailable(
                        Reason.PERMISSION, "socket owned by another user"
                    ),
                )
            )

        unix_count = self._count_unix_sockets()

        stats = ProtocolStats(
            tcp_established=tcp_states.get("ESTABLISHED", 0),
            tcp_listening=tcp_states.get("LISTEN", 0),
            tcp_time_wait=tcp_states.get("TIME_WAIT", 0),
            tcp_total=sum(tcp_states.values()),
            udp_total=udp_count,
            unix_total=unix_count,
            tcp_retransmits=self._read_retransmits(),
        )
        return tuple(connections), stats

    @staticmethod
    def _count_unix_sockets() -> int:
        """Count Unix domain sockets by reading ``/proc/net/unix``.

        ``psutil.net_connections(kind="unix")`` costs about 16 ms because it
        builds a full object per socket -- and a desktop has several hundred.
        Only the count is displayed, so counting lines is the proportionate
        approach.  The first line is a header.
        """
        lines = sysfs.read_lines(sysfs.PROC / "net/unix")
        return max(0, len(lines) - 1)

    @staticmethod
    def _own_connections() -> list[Any]:
        """Fall back to only this process's own sockets."""
        try:
            return list(psutil.Process().net_connections(kind="inet"))
        except (psutil.Error, OSError):
            return []

    @staticmethod
    def _read_retransmits():
        """Cumulative TCP retransmit count from ``/proc/net/snmp``."""
        lines = sysfs.read_lines(sysfs.PROC / "net/snmp")
        for index, line in enumerate(lines):
            if not line.startswith("Tcp:") or index + 1 >= len(lines):
                continue
            headers = line.split()
            values = lines[index + 1].split()
            if "RetransSegs" in headers and len(values) == len(headers):
                try:
                    return int(values[headers.index("RetransSegs")])
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------------- probes

    def ping(self, target: str, count: int = 4, timeout: float = 6.0) -> LatencyResult:
        """Measure round-trip latency to ``target``.

        This is the only method in the whole application that contacts a remote
        host.  It is never called automatically: the network page invokes it only
        when the user presses the button, and only if they have enabled latency
        probes in the privacy settings.
        """
        if not self.capabilities.ping:
            return LatencyResult(
                target, False, note=str(self.capabilities.ping.as_unavailable())
            )
        result = run(
            ["ping", "-n", "-c", str(count), "-w", str(int(timeout)), target],
            timeout=timeout + 2.0,
        )
        if not result.stdout:
            return LatencyResult(target, False, note="No response from ping")

        loss = None
        if match := re.search(r"([\d.]+)% packet loss", result.stdout):
            loss = float(match.group(1))
        # The summary line is "rtt min/avg/max/mdev = 1.2/3.4/5.6/0.7 ms".
        if match := re.search(
            r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", result.stdout
        ):
            return LatencyResult(
                target=target,
                reachable=True,
                min_ms=float(match.group(1)),
                avg_ms=float(match.group(2)),
                max_ms=float(match.group(3)),
                stddev_ms=float(match.group(4)),
                packet_loss_percent=loss,
            )
        return LatencyResult(
            target=target,
            reachable=False,
            packet_loss_percent=loss if loss is not None else 100.0,
            note="Host did not respond",
        )
