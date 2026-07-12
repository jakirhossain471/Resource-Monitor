"""Sensor backends + polling thread.

One QThread reads psutil + NVML every tick (1s) and LibreHardwareMonitor temps
every 3rd tick, emitting a plain dict. All backend init/read/teardown happens on
this worker thread (CLR/NVML/WMI handles have thread affinity — see council fix 1).
Every backend degrades to None when absent; the widget renders None as "N/A".
"""
import ctypes
import os
import sys
import threading
import time

import psutil
from PySide6.QtCore import QThread, Signal

# Frozen (PyInstaller) extracts bundled data under sys._MEIPASS; else run from source.
_BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(_BASE, "lib")
LHM_DLL = os.path.join(LIB, "LibreHardwareMonitorLib.dll")
# LHM's net472 build binds these NuGet assemblies at Open(); vendored in lib/.
LHM_DEPS = ("System.Memory", "System.Buffers", "System.Numerics.Vectors",
            "System.Runtime.CompilerServices.Unsafe")
FAST_KEYS = ("cpu", "ram_pct", "ram_used", "ram_total", "net_up", "net_down",
             "disk_read", "disk_write",
             "gpu_util", "gpu_mem_used", "gpu_mem_total", "gpu_temp")
TEMP_KEYS = ("cpu_temp", "mobo_temp", "igpu_util")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --- per-source drill-down gatherers (worker thread only; return plain data) ---
# Rows are plain dicts; the panel formats/escapes them on the GUI thread.
DETAIL_CAP = 15
NET_CAP = 10


def _uptime(create_time):
    s = max(0, int(time.time() - create_time))
    d, s = divmod(s, 86400)
    h, m = divmod(s // 60, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def gather_cpu():
    # process_iter reuses its internal Process objects across calls, so cpu_percent()
    # is a real since-last-call delta. First call after a (re)open primes to ~0 — the
    # loop discards that first gather. Aggregated by name (Task Manager convention).
    ncpu = psutil.cpu_count() or 1
    agg = {}
    for p in psutil.process_iter(["name", "cpu_percent"]):
        c = p.info["cpu_percent"]
        if c is None or p.pid == 0:   # pid 0 = System Idle Process (idle time, not usage)
            continue
        e = agg.setdefault(p.info["name"] or f"pid {p.pid}", [0.0, 0])
        e[0] += c
        e[1] += 1
    rows = [{"name": n, "n": v[1], "cpu": v[0] / ncpu} for n, v in agg.items()]
    rows.sort(key=lambda r: -r["cpu"])
    return rows[:DETAIL_CAP]


def gather_ram():
    agg = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        mi = p.info["memory_info"]
        if not mi:
            continue
        e = agg.setdefault(p.info["name"] or f"pid {p.pid}", [0, 0])
        e[0] += mi.rss
        e[1] += 1
    rows = [{"name": n, "rss": v[0], "n": v[1]} for n, v in agg.items()]
    rows.sort(key=lambda r: -r["rss"])
    return rows[:DETAIL_CAP]


def gather_gpu(nv, pynvml):
    if nv is None:
        return []
    best = {}   # pid -> max VRAM bytes (0 if the driver doesn't report per-proc VRAM)
    for fn in ("nvmlDeviceGetGraphicsRunningProcesses_v3", "nvmlDeviceGetComputeRunningProcesses_v3"):
        try:
            for x in getattr(pynvml, fn)(nv):
                mem = x.usedGpuMemory or 0
                if mem >= best.get(x.pid, -1):
                    best[x.pid] = mem
        except Exception:
            pass
    # Consumer GeForce often reports 0 VRAM per process. If any real values exist show
    # only those (clean, Task-Manager-like); otherwise fall back to the GPU process list.
    withmem = {p: m for p, m in best.items() if m > 0}
    use = withmem or best
    rows = []
    for pid, mem in use.items():
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = f"pid {pid}"
        rows.append({"name": name, "pid": pid, "vram": mem or None})
    rows.sort(key=lambda r: -(r["vram"] or 0))
    return rows[:DETAIL_CAP]


def gather_net():
    try:
        conns = psutil.net_connections("inet")
    except Exception:
        return None   # AccessDenied / OSError => panel shows "unavailable"
    groups = {}
    for c in conns:
        groups.setdefault(c.pid, []).append(c)
    rows, unknown = [], 0
    for pid, cs in groups.items():
        if pid is None:
            unknown += len(cs)
            continue
        try:
            p = psutil.Process(pid)
            ct = p.create_time()
            name = p.name()
            up = _uptime(ct) if ct > 1e9 else "—"   # pid 0/4 have epoch-0 create_time
        except Exception:
            name, up = f"pid {pid}", "—"
        eps = [(f"{c.raddr.ip}:{c.raddr.port}", c.status) for c in cs if c.raddr]
        rows.append({"name": name, "pid": pid, "uptime": up, "total": len(cs),
                     "conns": eps[:5], "more": max(0, len(eps) - 5)})
    rows.sort(key=lambda r: -r["total"])
    rows = rows[:NET_CAP]
    if unknown:
        rows.append({"name": "unknown", "pid": None, "uptime": "—",
                     "total": unknown, "conns": [], "more": 0})
    return rows


_GATHERS = {"cpu": gather_cpu, "ram": gather_ram, "net": gather_net}   # gpu/disk take backends


# --- per-process disk I/O rate via Windows PDH (SystemInformer-style counter source) ---
# PDH's \Process(*)\IO Read+Write Bytes/sec are ready-computed rates, so no manual
# per-pid delta bookkeeping. Instances arrive as base name + "#N" for duplicates;
# aggregated by name (Task Manager convention), matching gather_cpu/gather_ram.
# NOTE: these counters count ALL I/O (disk + network + pipe/device), not disk-only —
# Task Manager's true "Disk" column uses ETW. This is a light, dependency-free proxy.
try:
    import win32pdh
except Exception:
    win32pdh = None


class DiskIoBackend:
    _COUNTERS = ("IO Read Bytes/sec", "IO Write Bytes/sec")

    def __init__(self):
        self._query = None
        self._counters = []

    def open(self):
        # Returns True on success. Degrades silently (query stays None) when pywin32
        # is absent (e.g. a frozen build without it) or PDH refuses the counter path.
        if win32pdh is None:
            return False
        try:
            q = win32pdh.OpenQuery()
            self._counters = [
                win32pdh.AddCounter(q, win32pdh.MakeCounterPath(
                    (None, "Process", "*", None, -1, name)))
                for name in self._COUNTERS
            ]
            self._query = q
            return True
        except Exception:
            self._query, self._counters = None, []
            return False

    def gather(self):
        # One CollectQueryData per call. PDH rate counters need two samples spaced in
        # time, so the caller discards the first (prime) result — same as the cpu path.
        if self._query is None:
            return []
        try:
            win32pdh.CollectQueryData(self._query)
        except Exception:
            return []
        agg = {}   # base name -> [bytes/sec, {instance names}]
        for c in self._counters:
            try:
                data = win32pdh.GetFormattedCounterArray(c, win32pdh.PDH_FMT_DOUBLE)
            except Exception:
                continue   # PDH_INVALID_DATA on a tick with no instances — skip counter
            for inst, val in data.items():
                base = inst.split("#")[0]
                if base in ("_Total", "Idle"):
                    continue
                e = agg.setdefault(base, [0.0, set()])
                e[0] += val or 0.0
                e[1].add(inst)
        rows = [{"name": n, "disk": v[0], "n": len(v[1])}
                for n, v in agg.items() if v[0] > 0]
        rows.sort(key=lambda r: -r["disk"])
        return rows[:DETAIL_CAP]

    def close(self):
        if self._query is not None:
            try:
                win32pdh.CloseQuery(self._query)
            except Exception:
                pass
        self._query = None


class SensorThread(QThread):
    reading = Signal(dict)
    detail = Signal(str, int, list)   # (metric, request-token, rows)

    def __init__(self, temps_enabled=True, parent=None):
        super().__init__(parent)
        self._temps_enabled = temps_enabled
        self._stop = threading.Event()
        self._nv = None            # pynvml handle
        self._pynvml = None        # pynvml module (set once import succeeds)
        self._disk = None          # DiskIoBackend (PDH), None when unavailable
        self._lhm = None           # LHM Computer
        self._lhm_hw = []          # hardware objects to Update() each temp tick
        self._lhm_sensors = {}     # role -> ISensor
        self._net0 = None          # (bytes_sent, bytes_recv, monotonic)
        self._disk0 = None         # (read_bytes, write_bytes, monotonic) system-wide baseline
        self._temps = dict.fromkeys(TEMP_KEYS)  # carried forward between temp ticks
        self._detail_req = None    # (metric, token) written by GUI thread — atomic rebind
        self._detail_active = (None, 0)
        self._detail_primed = False

    def set_detail(self, metric, token):
        # Called on the GUI thread. A single attribute rebind is atomic under the GIL,
        # so the worker reads a consistent (metric, token) with no lock.
        self._detail_req = (metric, token) if metric else None

    def stop(self):
        self._stop.set()

    # --- backend init (worker thread) ---
    def _init_psutil(self):
        psutil.cpu_percent(percpu=False)  # prime; next calls are non-blocking diffs
        io = psutil.net_io_counters()
        self._net0 = (io.bytes_sent, io.bytes_recv, time.monotonic()) if io else (0, 0, time.monotonic())
        dio = psutil.disk_io_counters()
        self._disk0 = (dio.read_bytes, dio.write_bytes, time.monotonic()) if dio else None

    def _init_nvml(self):
        try:
            import pynvml
            self._pynvml = pynvml
            pynvml.nvmlInit()
            self._nv = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self._nv = None
            try:
                self._pynvml.nvmlShutdown()   # unwind a half-open init
            except Exception:
                pass

    def _init_disk(self):
        b = DiskIoBackend()
        self._disk = b if b.open() else None

    def _init_lhm(self):
        if not (self._temps_enabled and os.path.exists(LHM_DLL)):
            return
        c = None
        try:
            from pythonnet import load
            load("netfx")            # must precede first `import clr`
            import clr
            for dep in LHM_DEPS:     # load deps before Open() binds them
                clr.AddReference(os.path.join(LIB, dep + ".dll"))
            clr.AddReference(LHM_DLL)
            from LibreHardwareMonitor.Hardware import Computer  # noqa: E402
            c = Computer()
            c.IsCpuEnabled = True
            c.IsGpuEnabled = True
            c.IsMotherboardEnabled = True
            c.IsStorageEnabled = False   # avoids DiskInfoToolkit dep; NVMe temp dropped
            c.Open()
            self._lhm = c
            self._disable_value_history(c)   # else LHM buffers 24h of samples => slow RAM creep
            self._enumerate_lhm()
        except Exception:
            self._lhm = None
            if c is not None:                # don't orphan an opened Computer (WinRing0 handle)
                try:
                    c.Close()
                except Exception:
                    pass

    @staticmethod
    def _disable_value_history(c):
        from System import TimeSpan
        for hw in c.Hardware:
            for s in hw.Sensors:
                s.ValuesTimeWindow = TimeSpan.Zero
            for sub in hw.SubHardware:
                for s in sub.Sensors:
                    s.ValuesTimeWindow = TimeSpan.Zero

    def _enumerate_lhm(self):
        # Cache sensor refs by role once; update parents (+ their sub-hardware) per tick.
        from LibreHardwareMonitor.Hardware import HardwareType, SensorType
        used = set()
        for hw in self._lhm.Hardware:
            hw.Update()
            for sub in hw.SubHardware:
                sub.Update()
            t = hw.HardwareType
            if t == HardwareType.Cpu and "cpu_temp" not in self._lhm_sensors:
                s = self._pick_temp(hw, ("CPU Package", "Package", "Core Max", "Core Average", "Tctl", "Tdie"))
                if s:
                    self._lhm_sensors["cpu_temp"] = s
                    used.add(hw)
            elif t == HardwareType.Motherboard:
                for sub in hw.SubHardware:
                    s = self._pick_temp(sub, ())
                    if s and "mobo_temp" not in self._lhm_sensors:
                        self._lhm_sensors["mobo_temp"] = s
                        used.add(hw)
            elif t == HardwareType.GpuIntel and "igpu_util" not in self._lhm_sensors:
                for sensor in hw.Sensors:
                    if sensor.SensorType == SensorType.Load and (sensor.Name or "").startswith(("D3D 3D", "GPU Core")):
                        self._lhm_sensors["igpu_util"] = sensor
                        used.add(hw)
                        break
        self._lhm_hw = list(used)

    @staticmethod
    def _pick_temp(hw, prefer):
        from LibreHardwareMonitor.Hardware import SensorType
        temps = [s for s in hw.Sensors if s.SensorType == SensorType.Temperature]
        for name in prefer:
            for s in temps:
                if name in (s.Name or ""):
                    return s
        return temps[0] if temps else None

    # --- reads ---
    def _read_fast(self):
        d = dict.fromkeys(FAST_KEYS)
        d["cpu"] = psutil.cpu_percent(percpu=False)
        vm = psutil.virtual_memory()
        d["ram_pct"] = vm.percent
        d["ram_used"] = vm.used
        d["ram_total"] = vm.total
        io = psutil.net_io_counters()
        now = time.monotonic()
        if io is not None:
            s0, r0, t0 = self._net0
            dt = now - t0
            if dt > 0:
                d["net_up"] = max(0, io.bytes_sent - s0) / dt
                d["net_down"] = max(0, io.bytes_recv - r0) / dt
            self._net0 = (io.bytes_sent, io.bytes_recv, now)
        dio = psutil.disk_io_counters()
        if dio is not None and self._disk0 is not None:
            dr0, dw0, dt0 = self._disk0
            ddt = now - dt0
            if ddt > 0:
                d["disk_read"] = max(0, dio.read_bytes - dr0) / ddt
                d["disk_write"] = max(0, dio.write_bytes - dw0) / ddt
            self._disk0 = (dio.read_bytes, dio.write_bytes, now)
        if self._nv is not None:
            nv = self._pynvml
            for key, fn in (
                ("gpu_util", lambda: nv.nvmlDeviceGetUtilizationRates(self._nv).gpu),
                ("gpu_temp", lambda: nv.nvmlDeviceGetTemperature(self._nv, nv.NVML_TEMPERATURE_GPU)),
            ):
                try:
                    d[key] = fn()
                except Exception:
                    pass  # Optimus/muxless: GPU power-gated, read unavailable this tick
            try:
                m = nv.nvmlDeviceGetMemoryInfo(self._nv)
                d["gpu_mem_used"] = m.used
                d["gpu_mem_total"] = m.total
            except Exception:
                pass
        return d

    def _read_temps(self):
        if self._lhm is None:
            return
        try:
            for hw in self._lhm_hw:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
        except Exception:
            self._temps.update(dict.fromkeys(self._temps))  # show N/A, not a frozen value
            return
        for role, sensor in self._lhm_sensors.items():
            try:
                v = sensor.Value
                self._temps[role] = float(v) if v is not None else None
            except Exception:
                self._temps[role] = None  # sensor vanished (GPU sleep, hot-unplug)

    def _gather_detail(self, tick):
        req = self._detail_req              # one atomic read per tick
        metric = req[0] if req else None
        token = req[1] if req else 0
        changed = (metric, token) != self._detail_active   # open / close / switch / reopen
        if changed:
            self._detail_active = (metric, token)
            self._detail_primed = False
        if not metric:
            return
        # gather on open (instant first data) and every 2s thereafter
        if not (changed or tick % 2 == 0):
            return
        if metric == "gpu":
            rows = gather_gpu(self._nv, self._pynvml)
        elif metric == "disk":
            rows = self._disk.gather() if self._disk else []
        else:
            rows = _GATHERS[metric]()
        if metric in ("cpu", "disk") and not self._detail_primed:
            self._detail_primed = True      # first gather primes psutil/PDH — discard it
            return
        self.detail.emit(metric, token, rows if rows is not None else [])

    # --- loop ---
    def run(self):
        try:
            self._init_psutil()
            self._init_nvml()
            self._init_disk()
            self._init_lhm()
            tick = 0
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    d = self._read_fast()
                    if tick % 3 == 0:
                        self._read_temps()
                    d.update(self._temps)
                    self.reading.emit(d)   # headline out BEFORE detail work — never waits
                except Exception:
                    pass  # a transient sensor raise must not kill the loop
                try:
                    self._gather_detail(tick)
                except Exception:
                    pass  # detail failure must not kill the headline loop
                tick += 1
                # deadline wait: absorb gather time so the 1s cadence holds; floor 0.3s
                # keeps a slow gather (busy box) from spinning the loop.
                self._stop.wait(max(0.3, 1.0 - (time.monotonic() - t0)))
        finally:
            if self._disk is not None:
                self._disk.close()
            if self._nv is not None:
                try:
                    self._pynvml.nvmlShutdown()
                except Exception:
                    pass
            if self._lhm is not None:
                try:
                    self._lhm.Close()
                except Exception:
                    pass
