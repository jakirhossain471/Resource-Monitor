# Resource Monitor

A lightweight, always-on-desktop system monitor widget for Windows. Frameless,
translucent, and draggable — it sits quietly on your desktop and shows live CPU,
RAM, GPU, disk, network, and temperature readings. Click any metric to drill down
into the top processes driving it.

Built with [PySide6](https://doc.qt.io/qtforpython/) (Qt for Python). Hardware
temperatures come from [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor);
GPU metrics use NVIDIA's NVML.

![screenshot placeholder](docs/screenshot.png)

## Features

- **Live metrics** every second: CPU %, RAM used/total, GPU utilization + VRAM,
  disk read/write, network up/down.
- **Temperatures**: CPU, motherboard, GPU (via LibreHardwareMonitor + NVML).
- **Drill-down panel**: click a metric to see the top processes / connections
  driving it (CPU, RAM, GPU, disk I/O, active network connections).
- **Frameless desktop widget**: rounded translucent card that stays on the
  desktop layer, draggable anywhere, adjustable opacity.
- **Click-through mode**: let mouse events pass through the widget to windows
  beneath it.
- **Tray icon + right-click menu**: toggle individual metrics, opacity,
  click-through, and quit.
- **Cheap when idle**: labels repaint only when a formatted value actually
  changes; an idle system draws almost nothing.
- **Single instance**: a named mutex prevents a second copy from fighting over
  settings or the tray.
- **Graceful degradation**: any missing backend (no NVIDIA GPU, temps blocked,
  etc.) renders as `N/A` instead of crashing.

## Requirements

- Windows 10 / 11 (64-bit)
- Python 3.13 (3.11+ should work)
- An NVIDIA GPU for GPU metrics (optional; degrades to `N/A` otherwise)
- Temperatures generally require running as **Administrator** (LibreHardwareMonitor
  needs kernel access to read hardware sensors)

## Install & run from source

```powershell
git clone https://github.com/<your-user>/resource-monitor.git
cd resource-monitor

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python main.py
```

The `lib/` folder ships the LibreHardwareMonitor .NET assemblies and their
dependencies — they are loaded at runtime via [pythonnet](https://github.com/pythonnet/pythonnet)
and are not installed from pip.

## Build a standalone .exe

[PyInstaller](https://pyinstaller.org/) spec is included:

```powershell
pip install pyinstaller
pyinstaller ResourceMonitor.spec
```

The bundled app lands in `dist/ResourceMonitor/`. The `lib/` DLLs and `app.ico`
are packaged automatically by the spec.

## Configuration

Settings persist to `%APPDATA%\ResourceMonitor\settings.json`:

| Key             | Meaning                                         |
|-----------------|-------------------------------------------------|
| `pos`           | `[x, y]` widget position (or `null` = default)  |
| `opacity`       | Window opacity, `0.3`–`1.0`                     |
| `show`          | Per-metric visibility toggles                   |
| `temps_enabled` | Gate the LibreHardwareMonitor load              |
| `click_through` | Pass mouse events through the widget            |

The file is written on change; delete it to reset to defaults.

## Project layout

```
main.py          Entry point: wires the sensor thread to the widget, clean shutdown
sensors.py       Sensor backends (psutil / NVML / LibreHardwareMonitor) + polling QThread
widget.py        Frameless translucent desktop widget, tray, right-click menu
detail_panel.py  Drill-down popup: per-source top-process rows
settings.py      Load/save JSON settings in %APPDATA%
lib/             Bundled LibreHardwareMonitor .NET assemblies (see NOTICE)
ResourceMonitor.spec  PyInstaller build spec
```

## Architecture notes

- One `QThread` polls psutil + NVML every tick (1s) and LibreHardwareMonitor
  temps every 3rd tick, emitting a plain dict via a Qt signal (queued cross-thread
  delivery). All CLR/NVML/WMI init, read, and teardown happen on that worker
  thread because those handles have thread affinity.
- The widget renders only when a formatted string changes, keeping idle CPU near
  zero.

## License

This project's own source is released under the [MIT License](LICENSE).

It **bundles** third-party components under their own licenses — see [NOTICE](NOTICE):

- **LibreHardwareMonitorLib.dll** — MPL-2.0
- **HidSharp.dll** — Apache-2.0 / MIT
- **System.Memory / System.Buffers / System.Numerics.Vectors /
  System.Runtime.CompilerServices.Unsafe** — MIT (Microsoft)

Redistribution must preserve those licenses. `lib/LICENSE-LHM.txt` carries the
LibreHardwareMonitor terms.

## Acknowledgements

- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
- [psutil](https://github.com/giampaolo/psutil)
- [pythonnet](https://github.com/pythonnet/pythonnet)
- [Qt for Python (PySide6)](https://doc.qt.io/qtforpython/)
