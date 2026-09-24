"""Safe, cached helpers for invoking external command-line tools.

Design notes
------------
* Every call is time-limited.  A hung ``smartctl`` on a failing disk must never
  stall the monitoring pipeline.
* ``which`` results are memoised: probing the PATH on every poll is wasteful and
  the answer effectively never changes during a session.
* Nothing here ever runs through a shell, and arguments are always passed as a
  list, so no quoting or injection concerns arise.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 4.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of an external command invocation."""

    ok: bool
    stdout: str
    stderr: str
    returncode: int

    @property
    def lines(self) -> list[str]:
        """Non-empty output lines, stripped."""
        return [ln.strip() for ln in self.stdout.splitlines() if ln.strip()]


FAILED = CommandResult(False, "", "", -1)


@lru_cache(maxsize=256)
def which(program: str) -> str | None:
    """Memoised :func:`shutil.which`."""
    return shutil.which(program)


def has_tool(program: str) -> bool:
    """True when ``program`` is present on ``PATH``."""
    return which(program) is not None


def run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check_tool: bool = True,
) -> CommandResult:
    """Execute ``argv`` and capture its output, never raising on failure.

    Parameters
    ----------
    argv:
        Program and arguments.  Never passed through a shell.
    timeout:
        Hard wall-clock limit in seconds.
    check_tool:
        When true, return :data:`FAILED` immediately if the program is absent,
        skipping the process-spawn cost.

    Returns
    -------
    CommandResult
        ``ok`` is true only when the process exited zero within the timeout.
    """
    if not argv:
        return FAILED
    if check_tool and not has_tool(argv[0]):
        return FAILED
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning("Command timed out after %.1fs: %s", timeout, " ".join(argv))
        return CommandResult(False, "", "timed out", -1)
    except (OSError, ValueError) as exc:
        _log.debug("Command failed to start: %s (%s)", " ".join(argv), exc)
        return FAILED
    return CommandResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )
