# Architecture

This document explains *why* the code is shaped the way it is. For how to
extend it, see [DEVELOPMENT.md](DEVELOPMENT.md).

---

## 1. Layering

```
        ┌──────────────────────────────────────────┐
        │  ui/        pages, widgets, themes       │  Qt lives only here
        ├──────────────────────────────────────────┤
        │  services/  monitor, history, alerts,    │  Qt for signals/timers
        │             diagnostics, export          │
        ├──────────────────────────────────────────┤
        │  collectors/  one per domain             │  no Qt
        ├──────────────────────────────────────────┤
        │  models/    frozen snapshot dataclasses  │  no Qt, no I/O
        ├──────────────────────────────────────────┤
        │  core/      capabilities, DI, config,    │  no Qt
        │             units, ring buffers          │
        └──────────────────────────────────────────┘
```

Dependencies point downward only. The practical payoff: the entire data layer
runs under `pytest` with no display server, no event loop and no `QApplication`.
Over two-thirds of the test suite exercises real collectors against the real
machine in under eight seconds.

---

## 2. Absence is a value, not an exception

This is the single most consequential decision in the codebase.

On Linux, whether a metric exists depends on the kernel version, the
distribution, the hardware and the permissions of the running user. RAPL power
counters are root-only since CVE-2020-8694. SMART needs raw device access. A
laptop GPU may have no controllable fan. Integrated graphics have no VRAM.

Three ways to handle that, two of them bad:

| Approach | Problem |
| --- | --- |
| Raise an exception | Every call site needs a `try`; one missing sensor breaks a page |
| Return `0` or `None` | The user cannot tell "idle" from "unmeasurable" |
| **Return `Unavailable(reason, detail)`** | Absence carries its explanation |

```python
@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: Reason = Reason.PENDING
    detail: str = ""

    def __bool__(self) -> bool:
        return False
```

It is falsy, so `if value:` reads naturally. It stringifies to a displayable
explanation. It has an `actionable` property distinguishing *install this tool*
from *this hardware does not exist*, which the Diagnostics page uses to produce
a list of things the user could actually fix.

Every formatter in `core/units.py` accepts it, so views format unconditionally:

```python
tile.set_from(snapshot.package_power_w, watts)
# → "28.4 W", or an em dash whose tooltip reads
#   "Insufficient permissions: energy_uj is root-only on this kernel"
```

The marker survives JSON export as a structured object, so a script consuming
the export can also tell absence from zero.

### The companion predicate

`is_number()` exists because `isinstance(value, float)` is a subtly wrong
availability test: sysfs and psutil both return plain integers for some
readings, and a float-only check silently classifies an integer temperature as
absent. That defect existed in this codebase and was caught by a unit test; the
predicate and its test now prevent its return.

---

## 3. Capability probing happens once

`core/capabilities.py` probes for every optional feature at start-up and caches
the result behind `functools.cached_property`. Collectors consult the registry
instead of rediscovering the environment on every poll, and each absent
capability carries the reason that later becomes an `Unavailable`.

```python
@cached_property
def rapl_power(self) -> Capability:
    domains = sysfs.glob("class/powercap/intel-rapl:*/energy_uj")
    if not domains:
        return Capability("rapl_power", False, Reason.NO_DRIVER, ...)
    if sysfs.read_int(domains[0]) is None:
        return Capability("rapl_power", False, Reason.PERMISSION, ...)
    return Capability("rapl_power", True)
```

Note that it distinguishes *absent* from *forbidden*. The user can act on the
second.

---

## 4. Concurrency: one ticker, one pool

Ten collectors want different cadences, and their costs differ by two orders of
magnitude — `/proc/stat` takes 0.5 ms, the process table 60 ms, systemd 650 ms.
All of it must happen without the interface dropping a frame.

| Design | Why not |
| --- | --- |
| One thread per collector | Ten mostly-idle OS threads; fiddly shutdown |
| One shared worker thread | The 650 ms systemd poll stalls the 1 s CPU chart |
| **Ticker + bounded thread pool** | Chosen |

A single `QTimer` on the GUI thread fires every 200 ms, compares timestamps, and
dispatches due collectors to a four-thread `QThreadPool`. The ticker itself does
no work beyond arithmetic.

Guarantees:

- **No overlapping runs.** A collector still working when its slot arrives is
  skipped and its next attempt pushed out, so a chronically slow collector
  degrades its own frequency rather than saturating the pool.
- **Results arrive on the GUI thread.** Snapshots cross the boundary through a
  queued Qt signal connection; no view ever touches data a worker is mutating.
- **Payloads are immutable.** Snapshots are frozen dataclasses, so there is
  nothing to race on even if a consumer keeps a reference.

Measured on the development machine (6C/12T laptop, 370 processes):

```
GUI heartbeat target 50 ms → median 50.1 ms, p95 50.3 ms, max 51.2 ms
Beats exceeding 100 ms: 0   (measured across a 40-second run)
```

---

## 5. Cost control

The first working version cost 21.5% of a core. Profiling found four causes,
each fixed in a way worth recording:

| Problem | Cost | Fix |
| --- | --- | --- |
| `psutil.sensors_temperatures()` scans every hwmon chip; three collectors each called it | 9 ms × 3 | A shared provider with a 450 ms TTL, plus a targeted per-chip reader that caches resolved sysfs paths — 9 ms → **0.27 ms** |
| `nvidia-smi` cost scales with requested field count | 42 ms | Split the query: identity once per session, telemetry per sample — **25 ms** |
| The GPU process list needs a second `nvidia-smi` | 22 ms/sample | Refresh every fourth sample; the process set changes slowly |
| `powerprofilesctl` is a Python D-Bus script | 93 ms/sample | Cache for 60 s — **1.9 ms** |
| `psutil.net_connections(kind="unix")` builds an object per socket to produce a count | 16 ms | Count lines in `/proc/net/unix` |

Result: **21.5% → ~9% of one core**, with `systemd` additionally suspended
whenever its page is closed. The Diagnostics page shows the live per-collector
breakdown, so the application can be held to its own standard.

A separate incident is worth recording because it was not a micro-optimisation:
`bluetoothctl devices`, invoked non-interactively without a powered controller,
blocks waiting for input until killed — a **four-second** stall of a worker
thread. It was replaced by a sysfs read. The lesson generalises: every external
command runs under a hard timeout, and an interactive REPL is never a data
source.

---

## 6. Rate arithmetic

Most interesting Linux metrics are monotonic counters. Converting them to rates
is where monitoring tools quietly go wrong, so `RateTracker` handles the three
failure modes explicitly:

- **First sample** — no previous value, so the rate is `0.0`, not the absolute
  counter value (which would render as an enormous opening spike).
- **Counter reset** — an interface goes down, a device is re-plugged, a 32-bit
  counter wraps. A negative delta reports `0.0`, never negative traffic.
- **Irregular intervals** — rates divide by *measured* elapsed time, never the
  configured interval, so a late tick cannot inflate the figure.

`forget(prefix)` discards a departed device's counters, because otherwise a
replacement with the same kernel name would produce one enormous false spike
from the stale baseline.

---

## 7. Storage: two tiers

Charts and history have different needs, so they get different storage.

**In memory** — a fixed-capacity `collections.deque` ring buffer per metric.
O(1) append, automatic eviction, bounded memory no matter how long the session
runs.

**On disk** — SQLite, written behind a flush timer. Samples accumulate in memory
and are committed in one transaction every 15 seconds; a transaction per metric
per second would mean thousands of fsyncs a minute.

The schema is deliberately narrow — `(timestamp, metric, value)` triples rather
than a column per metric. A wide table would need a migration every time a
metric is added and would be mostly `NULL` on a machine with no GPU or battery.
WAL journalling lets the interface read history while the writer appends.

Retention enforces both an age limit and a size ceiling, with a `VACUUM` after a
large prune because SQLite otherwise keeps the freed pages.

---

## 8. Alerting without noise

A naive "CPU > 90%" rule fires when you open a browser and trains the user to
ignore alerts. Two mechanisms prevent that:

**Sustain windows.** A rule fires only once its condition has held continuously
for `sustain_seconds`. A momentary spike resets the timer without firing.

**Hysteresis.** Once firing, a rule clears only when the value retreats past the
threshold by 3%. Without it a metric sitting exactly on the boundary would fire
and clear on alternate samples.

Both are covered by tests driven from a controlled clock rather than real time.

---

## 9. The UI layer

**Design tokens.** Every colour, radius and spacing step is named once in
`ui/themes/tokens.py`. Widgets reference tokens, never literals, so a new theme
is a new `Palette` instance and the charts provably agree with the stylesheet.

**Generated stylesheets.** Qt Style Sheets have no variables, so the QSS is
built from the active palette at run time.

Two Qt-specific traps are worth recording, because both produced visible bugs
during development:

1. **A plain `QWidget` subclass ignores a stylesheet `background`** unless
   `WA_StyledBackground` is set. The sidebar rendered light-on-light until this
   was applied — and again for the unnamed widget inside its scroll area.
2. **pyqtgraph does not inherit the parent's stylesheet.** `setBackground(None)`
   leaves the default light grey, so a dark-theme card contains a glaring white
   plot. The surface colour must be passed explicitly.

**Custom painting where it pays.** Ring gauges, bar meters, the core heatmap and
the composition bar are painted with `QPainter`. A 128-thread core heatmap is
one `paintEvent`, not 128 stylesheet-evaluated child widgets.

**Model/view for large tables.** The process table refreshes several hundred
rows every 2.5 seconds. `QTableWidget` would destroy and recreate an item per
cell — tens of thousands of allocations per refresh, discarding the user's
selection and scroll position each time. A model emits `dataChanged` instead and
keeps both. Sorting uses a separate raw-value role, so a "Memory" column sorts
on an integer byte count while displaying `1.4 GiB`.

**Lazy pages with snapshot replay.** Pages are built on first visit, so start-up
only pays for what the user opens. Because a lazily-built page would otherwise
miss every snapshot that arrived before it existed — up to 30 seconds of empty
table for systemd — the window caches the latest snapshot per domain and replays
it on first build.

---

## 10. Composition root

`app/bootstrap.py` is the only module that knows how the object graph fits
together. Everything else receives collaborators through constructor parameters.
The container is 75 lines and provides exactly three things: lazy singletons,
one wiring location, and constructor injection for tests. It detects circular
dependencies and disposes services in reverse instantiation order.

Cross-domain enrichment is **pushed, not pulled**: the GPU snapshot's per-process
VRAM figures are handed to the process collector by the bootstrap wiring, so the
process collector keeps no dependency on the GPU layer and remains testable on a
machine with no graphics adapter.

---

## 11. Security posture

- **Allow-listed actions.** systemd verbs come from a frozen set; an arbitrary
  string can never reach the command line.
- **Validated unit names.** A strict pattern rejects anything containing shell
  metacharacters, option prefixes or path traversal. Nine adversarial inputs are
  covered by tests.
- **No shell, ever.** Every subprocess is invoked with an argument list.
- **Hard timeouts.** No external command can stall the pipeline.
- **No password handling.** Escalation is delegated to `pkexec`, which presents
  the desktop's own dialog.
- **Credential masking.** Process environment variables whose names suggest
  secrets are redacted before display — deliberately over-broad, because a
  monitoring UI gets screen-shared.
- **Self-protection.** The process collector refuses to signal PID 1 or its own
  process.
