"""Tests for the container, configuration, persistence and metric extraction."""

from __future__ import annotations

import time

import pytest
from app.core.container import Container
from app.core.errors import DependencyError
from app.database.repositories import MetricRepository
from app.services.metrics import MetricRegistry


class TestContainer:
    """The dependency-injection container."""

    def test_resolves_a_singleton_once(self) -> None:
        """A singleton factory runs at most once."""
        container = Container()
        calls = []

        class Service:
            pass

        container.register_singleton(
            Service, lambda _c: (calls.append(1), Service())[1]
        )
        first = container.resolve(Service)
        second = container.resolve(Service)
        assert first is second
        assert len(calls) == 1

    def test_transients_are_fresh_each_time(self) -> None:
        """A transient factory runs per resolution."""
        container = Container()

        class Service:
            pass

        container.register_transient(Service, lambda _c: Service())
        assert container.resolve(Service) is not container.resolve(Service)

    def test_unregistered_raises(self) -> None:
        """Resolving an unknown type is a programming error, not a silent None."""
        class Missing:
            pass

        with pytest.raises(DependencyError, match="not registered"):
            Container().resolve(Missing)

    def test_try_resolve_returns_none(self) -> None:
        """The optional form is available where absence is acceptable."""
        class Missing:
            pass

        assert Container().try_resolve(Missing) is None

    def test_detects_circular_dependencies(self) -> None:
        """A cycle is reported clearly instead of overflowing the stack."""
        container = Container()

        class A:
            pass

        class B:
            pass

        container.register_singleton(A, lambda c: c.resolve(B))
        container.register_singleton(B, lambda c: c.resolve(A))
        with pytest.raises(DependencyError, match="Circular"):
            container.resolve(A)

    def test_dispose_closes_services(self) -> None:
        """Shutdown calls close() on everything that defines it."""
        container = Container()
        closed = []

        class Service:
            def close(self) -> None:
                closed.append(True)

        container.register_singleton(Service, lambda _c: Service())
        container.resolve(Service)
        container.dispose()
        assert closed == [True]

    def test_dispose_survives_a_failing_close(self) -> None:
        """One broken shutdown must not prevent the others."""
        container = Container()
        closed = []

        class Bad:
            def close(self) -> None:
                raise RuntimeError("boom")

        class Good:
            def close(self) -> None:
                closed.append(True)

        container.register_singleton(Good, lambda _c: Good())
        container.register_singleton(Bad, lambda _c: Bad())
        container.resolve(Good)
        container.resolve(Bad)
        container.dispose()
        assert closed == [True]


class TestConfig:
    """Settings storage."""

    def test_round_trips_through_disk(self, config, temp_dir) -> None:
        """Settings survive a restart."""
        from app.core.config import ConfigService

        config.set("appearance.theme", "light")
        config.set("sampling.cpu", 2.5)
        config.set("ui_state.custom_key", {"nested": [1, 2, 3]})

        reloaded = ConfigService(temp_dir / "settings.json")
        assert reloaded.get("appearance.theme") == "light"
        assert reloaded.get("sampling.cpu") == 2.5
        assert reloaded.get("ui_state.custom_key") == {"nested": [1, 2, 3]}

    def test_nested_dataclasses_are_reconstructed(self, config, temp_dir) -> None:
        """Reloaded settings are typed objects, not raw dictionaries."""
        from app.core.config import ChartSettings, ConfigService

        config.set("charts.history_seconds", 600)
        reloaded = ConfigService(temp_dir / "settings.json")
        assert isinstance(reloaded.settings.charts, ChartSettings)
        assert reloaded.settings.charts.history_seconds == 600

    def test_notifies_listeners(self, config) -> None:
        """Changes are broadcast so services can react live."""
        seen = []
        config.subscribe(lambda key, value: seen.append((key, value)))
        config.set("appearance.theme", "midnight")
        assert ("appearance.theme", "midnight") in seen

    def test_unchanged_value_does_not_notify(self, config) -> None:
        """Setting the same value is a no-op, preventing feedback loops."""
        config.set("appearance.theme", "dark")
        seen = []
        config.subscribe(lambda key, value: seen.append(key))
        config.set("appearance.theme", "dark")
        assert seen == []

    def test_unsubscribe_stops_notifications(self, config) -> None:
        """The returned callable detaches the listener."""
        seen = []
        unsubscribe = config.subscribe(lambda key, value: seen.append(key))
        unsubscribe()
        config.set("appearance.theme", "light")
        assert seen == []

    def test_a_failing_listener_does_not_block_others(self, config) -> None:
        """One bad subscriber cannot break settings propagation."""
        seen = []

        def bad(key, value):
            raise RuntimeError("boom")

        config.subscribe(bad)
        config.subscribe(lambda key, value: seen.append(key))
        config.set("appearance.theme", "light")
        assert seen == ["appearance.theme"]

    def test_corrupt_file_falls_back_to_defaults(self, temp_dir) -> None:
        """A malformed settings file must not prevent start-up."""
        from app.core.config import ConfigService

        path = temp_dir / "broken.json"
        path.write_text("{ this is not json")
        assert ConfigService(path).get("appearance.theme") == "dark"

    def test_unknown_keys_are_ignored(self, config) -> None:
        """Writing an unknown key is refused rather than silently stored."""
        config.set("nonsense.path.here", 1)
        assert config.get("nonsense.path.here") is None

    def test_get_returns_default_for_missing(self, config) -> None:
        """Reading an unknown key yields the supplied default."""
        assert config.get("does.not.exist", "fallback") == "fallback"


class TestMetricRepository:
    """History persistence."""

    def test_batch_write_and_read_back(self, database) -> None:
        """Samples round-trip through SQLite."""
        repository = MetricRepository(database)
        now = time.time()
        written = repository.record_batch(
            [(now - index, "cpu.usage", float(index)) for index in range(50)]
        )
        assert written == 50
        assert repository.count() == 50
        assert len(repository.series("cpu.usage")) == 50

    def test_series_respects_the_time_window(self, database) -> None:
        """Only samples inside the window are returned."""
        repository = MetricRepository(database)
        now = time.time()
        repository.record_batch([
            (now - 1000, "m", 1.0),
            (now - 10, "m", 2.0),
        ])
        recent = repository.series("m", since=now - 100)
        assert [point.value for point in recent] == [2.0]

    def test_summary_statistics(self, database) -> None:
        """Aggregates are computed in SQL, not in Python."""
        repository = MetricRepository(database)
        now = time.time()
        repository.record_batch([(now, "m", v) for v in (10.0, 20.0, 30.0)])
        summary = repository.summary("m")
        assert summary["minimum"] == 10.0
        assert summary["maximum"] == 30.0
        assert summary["mean"] == 20.0

    def test_prune_removes_old_samples(self, database) -> None:
        """Retention deletes beyond the window and keeps the rest."""
        repository = MetricRepository(database)
        now = time.time()
        repository.record_batch([
            (now - 86_400 * 30, "m", 1.0),
            (now, "m", 2.0),
        ])
        removed = repository.prune(older_than_days=7)
        assert removed == 1
        assert repository.count() == 1

    def test_degraded_database_accepts_writes_silently(self) -> None:
        """An unusable database disables history rather than crashing."""
        from app.database.connection import Database

        broken = Database("/proc/nonexistent-directory/history.db")
        assert broken.degraded
        assert MetricRepository(broken).record_batch([(1.0, "m", 1.0)]) == 0
        assert MetricRepository(broken).series("m") == []


class TestMetricRegistry:
    """Metric extraction."""

    def test_every_metric_has_a_unique_dotted_name(self) -> None:
        """Names are database keys, so collisions would corrupt history."""
        registry = MetricRegistry()
        names = registry.names()
        assert len(names) == len(set(names))
        for name in names:
            assert "." in name, name

    def test_extraction_omits_unavailable_readings(self) -> None:
        """An absent metric is left out, never recorded as zero.

        Persisting a fabricated zero would poison every average and chart drawn
        from the history database.
        """
        from app.models.base import Reason, Unavailable
        from app.models.cpu import CpuSnapshot

        registry = MetricRegistry()
        snapshot = CpuSnapshot(
            usage=42.0,
            temperature=Unavailable(Reason.NO_HARDWARE),
            package_power_w=Unavailable(Reason.PERMISSION),
        )
        values = registry.extract("cpu", snapshot)
        assert values["cpu.usage"] == 42.0
        assert "cpu.temperature" not in values
        assert "cpu.power" not in values

    def test_extraction_tolerates_a_mismatched_snapshot(self) -> None:
        """A wrong snapshot type yields nothing rather than raising."""
        registry = MetricRegistry()
        assert registry.extract("cpu", object()) == {}

    def test_every_domain_is_covered(self) -> None:
        """Each collector domain contributes at least one metric."""
        registry = MetricRegistry()
        domains = {definition.domain for definition in registry.all()}
        expected = {
            "cpu", "memory", "gpu", "storage", "network",
            "processes", "sensors", "battery", "services",
        }
        assert expected <= domains
