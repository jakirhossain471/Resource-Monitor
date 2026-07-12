"""Frameless translucent desktop widget.

Draws a rounded card that sits on the desktop (StaysOnBottom), draggable, with a
tray + right-click menu. Value labels use tabular figures and update via setText
only when the formatted string changes, so an idle system repaints ~nothing.
"""
import ctypes
import time

from PySide6.QtCore import Qt, QPoint, Signal, Slot
from PySide6.QtGui import (QAction, QColor, QFont, QGuiApplication, QIcon, QPainter,
                           QPainterPath, QPixmap)
from PySide6.QtWidgets import (QApplication, QGridLayout, QLabel, QMenu, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

import settings as cfg
from detail_panel import DetailPanel
from sensors import is_admin

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# Declare pointer-width signatures so 64-bit HWND / LONG_PTR aren't truncated to int.
_user32 = ctypes.windll.user32
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]

MUTED = "#8a8f98"
BRIGHT = "#e8eaed"
AMBER = "#ffb454"
RED = "#ff5f56"
BLUE = "#62b0ff"

# (key, label) rows, in display order
ROWS = [("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("disk", "DISK"), ("net", "NET"), ("temp", "TEMP")]


def _rate(bps):
    if bps is None:
        return "—"
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps / 1024 / 1024:.1f} MB/s"


def _load_color(v):
    if v is None:
        return MUTED
    return BRIGHT if v < 70 else AMBER if v < 90 else RED


def _temp_color(v):
    if v is None:
        return MUTED
    return BRIGHT if v < 70 else AMBER if v < 85 else RED


class MonitorWidget(QWidget):
    detail_request = Signal(str, int)   # (metric, token); "" metric = stop gathering

    def __init__(self, s):
        super().__init__()
        self.s = s
        self._drag = None
        self._press = None
        self._last = {}   # key -> last (text, color) to skip no-op setText
        self._panel = None
        self._panel_metric = None
        self._detail_token = 0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnBottomHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(s["opacity"])

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)
        header = QLabel("RESOURCE MONITOR")
        header.setStyleSheet(f"color:{BLUE};font:600 9px 'Segoe UI';letter-spacing:2px;")
        root.addWidget(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self._rows = {}
        namefont = QFont("Segoe UI", 9)
        valfont = QFont("Segoe UI Semibold", 11)
        valfont.setStyleStrategy(QFont.PreferAntialias)
        for i, (key, label) in enumerate(ROWS):
            n = QLabel(label)
            n.setFont(namefont)
            n.setStyleSheet(f"color:{MUTED};")
            v = QLabel("—")
            v.setFont(valfont)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v.setStyleSheet(f"color:{BRIGHT};")
            grid.addWidget(n, i, 0)
            grid.addWidget(v, i, 1)
            self._rows[key] = (n, v)
        root.addLayout(grid)

        self._apply_visibility()
        self._build_tray()
        if s.get("pos"):
            self.move(self._clamp_pos(QPoint(*s["pos"])))
        if s.get("click_through"):
            self.set_click_through(True)

    @staticmethod
    def _clamp_pos(p):
        # A saved position on a now-disconnected monitor would put the widget off-screen
        # (and, with click-through, unrecoverable). Fall back to the primary screen.
        for scr in QGuiApplication.screens():
            if scr.availableGeometry().contains(p):
                return p
        return QGuiApplication.primaryScreen().availableGeometry().topLeft() + QPoint(40, 40)

    # --- painting: rounded translucent card + faux-depth border ---
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.fillPath(path, QColor(18, 20, 28, 235))
        p.setPen(QColor(255, 255, 255, 22))          # inner highlight
        p.drawPath(path)
        p.setPen(QColor(0, 0, 0, 70))                # outer shade => faux depth
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 15, 15)

    # --- live update (GUI thread, queued from sensor thread) ---
    @Slot(dict)
    def update_reading(self, d):
        vals = {
            "cpu": (self._pct(d["cpu"]), _load_color(d["cpu"])),
            "ram": (self._ram(d), _load_color(d["ram_pct"])),
            "gpu": self._gpu(d),
            "disk": self._disk(d),
            "net": (f"↓ {_rate(d['net_down'])}   ↑ {_rate(d['net_up'])}", BLUE),
            "temp": self._temp(d),
        }
        for key, (text, color) in vals.items():
            if self._last.get(key) == (text, color):
                continue
            self._last[key] = (text, color)
            lbl = self._rows[key][1]
            lbl.setText(text)
            lbl.setStyleSheet(f"color:{color};")

    @staticmethod
    def _pct(v):
        return "—" if v is None else f"{v:.0f}%"

    def _ram(self, d):
        if d["ram_pct"] is None:
            return "—"
        used = d["ram_used"] / 1024 ** 3 if d["ram_used"] else 0   # GiB (Task Manager convention)
        tot = d["ram_total"] / 1024 ** 3 if d["ram_total"] else 0
        return f"{d['ram_pct']:.0f}%  {used:.1f}/{tot:.0f}G"

    def _gpu(self, d):
        util = d["gpu_util"] if d["gpu_util"] is not None else d["igpu_util"]
        if util is None:
            return ("—", MUTED)
        s = f"{util:.0f}%"
        if d["gpu_mem_used"] and d["gpu_mem_total"]:
            s += f"  {d['gpu_mem_used'] / 1024 ** 3:.1f}/{d['gpu_mem_total'] / 1024 ** 3:.0f}G"
        return (s, _load_color(util))

    def _disk(self, d):
        r, w = d["disk_read"], d["disk_write"]
        if r is None and w is None:
            return ("—", MUTED)
        return (f"↓ {_rate(r)}   ↑ {_rate(w)}", BLUE)

    def _temp(self, d):
        parts = []
        if d["cpu_temp"] is not None:
            parts.append(f"C {d['cpu_temp']:.0f}°")
        if d["gpu_temp"] is not None:
            parts.append(f"G {d['gpu_temp']:.0f}°")
        if d["mobo_temp"] is not None:
            parts.append(f"M {d['mobo_temp']:.0f}°")
        if not parts:
            return ("—", MUTED)
        hot = max(v for v in (d["cpu_temp"], d["gpu_temp"], d["mobo_temp"]) if v is not None)
        return ("  ".join(parts), _temp_color(hot))

    # --- visibility / menu ---
    def _apply_visibility(self):
        for key, (n, v) in self._rows.items():
            vis = self.s["show"].get(key, True)
            n.setVisible(vis)
            v.setVisible(vis)
        self.adjustSize()
        self.setFixedWidth(max(210, self.sizeHint().width()))

    def _build_menu(self):
        m = QMenu(self)
        for key, label in ROWS:
            a = QAction(label, m, checkable=True)
            a.setChecked(self.s["show"].get(key, True))
            a.toggled.connect(lambda on, k=key: self._toggle_metric(k, on))
            m.addAction(a)
        m.addSeparator()
        op = m.addMenu("Opacity")
        for pct in (100, 85, 70):
            a = QAction(f"{pct}%", op, checkable=True)
            a.setChecked(abs(self.s["opacity"] - pct / 100) < 0.01)
            a.triggered.connect(lambda _, p=pct: self._set_opacity(p / 100))
            op.addAction(a)
        ct = QAction("Click-through", m, checkable=True)
        ct.setChecked(self.s.get("click_through", False))
        ct.toggled.connect(self.set_click_through)
        m.addAction(ct)
        if not is_admin():
            note = QAction("Run as admin for CPU temps", m)
            note.setEnabled(False)
            m.addSeparator()
            m.addAction(note)
        m.addSeparator()
        quit_a = QAction("Quit", m)
        quit_a.triggered.connect(QApplication.quit)
        m.addAction(quit_a)
        return m

    def _build_tray(self):
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(BLUE))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 24, 24, 6, 6)
        p.end()
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        self.tray.setToolTip("Resource Monitor")
        self._refresh_tray_menu()
        self.tray.show()

    def _refresh_tray_menu(self):
        old = self.tray.contextMenu()
        self.tray.setContextMenu(self._build_menu())
        if old is not None:
            old.deleteLater()          # don't accumulate a menu per checkmark rebuild

    def _toggle_metric(self, key, on):
        self.s["show"][key] = on
        cfg.save(self.s)
        self._apply_visibility()
        self._refresh_tray_menu()   # keep tray checkmarks in sync

    def _set_opacity(self, o):
        self.s["opacity"] = o
        self.setWindowOpacity(o)
        if self._panel:
            self._panel.setWindowOpacity(o)
        cfg.save(self.s)
        self._refresh_tray_menu()

    def set_click_through(self, on):
        self.s["click_through"] = on
        hwnd = int(self.winId())
        style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if on:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        cfg.save(self.s)
        self._refresh_tray_menu()   # keep tray checkmark in sync

    def contextMenuEvent(self, e):
        m = self._build_menu()
        m.setAttribute(Qt.WA_DeleteOnClose)   # don't accumulate a menu per right-click
        m.exec(e.globalPos())

    # --- detail panel ---
    def _row_at(self, pos):
        for key, (n, v) in self._rows.items():
            if not n.isVisible():
                continue
            top = min(n.geometry().top(), v.geometry().top()) - 3
            bot = max(n.geometry().bottom(), v.geometry().bottom()) + 3
            if top <= pos.y() <= bot:
                return key
        return None

    def _toggle_panel(self, metric):
        if self._panel is None:
            self._panel = DetailPanel(self.s["opacity"])
            self._panel.closed.connect(self._on_panel_closed)
        p = self._panel
        if p.isVisible() and p.cur_metric == metric:
            self._close_panel()
            return
        # toggle-race grace: clicking the widget deactivates+closes the panel first;
        # without this the same-row click would immediately reopen it.
        if metric == self._panel_metric and time.monotonic() - p.closed_at < 0.25:
            return
        self._panel_metric = metric
        self._detail_token += 1
        p.open_for(metric, self)
        self.detail_request.emit(metric, self._detail_token)

    def _close_panel(self):
        if self._panel and self._panel.isVisible():
            self._panel.close()   # fires closed -> _on_panel_closed

    def _on_panel_closed(self):
        self._detail_token += 1   # invalidate any in-flight detail emission
        self.detail_request.emit("", self._detail_token)

    @Slot(str, int, list)
    def update_detail(self, metric, token, rows):
        if token == self._detail_token and self._panel and self._panel.isVisible():
            self._panel.set_rows(metric, rows)

    # --- drag to move / click to drill down ---
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._drag = self._press - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        if (e.globalPosition().toPoint() - self._press).manhattanLength() < 6:
            return   # within jitter threshold — a click, don't move yet
        self.move(e.globalPosition().toPoint() - self._drag)
        self._close_panel()   # a parked panel looks broken while dragging

    def mouseReleaseEvent(self, e):
        if self._drag is None:
            return
        moved = (e.globalPosition().toPoint() - self._press).manhattanLength()
        self._drag = None
        if moved < 6:                                    # a click, not a drag
            key = self._row_at(e.position().toPoint())
            if key and key != "temp":
                self._toggle_panel(key)
        else:                                            # a real drag: persist position
            self.s["pos"] = [self.x(), self.y()]
            cfg.save(self.s)
