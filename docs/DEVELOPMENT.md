# Developer guide

How to extend the application. Every task below is a small, local change — that
is the point of the layering described in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Setting up

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/python run.py                    # run
.venv/bin/python -m pytest                 # test (no display server needed)
.venv/bin/ruff check app tests tools       # lint
.venv/bin/mypy app                         # type check
```

### A caveat about `mypy` and `--system-site-packages`

`--system-site-packages` lets the environment reuse a distribution's PySide6,
which is usually better integrated with the desktop. It has one trap worth
knowing about: some distribution packages ship **without the PySide6 type
stubs**, so `mypy` cannot see any Qt signature and silently skips every check
that depends on one. It reports success while verifying far less than it
appears to.

This is not hypothetical. A local run reported "no issues in 90 source files"
while CI -- which installs PySide6 from PyPI, stubs included -- found twelve
errors, three of them latent `None`-dereference crashes in real code paths.

CI is therefore the authority on types. To reproduce it exactly before pushing:

```bash
python -m venv /tmp/ci-check          # note: NO --system-site-packages
/tmp/ci-check/bin/pip install PySide6 psutil pyqtgraph mypy ruff pytest
/tmp/ci-check/bin/mypy app
```

Useful flags while developing:

```bash
python run.py --debug --page CpuPage --no-history
```

`--no-history` keeps your real metrics database untouched.

---

## Adding a collector

Five steps. Nothing else in the application needs to change.

**1. Define the snapshot** in `app/models/`:

```python
@dataclass(frozen=True, slots=True)
class ThermalZoneSnapshot(Snapshot):
    """One sample of the ACPI thermal zones."""

    zones: tuple[ThermalZone, ...] = ()
```

Use `Maybe[T]` for any field whose availability varies, and never a bare `0`.

**2. Write the collector** in `app/collectors/`:

```python
class ThermalZoneCollector(Collector[ThermalZoneSnapshot]):
    """Reads /sys/class/thermal."""

    domain = "thermal"
    title = "Thermal zones"

    def _collect(self) -> ThermalZoneSnapshot:
        return ThermalZoneSnapshot(zones=tuple(self._read_zones()))

    def _empty(self) -> ThermalZoneSnapshot:
        return ThermalZoneSnapshot()
```

Rules: no Qt imports; return `Unavailable` rather than raising; use
`self.rates` for counter-to-rate conversion; use `self.hwmon` for sensors so you
share the cache.

**3. Add a capability** in `core/capabilities.py` if the source is optional:

```python
@cached_property
def thermal_zones(self) -> Capability:
    return _any_glob("thermal_zones", "class/thermal/thermal_zone*")
```

**4. Register it** in `app/bootstrap.py`:

```python
COLLECTOR_CLASSES = (..., ThermalZoneCollector)
```

**5. Give it a cadence** in `core/config.py`:

```python
class SamplingSettings:
    thermal: float = 5.0
```

The settings page picks up the new interval automatically.

---

## Adding a metric

Metrics are what history records, alerts compare and the pickers enumerate. One
entry in `app/services/metrics.py` serves all three:

```python
MetricDefinition(
    "thermal.max", "Hottest thermal zone", "celsius",
    lambda s: _safe(max((z.temperature for z in s.zones), default=None)),
    "thermal", suggested_threshold=80.0,
)
```

`suggested_threshold` pre-fills the alert rule editor, and `inverted=True` marks
metrics where a *low* value is the problem (battery charge, free space).

The extractor must return `None` when the reading is unavailable. Never return
zero — it would be persisted as a real sample and poison every average drawn
from it.

---

## Adding a page

```python
class ThermalPage(Page):
    domain = "thermal"
    title = "Thermal zones"
    icon = "◈"
    section = "System"
    description = "ACPI thermal zone temperatures"

    def build_ui(self) -> None:
        card = Card("Zones", theme=self.theme, accent=self.context.colour("sensors"))
        self._table = KeyValueTable(theme=self.theme)
        card.add(self._table)
        self.add(card)

    def on_snapshot(self, domain: str, snapshot: object) -> None:
        if not self.is_visible_page:
            return
        ...
```

Register it in `PAGE_CLASSES` in `ui/main_window.py`. The sidebar, command
palette and keyboard shortcuts all build themselves from that list.

Conventions worth following:

- Build widgets once in `build_ui`; update them in `on_snapshot`. Rebuilding
  per sample allocates needlessly and destroys the user's selection.
- Guard expensive repaints with `if not self.is_visible_page: return`, but
  update chart *series* unconditionally so history stays continuous.
- Set `suspend_when_hidden = True` if the collector is expensive and its data
  appears nowhere else.
- Every empty region gets an `EmptyState` explaining why.

---

## Adding a dashboard widget

A widget is a spec plus a builder returning an updater closure:

```python
def _build_thermal(page: DashboardPage, card: Card):
    meter = BarMeter(theme=page.theme, label="Hottest zone")
    card.add(meter)

    def update(domain: str, snapshot: object) -> None:
        meter.set_value(...)

    return update


WidgetSpec("thermal", "Thermal", ("thermal",), 1, _build_thermal,
           "Hottest ACPI zone")
```

Append it to `_WIDGET_SPECS`. It appears in the Customise dialog immediately.

---

## Adding a theme

Add a `Palette` to `ui/themes/tokens.py`:

```python
SOLARIZED = replace(DARK, name="solarized", canvas="#002b36", ...)
PALETTES["solarized"] = SOLARIZED
```

Then add it to the theme combo in `ui/pages/settings.py`. Because every widget
reads tokens rather than literals, nothing else changes.

---

## Adding a diagnostic check

Add a method to `DiagnosticsService` and append it to `self._checks`:

```python
def _check_thermal_zones(self, store: SnapshotStore) -> list[CheckResult]:
    snapshot = store.get("thermal")
    if not isinstance(snapshot, ThermalZoneSnapshot):
        return [_skip("thermal", "Thermal zones", "No sample yet", "Sensors")]
    ...
```

A check must always supply `advice` when it reports a problem, and must return
`skipped=True` rather than `OK` when it could not run — "could not look" is not
"nothing wrong".

---

## Testing

The data layer needs no display server, so most tests are plain unit tests.

```bash
pytest                          # everything
pytest tests/test_alerts.py     # one module
pytest -k thermal               # by name
pytest --cov=app --cov-report=term-missing
```

Patterns used in this suite:

- **Contract tests** (`test_collectors.py`) run the real collectors against the
  real machine and assert the *contract* — returns a snapshot, never raises,
  reports its cost — because values differ per machine.
- **Controlled clocks** (`test_alerts.py`) drive time-dependent logic with
  `monkeypatch` rather than `sleep`.
- **Adversarial inputs** (`TestServiceCollectorSafety`) cover every injection
  shape the unit-name validator must reject.
- **Invariants over values**: assert that the CPU-time breakdown sums to 100%,
  not that user time equals some number.

For UI work there is a dedicated tool:

```bash
QT_QPA_PLATFORM=offscreen python tools/screenshot_pages.py out/ dark
QT_QPA_PLATFORM=offscreen python tools/screenshot_pages.py out/ light
```

It runs the real application against the real machine, visits every page and
saves a PNG of each. Any exception while building or updating a page is
reported with its traceback, so it doubles as an integration smoke test. This
is how the unstyled sidebar, the white-on-dark chart background and the clipped
composition legend were all caught.

---

## Code style

- PEP 8, 88-column lines, enforced by `ruff` (currently clean).
- Type hints on every public signature; `mypy app` is clean and should stay so.
- Prefer `is_number()` over `isinstance(x, float)` as an availability test —
  sysfs and psutil both return integers for some readings, and a float-only
  check silently classifies them as absent.
- Docstrings on every module, class and public method — numpy convention.
- Comments explain **why**, not what. A comment restating the code is noise; a
  comment recording why `asdict()` could not be used is worth keeping.
- One responsibility per module. If a file needs "and" to describe it, split it.
