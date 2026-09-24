"""A deliberately small dependency-injection container.

Why not a framework?
--------------------
The application needs three things from DI: a single place that wires object
graphs, constructor injection so collaborators can be replaced in tests, and
lazy singletons so start-up cost is paid only for what is actually used.  A
75-line container delivers all three without adding a dependency or hiding
control flow behind decorators and metaclasses.

Services are registered by their *type*, which keeps look-ups type-checkable::

    container.register_singleton(HistoryService, lambda c: HistoryService(
        c.resolve(Database)
    ))
    history = container.resolve(HistoryService)   # inferred as HistoryService
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TypeVar, cast

from app.core.errors import DependencyError

_log = logging.getLogger(__name__)

T = TypeVar("T")

#: A factory receives the container so it can resolve its own dependencies.
Factory = Callable[["Container"], T]


class Container:
    """Registry of lazily-constructed, type-keyed services.

    Thread-safe: a re-entrant lock guards instantiation so two worker threads
    resolving the same singleton concurrently cannot build it twice.
    """

    def __init__(self) -> None:
        self._factories: dict[type, Factory] = {}
        self._singletons: dict[type, object] = {}
        self._transients: set[type] = set()
        self._lock = threading.RLock()
        self._resolving: set[type] = set()

    # ------------------------------------------------------------ registration

    def register_singleton(self, key: type[T], factory: Factory[T]) -> None:
        """Register ``key`` to be built once, on first resolution."""
        with self._lock:
            self._factories[key] = factory
            self._transients.discard(key)

    def register_transient(self, key: type[T], factory: Factory[T]) -> None:
        """Register ``key`` to be built afresh on every resolution."""
        with self._lock:
            self._factories[key] = factory
            self._transients.add(key)

    def register_instance(self, key: type[T], instance: T) -> None:
        """Register an already-constructed object (useful for test doubles)."""
        with self._lock:
            self._singletons[key] = instance
            self._factories.pop(key, None)

    # -------------------------------------------------------------- resolution

    def resolve(self, key: type[T]) -> T:
        """Return the service registered for ``key``.

        Raises
        ------
        DependencyError
            If ``key`` was never registered, or if a circular dependency is
            detected while constructing it.
        """
        with self._lock:
            if key in self._singletons:
                return cast("T", self._singletons[key])

            factory = self._factories.get(key)
            if factory is None:
                raise DependencyError(
                    f"{key.__name__} is not registered in the container"
                )

            if key in self._resolving:
                chain = " -> ".join(k.__name__ for k in self._resolving)
                raise DependencyError(f"Circular dependency: {chain} -> {key.__name__}")

            self._resolving.add(key)
            try:
                instance = factory(self)
            finally:
                self._resolving.discard(key)

            if key not in self._transients:
                self._singletons[key] = instance
                _log.debug("Instantiated singleton %s", key.__name__)
            return cast("T", instance)

    def try_resolve(self, key: type[T]) -> T | None:
        """Resolve ``key``, or return ``None`` if it is not registered."""
        try:
            return self.resolve(key)
        except DependencyError:
            return None

    def has(self, key: type) -> bool:
        """True when ``key`` can be resolved."""
        return key in self._singletons or key in self._factories

    # ---------------------------------------------------------------- lifecycle

    def dispose(self) -> None:
        """Call ``close()`` on every instantiated singleton that defines it.

        Disposal runs in reverse instantiation order so that dependents shut
        down before the services they depend on.
        """
        with self._lock:
            for instance in reversed(list(self._singletons.values())):
                closer = getattr(instance, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        _log.exception("Error disposing %s", type(instance).__name__)
            self._singletons.clear()
