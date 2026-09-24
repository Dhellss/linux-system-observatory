"""SQLite schema and migrations.

Design decisions
----------------
**A narrow metrics table, not a wide one.**  Metrics are stored as
``(timestamp, metric, value)`` triples rather than one column per metric.  A wide
table would need a migration every time a metric is added, and would be mostly
NULL on systems lacking a GPU or battery.  The narrow shape also lets the
retention sweeper delete by time with a single index scan.

**WAL journalling.**  The UI reads history while a background thread writes
samples.  Write-Ahead Logging lets those happen concurrently instead of the
reader blocking the writer, which in rollback-journal mode would stall the
interface.

**Explicit schema version.**  ``user_version`` drives forward-only migrations, so
an upgrade never silently discards a user's history.
"""

from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)

#: Current schema version.  Increment and add a migration when changing tables.
SCHEMA_VERSION = 1

_INITIAL_SCHEMA = """
-- One row per metric sample.  Kept deliberately narrow; see module docstring.
CREATE TABLE IF NOT EXISTS metrics (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded  REAL    NOT NULL,         -- Unix timestamp, seconds
    metric    TEXT    NOT NULL,         -- dotted path, e.g. 'cpu.usage'
    value     REAL    NOT NULL
);

-- Retention sweeps and chart queries both filter on time first, then metric.
CREATE INDEX IF NOT EXISTS idx_metrics_recorded ON metrics (recorded);
CREATE INDEX IF NOT EXISTS idx_metrics_metric_time ON metrics (metric, recorded);

-- A named recording session, so a user can compare "before" and "after".
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    started     REAL    NOT NULL,
    ended       REAL,
    note        TEXT    NOT NULL DEFAULT ''
);

-- Alert firings, retained so the user can see what happened while away.
CREATE TABLE IF NOT EXISTS alert_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred   REAL    NOT NULL,
    rule_id    TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    metric     TEXT    NOT NULL,
    value      REAL    NOT NULL,
    threshold  REAL    NOT NULL,
    severity   INTEGER NOT NULL,
    active     INTEGER NOT NULL          -- 1 = fired, 0 = cleared
);

CREATE INDEX IF NOT EXISTS idx_alert_events_time ON alert_events (occurred);

-- Diagnostic runs, so health can be tracked over time.
CREATE TABLE IF NOT EXISTS diagnostic_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    generated  REAL    NOT NULL,
    worst      INTEGER NOT NULL,
    summary    TEXT    NOT NULL
);
"""


def initialise(connection: sqlite3.Connection) -> None:
    """Create or migrate the schema on ``connection``."""
    _configure_pragmas(connection)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        _log.info("Creating metrics database schema v%d", SCHEMA_VERSION)
        connection.executescript(_INITIAL_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
        return
    if version < SCHEMA_VERSION:
        _migrate(connection, version)
    elif version > SCHEMA_VERSION:
        # A newer build wrote this file.  Refusing to touch it is safer than
        # guessing; the caller degrades to in-memory history.
        raise sqlite3.DatabaseError(
            f"Database schema v{version} is newer than this build supports "
            f"(v{SCHEMA_VERSION})"
        )


def _configure_pragmas(connection: sqlite3.Connection) -> None:
    """Apply the connection-level settings the access pattern needs."""
    # WAL lets the UI read history while the writer thread appends samples.
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL trades a theoretical loss of the last transaction on power failure
    # for far fewer fsyncs.  Losing a second of performance history is
    # inconsequential; stalling the UI on every flush is not.
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    # Cap the page cache so a long session cannot balloon resident memory.
    connection.execute("PRAGMA cache_size = -8000")  # ~8 MB
    connection.execute("PRAGMA busy_timeout = 4000")


def _migrate(connection: sqlite3.Connection, from_version: int) -> None:
    """Apply forward migrations from ``from_version`` to the current version.

    Each migration is a separate, committed step so that a failure part-way
    leaves the database at a known version rather than in a half-migrated state.
    """
    _log.info("Migrating database from v%d to v%d", from_version, SCHEMA_VERSION)
    # No migrations exist yet; the structure is here so that adding the first one
    # requires no rethinking of the surrounding machinery.
    migrations: dict[int, str] = {}
    for version in range(from_version + 1, SCHEMA_VERSION + 1):
        script = migrations.get(version)
        if script:
            connection.executescript(script)
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
