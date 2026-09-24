"""Network interface, connection and probe domain models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


class InterfaceKind(enum.Enum):
    """Interface class, inferred from sysfs attributes and naming."""

    ETHERNET = "Ethernet"
    WIRELESS = "Wi-Fi"
    LOOPBACK = "Loopback"
    BRIDGE = "Bridge"
    VIRTUAL = "Virtual"
    TUNNEL = "VPN / Tunnel"
    BLUETOOTH = "Bluetooth"
    CELLULAR = "Cellular"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class Address:
    """One configured address on an interface."""

    family: str          # "IPv4" | "IPv6" | "MAC"
    address: str
    netmask: Maybe[str] = None
    broadcast: Maybe[str] = None
    scope: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class WirelessInfo:
    """Wi-Fi radio details, present only on wireless interfaces."""

    ssid: Maybe[str] = None
    bssid: Maybe[str] = None
    frequency_mhz: Maybe[float] = None
    channel: Maybe[int] = None
    signal_dbm: Maybe[float] = None
    #: Derived 0-100 quality figure; -30 dBm is excellent, -90 unusable.
    quality_percent: Maybe[float] = None
    bitrate_mbps: Maybe[float] = None
    security: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class InterfaceCounters:
    """Rate-converted traffic counters for one interface."""

    download_bytes_per_s: float = 0.0
    upload_bytes_per_s: float = 0.0
    rx_packets_per_s: float = 0.0
    tx_packets_per_s: float = 0.0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    collisions: int = 0
    total_rx_bytes: int = 0
    total_tx_bytes: int = 0

    @property
    def total_bytes_per_s(self) -> float:
        """Combined throughput in both directions."""
        return self.download_bytes_per_s + self.upload_bytes_per_s

    @property
    def error_count(self) -> int:
        """Total error and drop events since boot."""
        return self.rx_errors + self.tx_errors + self.rx_dropped + self.tx_dropped


@dataclass(frozen=True, slots=True)
class Interface:
    """A network interface and its current state."""

    name: str
    kind: InterfaceKind
    is_up: bool
    addresses: tuple[Address, ...] = ()
    mac: Maybe[str] = None
    mtu: Maybe[int] = None
    speed_mbps: Maybe[int] = None
    duplex: Maybe[str] = None
    driver: Maybe[str] = None
    counters: InterfaceCounters = field(default_factory=InterfaceCounters)
    wireless: Maybe[WirelessInfo] = None
    #: True for the interface carrying the default route.
    is_default: bool = False

    @property
    def ipv4(self) -> str | None:
        """First IPv4 address, if any."""
        return next((a.address for a in self.addresses if a.family == "IPv4"), None)

    @property
    def ipv6(self) -> str | None:
        """First globally-scoped IPv6 address, if any."""
        return next(
            (a.address for a in self.addresses
             if a.family == "IPv6" and not a.address.startswith("fe80")),
            None,
        )


@dataclass(frozen=True, slots=True)
class Connection:
    """One socket from the kernel's connection table."""

    protocol: str          # "tcp" | "tcp6" | "udp" | "udp6" | "unix"
    local_address: str
    local_port: int
    remote_address: Maybe[str]
    remote_port: Maybe[int]
    status: str
    pid: Maybe[int] = None
    process_name: Maybe[str] = None

    @property
    def is_listening(self) -> bool:
        """True for a server socket awaiting connections."""
        return self.status == "LISTEN"


@dataclass(frozen=True, slots=True)
class ProtocolStats:
    """Aggregate socket counts by protocol and state."""

    tcp_established: int = 0
    tcp_listening: int = 0
    tcp_time_wait: int = 0
    tcp_total: int = 0
    udp_total: int = 0
    unix_total: int = 0
    tcp_retransmits: Maybe[int] = None


@dataclass(frozen=True, slots=True)
class LatencyResult:
    """Outcome of a manually-triggered ICMP probe.

    Network probes are the only feature that contacts a remote host, so they are
    opt-in, on demand, and their target is user-configurable.
    """

    target: str
    reachable: bool
    min_ms: Maybe[float] = None
    avg_ms: Maybe[float] = None
    max_ms: Maybe[float] = None
    stddev_ms: Maybe[float] = None
    packet_loss_percent: Maybe[float] = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class NetworkSnapshot(Snapshot):
    """One complete network sample."""

    interfaces: tuple[Interface, ...] = ()
    totals: InterfaceCounters = field(default_factory=InterfaceCounters)
    connections: tuple[Connection, ...] = ()
    protocols: ProtocolStats = field(default_factory=ProtocolStats)
    gateway_v4: Maybe[str] = None
    gateway_v6: Maybe[str] = None
    dns_servers: tuple[str, ...] = ()
    hostname: Maybe[str] = None
    #: True when at least one non-loopback interface is up with an address.
    online: bool = False

    @property
    def default_interface(self) -> Interface | None:
        """The interface carrying the default route."""
        return next((i for i in self.interfaces if i.is_default), None)

    @property
    def active(self) -> list[Interface]:
        """Up, non-loopback interfaces, busiest first."""
        return sorted(
            (i for i in self.interfaces
             if i.is_up and i.kind is not InterfaceKind.LOOPBACK),
            key=lambda i: i.counters.total_bytes_per_s,
            reverse=True,
        )
