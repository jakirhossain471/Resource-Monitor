"""Entry point: wire sensor thread to widget, own clean shutdown."""
import ctypes
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import settings as cfg
from sensors import SensorThread
from widget import MonitorWidget

# per-monitor-v2 rounding: keeps frameless dragging crisp across mixed-DPI monitors
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def _already_running():
    # named mutex → a second launch exits instead of fighting over settings.json / tray
    k = ctypes.WinDLL("kernel32", use_last_error=True)    # robust last-error, not clobbered by ctypes
    k.CreateMutexW(None, False, "ResourceMonitor.singleton")
    return ctypes.get_last_error() in (183, 5)           # ALREADY_EXISTS, or ACCESS_DENIED (elevated instance)


def main():
    if _already_running():
        return
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # tray keeps app alive if widget hidden

    s = cfg.load()
    widget = MonitorWidget(s)
    thread = SensorThread(temps_enabled=s.get("temps_enabled", True))
    thread.reading.connect(widget.update_reading)   # queued (cross-thread) automatically
    thread.detail.connect(widget.update_detail)     # per-source drill-down rows
    widget.detail_request.connect(thread.set_detail)  # GUI tells worker what to gather

    def shutdown():
        thread.stop()
        if not thread.wait(4000):   # teardown (nvml/LHM Close) runs inside run()'s finally
            thread.wait()           # a hung LHM Update must finish before GC — never orphan
    app.aboutToQuit.connect(shutdown)

    widget.show()
    thread.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
