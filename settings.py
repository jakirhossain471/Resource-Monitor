"""Widget settings: load/save JSON in %APPDATA%\\ResourceMonitor."""
import json
import os

DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ResourceMonitor")
PATH = os.path.join(DIR, "settings.json")

DEFAULTS = {
    "pos": None,            # [x, y] or None => center-ish default
    "opacity": 0.92,        # window opacity 0..1
    "show": {"cpu": True, "ram": True, "gpu": True, "disk": True, "net": True, "temp": True},
    "temps_enabled": True,  # gate lazy CLR load
    "click_through": False,
}


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        return dict(DEFAULTS)
    s = dict(DEFAULTS)
    s.update(data)
    show = data.get("show")
    s["show"] = {**DEFAULTS["show"], **(show if isinstance(show, dict) else {})}
    try:
        s["opacity"] = min(1.0, max(0.3, float(s["opacity"])))
    except (TypeError, ValueError):
        s["opacity"] = DEFAULTS["opacity"]
    pos = s.get("pos")                       # corrupt pos would crash QPoint(*pos) at every launch
    if not (isinstance(pos, list) and len(pos) == 2 and all(isinstance(v, (int, float)) for v in pos)):
        s["pos"] = None
    return s


def save(s):
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass  # non-fatal: a widget that can't persist position still runs
