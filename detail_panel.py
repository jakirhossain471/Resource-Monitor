"""Drill-down popup panel: a frameless translucent card that lists per-source rows.

Self-contained (own theme + card paint) to avoid a circular import with widget.py,
which owns it. Closes on window deactivation (survives clicking its own scrollbar,
unlike focus-out). One rich-text label, updated only when the rendered HTML changes.
"""
import html
import time

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

DIM = "#6b7079"
BRIGHT = "#e8eaed"
AMBER = "#ffb454"
RED = "#ff5f56"
BLUE = "#62b0ff"
GREEN = "#79d17a"

TITLES = {"cpu": "CPU — TOP PROCESSES", "ram": "RAM — TOP PROCESSES",
          "gpu": "GPU — PROCESSES", "net": "CONNECTIONS", "disk": "DISK I/O — TOP PROCESSES"}
PLACEHOLDER = {"cpu": "measuring…", "ram": "gathering…", "gpu": "gathering…",
               "net": "gathering…", "disk": "measuring…"}


def _load_color(v):
    return BRIGHT if v < 70 else AMBER if v < 90 else RED


def _mb(b):
    if not b:
        return "—"
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    return f"{b / 1024 ** 2:.0f} MB"


def _rate(b):
    if not b:
        return "—"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.1f} MB/s"
    if b >= 1024:
        return f"{b / 1024:.0f} KB/s"
    return f"{b:.0f} B/s"


def _state_color(s):
    return GREEN if s == "ESTABLISHED" else DIM if s in ("LISTEN", "NONE") else AMBER


def _row(name_html, value_html):
    return (f"<tr><td>{name_html}</td>"
            f"<td align='right' style='padding-left:14px'>{value_html}</td></tr>")


def fmt_cpu(rows):
    if not rows:
        return f"<span style='color:{DIM}'>no data</span>"
    out = ["<table width='100%' cellspacing='0' cellpadding='3'>"]
    for r in rows:
        nm = html.escape(r["name"][:26])
        n = f" <span style='color:{DIM}'>×{r['n']}</span>" if r["n"] > 1 else ""
        out.append(_row(f"<span style='color:{BRIGHT}'>{nm}</span>{n}",
                        f"<span style='color:{_load_color(r['cpu'])}'>{r['cpu']:.1f}%</span>"))
    out.append("</table>")
    return "".join(out)


def fmt_ram(rows):
    if not rows:
        return f"<span style='color:{DIM}'>no data</span>"
    out = ["<table width='100%' cellspacing='0' cellpadding='3'>"]
    for r in rows:
        nm = html.escape(r["name"][:26])
        n = f" <span style='color:{DIM}'>×{r['n']}</span>" if r["n"] > 1 else ""
        out.append(_row(f"<span style='color:{BRIGHT}'>{nm}</span>{n}",
                        f"<span style='color:{BRIGHT}'>{_mb(r['rss'])}</span>"))
    out.append("</table>")
    return "".join(out)


def fmt_gpu(rows):
    if not rows:
        return f"<span style='color:{DIM}'>no GPU processes</span>"
    out = ["<table width='100%' cellspacing='0' cellpadding='3'>"]
    for r in rows:
        nm = html.escape(r["name"][:26])
        out.append(_row(f"<span style='color:{BRIGHT}'>{nm}</span>",
                        f"<span style='color:{BRIGHT}'>{_mb(r['vram'])}</span>"))
    out.append("</table>")
    return "".join(out)


def fmt_net(rows):
    if not rows:
        return f"<span style='color:{DIM}'>no active connections (or unavailable)</span>"
    out = []
    for r in rows:
        nm = html.escape(r["name"][:24])
        out.append(f"<div style='margin-top:7px'><span style='color:{BRIGHT}'>{nm}</span> "
                   f"<span style='color:{DIM}'>{r['uptime']} · {r['total']} conn</span></div>")
        for ep, st in r["conns"]:
            out.append(f"<div style='margin-left:12px'><span style='color:{BLUE}'>{html.escape(ep[:30])}</span> "
                       f"<span style='color:{_state_color(st)}'>{st}</span></div>")
        if r["more"]:
            out.append(f"<div style='color:{DIM};margin-left:12px'>+{r['more']} more</div>")
    return "".join(out)


def fmt_disk(rows):
    if not rows:
        return f"<span style='color:{DIM}'>no active disk I/O</span>"
    out = ["<table width='100%' cellspacing='0' cellpadding='3'>"]
    for r in rows:
        nm = html.escape(r["name"][:26])
        n = f" <span style='color:{DIM}'>×{r['n']}</span>" if r["n"] > 1 else ""
        out.append(_row(f"<span style='color:{BRIGHT}'>{nm}</span>{n}",
                        f"<span style='color:{BRIGHT}'>{_rate(r['disk'])}</span>"))
    out.append("</table>")
    return "".join(out)


FORMATTERS = {"cpu": fmt_cpu, "ram": fmt_ram, "gpu": fmt_gpu, "net": fmt_net, "disk": fmt_disk}


class DetailPanel(QWidget):
    closed = Signal()

    def __init__(self, opacity):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)   # not StaysOnBottom, not click-through
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(opacity)
        self.cur_metric = None
        self.closed_at = 0.0
        self._last = None
        self._main = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        self._title = QLabel()
        self._title.setStyleSheet(f"color:{BLUE};font:600 9px 'Segoe UI';letter-spacing:2px;")
        root.addWidget(self._title)
        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QScrollArea.NoFrame)
        self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._area.setStyleSheet("QScrollArea,QScrollArea>QWidget,QScrollArea>QWidget>QWidget{background:transparent;}")
        self._area.viewport().setStyleSheet("background:transparent;")
        self._body = QLabel()
        self._body.setTextFormat(Qt.RichText)
        self._body.setAlignment(Qt.AlignTop)
        self._body.setWordWrap(False)
        self._body.setFont(QFont("Segoe UI", 9))
        self._area.setWidget(self._body)
        root.addWidget(self._area)
        self.setFixedWidth(300)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.fillPath(path, QColor(18, 20, 28, 235))
        p.setPen(QColor(255, 255, 255, 22))
        p.drawPath(path)
        p.setPen(QColor(0, 0, 0, 70))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 15, 15)

    def open_for(self, metric, main):
        self.cur_metric = metric
        self._main = main
        self._last = None
        self._title.setText(TITLES.get(metric, metric.upper()))
        self._body.setText(f"<span style='color:{DIM}'>{PLACEHOLDER.get(metric, '…')}</span>")
        self._resize_to_content(grow_only=False)
        self._place_beside(main)
        self.show()
        self.raise_()
        self.activateWindow()

    def set_rows(self, metric, rows):
        if metric != self.cur_metric:
            return
        h = FORMATTERS[metric](rows)
        if h == self._last:
            return
        self._last = h
        self._body.setText(h)
        self._resize_to_content()   # grow-only, no reposition — avoids 2s jitter

    def _resize_to_content(self, grow_only=True):
        self._body.adjustSize()
        inner = min(440, 12 + self._title.sizeHint().height() + 8 + self._body.sizeHint().height() + 12)
        self.setFixedHeight(max(self.height(), inner) if grow_only else inner)
        scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        bottom = scr.availableGeometry().bottom() - self.height()   # keep growth on-screen (y only)
        if self.y() > bottom:
            self.move(self.x(), max(0, bottom))

    def _place_beside(self, main):
        g = main.frameGeometry()
        scr = QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry()
        x = g.right() + 8
        if x + self.width() > avail.right():
            x = g.left() - 8 - self.width()          # flip to the left side
        x = max(avail.left(), min(x, avail.right() - self.width()))
        y = max(avail.top(), min(g.top(), avail.bottom() - self.height()))
        self.move(x, y)

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self.close()
        return super().event(e)

    def closeEvent(self, e):
        self.closed_at = time.monotonic()
        self.closed.emit()
        super().closeEvent(e)
