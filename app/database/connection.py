"""SQLite connection management.

SQLite connections are not safe to share across threads, and the application has
at least two writers in play (the history flusher and the alert recorder) plus UI
readers.  Rather than serialising everything through one lock, each thread gets
its own connection via :class:`threading.local`.  SQLite handles the
cross-connection coordination itself, which in WAL mode means readers never block
the writer.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.paths import database_file
from app.database.schema import initialise

_log = logging.getLogger(__name__)


class Database:
    """Thread-local SQLite connection factory.

    Set ``path`` to ``":memory:"`` in tests for an isolated, fast database.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = str(path) if path is not None else str(database_file())
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._degraded = False
        self._initialise()

    @property
    def path(self) -> str:
        """Filesystem location of the database."""
        return self._path

    @property
    def degraded(self) -> bool:
        """True when the database is unusable and history must be skipped.

        A monitoring tool must still run when its history store cannot be opened
        -- a read-only home directory, a full disk, a schema from the future.
        Callers check this instead of handling exceptions at every call site.
        """
        return self._degraded

    def _initialise(self) -> None:
        """Open the first connection and create or migrate the schema."""
        try:
            initialise(self.connection())
        except sqlite3.Error as exc:
            _log.error("History database unavailable (%s); disabling persistence", exc)
            self._degraded = True

    def connection(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use."""
        existing: sqlite3.Connection | None = getattr(
            self._local, "connection", None
        )
        if existing is not None:
            return existing
        connection = sqlite3.connect(
            self._path,
            timeout=5.0,
            # Each thread has its own connection, so SQLite's own thread check
            # is redundant; disabling it avoids a spurious error when a Qt
            # thread-pool thread is reused for a different task.
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        if self._connections:
            # Pragmas are per-connection, so every new one needs them too.
            from app.database.schema import _configure_pragmas

            _configure_pragmas(connection)
        self._local.connection = connection
        with self._lock:
            self._connections.append(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block in a transaction, committing on success.

        Yields nothing useful when the database is degraded, but still yields a
        connection object so callers need no special-casing; writes simply go
        nowhere.
        """
        connection = self.connection()
        try:
            yield connection
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            _log.exception("Database transaction rolled back")
            raise

    def execute(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        """Run a read query and return all rows, or an empty list on failure."""
        if self._degraded:
            return []
        try:
            return self.connection().execute(sql, parameters).fetchall()
        except sqlite3.Error:
            _log.exception("Query failed: %s", sql.split("\n")[0])
            return []

    def size_bytes(self) -> int:
        """Current on-disk size of the database and its WAL."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(self._path + suffix)
            try:
                if candidate.exists():
                    total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def vacuum(self) -> None:
        """Reclaim free pages after a large prune.

        Deleting rows in SQLite marks pages free but does not shrink the file, so
        an explicit vacuum is needed after retention sweeps to honour the user's
        configured size limit.
        """
        if self._degraded:
            return
        try:
            self.connection().execute("VACUUM")
        except sqlite3.Error:
            _log.warning("VACUUM failed; database file will not shrink")

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the main database file."""
        if self._degraded:
            return
        try:
            self.connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            _log.debug("WAL checkpoint failed", exc_info=True)

    def close(self) -> None:
        """Checkpoint and close every connection opened by any thread."""
        self.checkpoint()
        with self._lock:
            for connection in self._connections:
                with contextlib.suppress(sqlite3.Error):
                    connection.close()
            self._connections.clear()
        self._local = threading.local()
