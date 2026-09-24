"""Shared pytest fixtures.

The data layer is deliberately free of Qt, so almost every test here runs
headless with no display server and no event loop.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.capabilities import CapabilityRegistry
from app.core.config import ConfigService
from app.database.connection import Database
from app.utils.hwmon import HwmonProvider


@pytest.fixture
def temp_dir() -> Path:
    """An isolated temporary directory."""
    return Path(tempfile.mkdtemp())


@pytest.fixture
def config(temp_dir: Path) -> ConfigService:
    """A settings service backed by a throwaway file."""
    return ConfigService(temp_dir / "settings.json")


@pytest.fixture
def database(temp_dir: Path) -> Database:
    """A throwaway metrics database."""
    db = Database(temp_dir / "history.db")
    yield db
    db.close()


@pytest.fixture(scope="session")
def capabilities() -> CapabilityRegistry:
    """The real capability registry for this machine.

    Probing is cached per process, so sharing one instance across the session
    keeps the suite fast.
    """
    return CapabilityRegistry()


@pytest.fixture(scope="session")
def hwmon() -> HwmonProvider:
    """A shared sensor provider."""
    return HwmonProvider()
