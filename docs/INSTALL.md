# Installation

## Requirements

- Linux with a 4.20 or newer kernel (older kernels work; PSI pressure metrics
  need 4.20+)
- Python 3.11 or newer (developed and tested on 3.14)
- A Qt-capable desktop session (X11 or Wayland)

## From source

```bash
git clone https://github.com/Dhellss/linux-system-observatory.git
cd linux-system-observatory
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

`--system-site-packages` lets the virtual environment reuse a distribution's
PySide6 build, which is usually better integrated with the desktop than the
wheel from PyPI. Omit it for a fully isolated environment.

## As an installed package

```bash
pip install .
observatory
```

This provides the `observatory` command. Add the desktop entry so it appears in
your application menu:

```bash
install -Dm644 packaging/observatory.desktop \
    ~/.local/share/applications/observatory.desktop
update-desktop-database ~/.local/share/applications
```

## Distribution dependencies

Installing PySide6 from your distribution rather than PyPI is recommended.

**Arch Linux**
```bash
sudo pacman -S python-pyside6 python-psutil python-pyqtgraph
```

**Debian / Ubuntu**
```bash
sudo apt install python3-pyside6.qtwidgets python3-psutil python3-pyqtgraph
```

**Fedora**
```bash
sudo dnf install python3-pyside6 python3-psutil python3-pyqtgraph
```

**openSUSE**
```bash
sudo zypper install python3-pyside6 python3-psutil python3-pyqtgraph
```

## Optional tools

Each unlocks additional metrics. All are optional — the application probes for
them at start-up and explains in the Diagnostics page what is missing and what
installing it would enable.

| Package | Provides |
| --- | --- |
| `smartmontools` | Drive health, endurance, SMART attributes |
| `lm_sensors` | Motherboard voltages and fan speeds (run `sensors-detect` once) |
| `iw` | Wi-Fi SSID, channel and link rate |
| `pciutils` | PCI device names |
| `usbutils` | USB device enumeration |
| `mesa-utils` (or `mesa-demos`) | OpenGL renderer information |
| `vulkan-tools` | Vulkan device enumeration |
| `polkit` | Privileged systemd unit actions |
| `libnotify` | Desktop notifications |

## SMART permissions

SMART attributes require raw device access, so an unprivileged session shows
"Insufficient permissions" on the Storage page. Drive **temperature** still
works without privileges, because it is read from hwmon.

Rather than running the whole application as root, grant `smartctl` alone:

```bash
sudo tee /etc/sudoers.d/observatory-smart >/dev/null <<'SUDO'
%wheel ALL=(root) NOPASSWD: /usr/bin/smartctl
SUDO
sudo chmod 0440 /etc/sudoers.d/observatory-smart
```

Replace `%wheel` with your administrative group (`sudo` on Debian/Ubuntu).

## Where files are stored

Everything follows the XDG Base Directory specification:

| Purpose | Location |
| --- | --- |
| Settings, dashboard layout, alert rules | `$XDG_CONFIG_HOME/linux-system-observatory/settings.json` |
| Metrics history database | `$XDG_DATA_HOME/linux-system-observatory/history.db` |
| Log file | `$XDG_STATE_HOME/linux-system-observatory/logs/observatory.log` |

To remove every trace:

```bash
rm -rf ~/.config/linux-system-observatory \
       ~/.local/share/linux-system-observatory \
       ~/.local/state/linux-system-observatory
```

## Troubleshooting

**The window does not appear on Wayland.** Force the X11 backend:
`QT_QPA_PLATFORM=xcb observatory`

**Fonts look wrong.** The interface prefers Inter and JetBrains Mono, falling
back to DejaVu. Installing `ttf-jetbrains-mono` and `inter-font` improves the
appearance but is not required.

**High CPU use.** Open Diagnostics to see the per-collector breakdown, then
raise the interval of the expensive one in Settings. The `processes` and
`services` collectors dominate on most systems.

**No GPU data.** Diagnostics reports which driver interface is missing. A
common cause is an Intel adapter bound to `simple-framebuffer` because `i915`
did not load.
