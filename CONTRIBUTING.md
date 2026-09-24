# Contributing

Contributions are welcome. This document covers what the project expects.

## Before you start

For anything larger than a bug fix, open an issue first describing the problem.
It is easier to agree an approach before code exists than after.

## Development setup

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) walks through adding a collector,
metric, page, widget, theme or diagnostic check.

## Standards

Before opening a pull request:

```bash
.venv/bin/python -m pytest      # all tests pass
.venv/bin/ruff check app tests  # no lint errors
.venv/bin/mypy app              # no new type errors
```

Code should carry type hints on public signatures, docstrings on modules,
classes and public methods, and comments that explain *why* rather than what.

## Project-specific rules

These are not stylistic preferences; they are what makes the application behave
correctly on hardware the author has never seen.

**Never fabricate a value.** If a metric cannot be read, return
`Unavailable(reason, detail)`. Returning `0` makes a missing sensor
indistinguishable from an idle one, and the zero will be persisted into the
history database as though it were real.

**Never assume a tool exists.** Probe for it through `CapabilityRegistry` and
degrade with an explanation. The application must be fully usable on a minimal
system with none of the optional packages installed.

**Never block the GUI thread.** Anything touching a subprocess, a device or the
network belongs on a worker. A `QRunnable` with a signal for the result is the
established pattern here; see `_ProbeTask` in `ui/pages/network.py`.

**Never run a shell.** Subprocesses take an argument list and a hard timeout.
Any user-influenced value reaching a command line must be validated against an
allow-list first — see `ServiceCollector.control`.

**Read thresholds from the hardware.** A temperature limit belongs to the
device, not to a constant in the source.

## Testing expectations

New collectors should be added to the parametrised contract tests in
`tests/test_collectors.py`. New logic with branches needs unit tests. Anything
time-dependent should be driven by a controlled clock rather than `sleep`.

Tests must pass on a machine with no GPU, no battery and no `systemd` — that is
precisely the configuration most likely to expose a broken assumption.

## Commit messages

Explain the reasoning, not just the change:

```
Cache the power profile for 60 seconds

powerprofilesctl is a Python D-Bus script costing ~93 ms per call, which
was more than the rest of the battery sample combined. The profile only
changes when the user changes it.
```

## Reporting bugs

Please include:

- Distribution and kernel (`uname -a`)
- Python and PySide6 versions
- The Diagnostics page export (Export Markdown), which captures the capability
  probe and every collector's state
- Relevant lines from `~/.local/state/linux-system-observatory/logs/`

## Licence

Contributions are accepted under the MIT licence, matching the project.
