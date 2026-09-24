# Linux System Observatory

A comprehensive system monitor for Linux desktops. It combines the depth of
`btop` and `nvtop`, the process management of GNOME System Monitor, and the
hardware inventory of `inxi`, behind one modern Qt interface.

Everything stays on your machine. No telemetry, no analytics, no update check,
no network access unless you explicitly ask for a latency probe.

---

## What it monitors

| Page | Highlights |
| --- | --- |
| **Dashboard** | Customisable widget grid — reorder, resize, choose from 15 widgets |
| **Processor** | Per-core load and frequency, SMT topology, cache, governor, turbo state, RAPL power, full CPU-time breakdown, core heatmap |
| **Memory** | Composition bar, kernel accounting (slab, page tables, dirty, commit), swap areas, **zram compression ratios**, huge pages, **PSI pressure stalls** |
| **Graphics** | NVIDIA, AMD and Intel in one view — utilisation, VRAM, clocks, power, throttle reasons, per-process VRAM, OpenGL/Vulkan stack |
| **Storage** | Devices, partitions, filesystems, per-device IOPS, queue depth, latency, **SMART health and NVMe endurance** |
| **Network** | Per-interface throughput, Wi-Fi signal, sockets with process attribution, routing, DNS, on-demand latency probe |
| **Processes** | Table and tree views, inspector (threads, open files, environment), priority and affinity control, terminate/kill with confirmation |
| **Services** | systemd units, dependencies, boot timing breakdown, start/stop/enable via polkit |
| **Sensors** | Every hwmon chip — temperatures, fans, voltages, currents, power |
| **Battery** | Charge, **health versus design capacity**, wear, cycles, power flow |
| **System** | Distribution, kernel, firmware, Secure Boot, DIMMs, PCI/USB/audio/input devices, displays |
| **Logs** | Journal viewer with filtering, search, bookmarks and export |
| **Diagnostics** | 16 health checks, each with evidence and a recommended action |
| **History** | Recorded metrics, named sessions, CSV/JSON export |
| **Alerts** | Threshold rules with sustain windows and hysteresis |
| **Settings** | Theme, per-domain sampling intervals, retention, privacy, notifications |

---

## Three things done differently

**Absent data is explained, never faked.** Most monitors show `0` or a blank
when a metric is unavailable. This one returns a typed `Unavailable` value
carrying the *reason*, which the interface renders as an em dash with the
explanation in the tooltip:

> `Insufficient permissions: energy_uj is root-only on this kernel`

You always know whether a number is zero, missing, or unmeasurable — and what
you could do about it. The Diagnostics page lists every unavailable source and
which are actionable.

**Thresholds come from your hardware.** A CPU rated to 105 °C and an NVMe
controller rated to 85 °C do not share a danger point, so limits are read from
the device rather than hard-coded. Alerts additionally require a condition to
hold for a sustain window and use a hysteresis band when clearing, so a browser
launch does not fire a "CPU critical" notification.

**Monitoring is measured against itself.** Every collector runs on its own
cadence with its own cost instrumentation, and the Diagnostics page shows the
breakdown. On the development machine the full set costs roughly **9% of one
core**, with the GUI thread's frame budget never exceeded.

---

## Installation

Requires Python 3.11 or newer. Developed and tested on Python 3.14; the 3.11
floor reflects the language features used (no PEP 695 syntax) but has not been
exercised in CI yet.

```bash
git clone https://github.com/<your-username>/linux-system-observatory.git
cd linux-system-observatory
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Or install it properly:

```bash
pip install .
observatory
```

See [docs/INSTALL.md](docs/INSTALL.md) for distribution packages, optional
tools, and the SMART permissions setup.

### Optional tools

Every one of these is optional; the application detects what is present at
start-up and explains what is missing.

| Tool | Unlocks |
| --- | --- |
| `smartmontools` | Drive health, endurance, SMART attributes |
| `lm_sensors` | Motherboard voltages and fan speeds |
| `iw` | Wi-Fi SSID, channel and link rate |
| `pciutils`, `usbutils` | Device names in the inventory |
| `mesa-utils`, `vulkan-tools` | OpenGL and Vulkan details |
| `polkit` | Privileged systemd unit actions |

---

## Usage

```bash
observatory                     # normal launch
observatory --theme light       # override the theme for one run
observatory --page CpuPage      # open straight to a page
observatory --no-history        # run without writing anything to disk
observatory --debug             # verbose logging
```

### Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+K` | Command palette |
| `Ctrl+F` | Focus search |
| `Ctrl+1`…`Ctrl+9` | Jump to page |
| `Ctrl+B` | Collapse sidebar |
| `Ctrl+P` | Pause / resume monitoring |
| `F5` | Refresh current page |
| `Escape` | Dismiss palette, banner or search |

---

## Privacy

This is a local tool and behaves like one:

- No telemetry, analytics, crash reporting or update check.
- Nothing is written outside `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME` and
  `$XDG_STATE_HOME`.
- Process environment variables whose names suggest credentials are masked
  before they are ever displayed.
- The **only** outbound network access is the Network page's latency probe. It
  is disabled by default, must be enabled in Settings, targets a host you
  choose, and runs only when you press the button.
- The application never handles your password. Privileged systemd actions go
  through `pkexec`, so your desktop's own authentication dialog handles it.

---

## Architecture

```
app/
├── core/          Capability probing, DI container, config, units, buffers
├── models/        Immutable snapshot dataclasses + the Unavailable sentinel
├── collectors/    One per domain — pure Python, no Qt, independently testable
├── services/      Scheduler, history, alerts, diagnostics, export
├── database/      SQLite schema, connections, repositories
├── ui/
│   ├── themes/    Design tokens and generated stylesheets
│   ├── widgets/   Cards, charts, gauges, tables, navigation
│   └── pages/     One module per page
└── utils/         sysfs, PCI, hwmon and subprocess helpers

tools/             Development helpers (headless page screenshots)
tests/             213 tests, no display server required
docs/              Architecture, development, installation
```

Dependencies flow one way: `ui → services → collectors → models → core`. The
collector layer imports no Qt at all, which is why the whole data layer can be
tested headlessly.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design decisions and
the reasoning behind them.

---

## Development

```bash
.venv/bin/python -m pytest          # 213 tests, no display server needed
.venv/bin/ruff check app tests      # lint  — clean
.venv/bin/mypy app                  # types — clean across 90 modules

# Render every page to a PNG for visual review, headlessly:
QT_QPA_PLATFORM=offscreen .venv/bin/python tools/screenshot_pages.py out/ dark
```

[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) explains how to add a collector, a
metric, a dashboard widget or a page. Each is a small, local change by design.

---

## Licence

MIT. See [LICENSE](LICENSE).
