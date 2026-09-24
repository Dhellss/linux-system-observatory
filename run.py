#!/usr/bin/env python3
"""Development launcher.

Adds the project root to ``sys.path`` so the application runs from a checkout
without installation.  An installed copy uses the ``observatory`` console script
defined in ``pyproject.toml`` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
