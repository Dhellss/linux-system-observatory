# Packaging notes

## Desktop entry

Install `observatory.desktop` so the application appears in the desktop menu:

```bash
install -Dm644 packaging/observatory.desktop \
    ~/.local/share/applications/observatory.desktop
update-desktop-database ~/.local/share/applications
```

## Optional privilege escalation for SMART

SMART attributes require raw device access. Rather than running the whole
application as root, grant `smartctl` alone:

```bash
sudo tee /etc/sudoers.d/observatory-smart >/dev/null <<'SUDO'
%wheel ALL=(root) NOPASSWD: /usr/bin/smartctl
SUDO
sudo chmod 0440 /etc/sudoers.d/observatory-smart
```

The application never invokes `sudo` itself. Systemd unit actions are escalated
through `pkexec`, which presents the desktop's own authentication dialog, so no
password ever passes through this program.

## Distribution packaging

The project is a standard PEP 517 package. Distribution maintainers can build a
wheel with `python -m build` and depend on the system's own `python-pyside6`,
`python-psutil` and `python-pyqtgraph` packages rather than vendoring them.

Recommended optional runtime dependencies, each of which unlocks additional
metrics and all of which degrade gracefully when absent:

| Package        | Enables                                 |
| -------------- | --------------------------------------- |
| `smartmontools`| Drive health, endurance, SMART attributes |
| `lm_sensors`   | Motherboard voltage and fan sensors     |
| `iw`           | Wi-Fi SSID, channel and link rate       |
| `pciutils`     | PCI device names                        |
| `usbutils`     | USB device enumeration                  |
| `mesa-utils`   | OpenGL renderer information             |
| `vulkan-tools` | Vulkan device enumeration               |
| `polkit`       | Privileged systemd unit actions         |
