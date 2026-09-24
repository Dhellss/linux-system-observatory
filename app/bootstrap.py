"""Composition root.

This is the only module that knows how the whole object graph fits together.
Everything else receives its collaborators through constructor parameters, which
is what keeps the layers independently testable: a collector can be exercised
without Qt, a service without a window, a page without a running system.

Read this file to understand the application's structure -- the wiring below is
the architecture, stated once, in dependency order.
"""

from __future__ import annotations

import logging

from app.collectors.base import Collector
from app.collectors.battery import BatteryCollector
from app.collectors.cpu import CpuCollector
from app.collectors.gpu import GpuCollector
from app.collectors.memory import MemoryCollector
from app.collectors.network import NetworkCollector
from app.collectors.process import ProcessCollector
from app.collectors.sensors import SensorCollector
from app.collectors.services import ServiceCollector
from app.collectors.storage import StorageCollector
from app.collectors.system import SystemCollector
from app.core.capabilities import CapabilityRegistry
from app.core.config import ConfigService
from app.core.container import Container
from app.database.connection import Database
from app.database.repositories import AlertRepository
from app.services.alerts import AlertEngine
from app.services.diagnostics import DiagnosticsService
from app.services.export import ExportService
from app.services.history import HistoryService
from app.services.metrics import MetricRegistry
from app.services.monitor import MonitorService
from app.services.notifications import NotificationService
from app.utils.hwmon import HwmonProvider

_log = logging.getLogger(__name__)

#: Every collector the application registers, in start-up order.
COLLECTOR_CLASSES: tuple[type[Collector], ...] = (
    CpuCollector,
    MemoryCollector,
    GpuCollector,
    StorageCollector,
    NetworkCollector,
    ProcessCollector,
    SensorCollector,
    BatteryCollector,
    SystemCollector,
    ServiceCollector,
)


def build_container(*, database_path: str | None = None) -> Container:
    """Construct and wire every service.

    Parameters
    ----------
    database_path:
        Override for the history database, used by tests to keep runs isolated.

    Returns
    -------
    Container
        A container with every service registered as a lazy singleton.
    """
    container = Container()

    # --- Layer 1: environment facts, with no dependencies of their own. -------
    container.register_singleton(CapabilityRegistry, lambda _c: CapabilityRegistry())
    container.register_singleton(ConfigService, lambda _c: ConfigService())
    container.register_singleton(HwmonProvider, lambda _c: HwmonProvider())
    container.register_singleton(MetricRegistry, lambda _c: MetricRegistry())
    container.register_singleton(
        Database, lambda _c: Database(database_path)
    )

    # --- Layer 2: services built from layer 1. -------------------------------
    container.register_singleton(
        AlertRepository, lambda c: AlertRepository(c.resolve(Database))
    )
    container.register_singleton(
        MonitorService, lambda c: MonitorService(c.resolve(ConfigService))
    )
    container.register_singleton(
        HistoryService,
        lambda c: HistoryService(
            c.resolve(ConfigService),
            c.resolve(Database),
            c.resolve(MetricRegistry),
        ),
    )
    container.register_singleton(
        AlertEngine,
        lambda c: AlertEngine(
            c.resolve(ConfigService),
            c.resolve(AlertRepository),
            c.resolve(MetricRegistry),
        ),
    )
    container.register_singleton(
        DiagnosticsService,
        lambda c: DiagnosticsService(c.resolve(CapabilityRegistry)),
    )
    container.register_singleton(
        NotificationService,
        lambda c: NotificationService(c.resolve(ConfigService)),
    )
    container.register_singleton(
        ExportService,
        lambda c: ExportService(
            c.resolve(HistoryService), c.resolve(DiagnosticsService)
        ),
    )
    return container


def register_collectors(container: Container) -> None:
    """Instantiate every collector and add it to the monitoring schedule.

    Collectors share one :class:`HwmonProvider` so that domains sampling in the
    same window pay for a single sensor scan between them.
    """
    monitor = container.resolve(MonitorService)
    capabilities = container.resolve(CapabilityRegistry)
    hwmon = container.resolve(HwmonProvider)

    for collector_class in COLLECTOR_CLASSES:
        try:
            monitor.register(collector_class(capabilities, hwmon))
        except Exception:
            _log.exception(
                "Could not register the %s collector; continuing without it",
                collector_class.domain,
            )


def wire_cross_domain(container: Container) -> None:
    """Connect the few places where one domain enriches another.

    Only one such link exists today: GPU memory per process, which the process
    table displays.  It is pushed from the GPU snapshot into the process
    collector rather than pulled, so the process collector keeps no dependency on
    the GPU layer and remains testable on a machine with no graphics adapter.
    """
    monitor = container.resolve(MonitorService)
    processes = monitor.collector_of("processes", ProcessCollector)
    if processes is None:
        return

    def on_snapshot(domain: str, snapshot: object) -> None:
        if domain != "gpu":
            return
        mapping: dict[int, int] = {}
        for gpu in getattr(snapshot, "gpus", ()):
            for process in gpu.processes:
                if isinstance(process.used_vram_bytes, int):
                    mapping[process.pid] = process.used_vram_bytes
        processes.set_gpu_memory(mapping)

    monitor.snapshot_ready.connect(on_snapshot)


def build_page_context(container: Container, theme):
    """Assemble the :class:`~app.ui.pages.base.PageContext` handed to every page."""
    from app.ui.pages.base import PageContext

    return PageContext(
        theme=theme,
        config=container.resolve(ConfigService),
        capabilities=container.resolve(CapabilityRegistry),
        monitor=container.resolve(MonitorService),
        history=container.resolve(HistoryService),
        alerts=container.resolve(AlertEngine),
        diagnostics=container.resolve(DiagnosticsService),
        export=container.resolve(ExportService),
        notifications=container.resolve(NotificationService),
        metrics=container.resolve(MetricRegistry),
    )
