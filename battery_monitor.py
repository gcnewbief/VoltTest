#!/usr/bin/env python3
"""
VoltCheck - Laptop battery drain-test & health monitor.

Live data  : CallNtPowerInformation / GetSystemPowerStatus via ctypes (no deps)
Static data: powercfg /batteryreport HTML + WMI root\\wmi fallback (one-shot)
UI         : customtkinter + embedded matplotlib graph
Report     : self-contained HTML (inline SVG chart) + CSV log export
Demo       : simulated drain for machines without a battery (python battery_monitor.py --demo)
"""

import ctypes
import csv
import html
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime

import customtkinter as ctk
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

# ---------------------------------------------------------------- constants

APP_NAME = "VoltCheck"
SAMPLE_MS = 2000               # poll interval
GRAPH_MAX_POINTS = 5400        # ~3h of data at 2s

BG        = "#0a0e0a"          # near-black, green tint
PANEL     = "#101510"
PANEL_ALT = "#0c110c"
BORDER    = "#1d2b1d"
GREEN     = "#00e676"
GREEN_DIM = "#1b5e20"
TEAL      = "#69f0ae"
TEXT      = "#d9ffe4"
TEXT_DIM  = "#6f8f76"
ORANGE    = "#ffab40"
RED       = "#ff5252"
GRID      = "#1c281c"

HEALTH_GOOD = 90.0
HEALTH_WORN = 60.0
DROP_FAULT_PCT = 3            # % drop per sample that suggests a dead cell
SPIKE_W = 55                  # sustained discharge rate worth flagging (W)

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
LOG_DIR = os.path.join(APP_DIR, "logs")
REPORT_DIR = os.path.join(APP_DIR, "reports")


# ---------------------------------------------------------------- ctypes data

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class SYSTEM_BATTERY_STATE(ctypes.Structure):
    # 4 x BOOLEAN + Spare1[3] + Tag = 8 bytes, then ULONGs from offset 8
    _fields_ = [
        ("AcOnLine", ctypes.c_bool),
        ("BatteryPresent", ctypes.c_bool),
        ("Charging", ctypes.c_bool),
        ("Discharging", ctypes.c_bool),
        ("Spare1", ctypes.c_bool * 3),
        ("Tag", ctypes.c_byte),
        ("MaxCapacity", ctypes.c_ulong),        # full-charged capacity, mWh
        ("RemainingCapacity", ctypes.c_ulong),  # mWh
        ("RateOfCharge", ctypes.c_int32),       # mW; + charging, - discharging
        ("EstimatedTime", ctypes.c_ulong),      # seconds, 0xFFFFFFFF = unknown
        ("DefaultAlert1", ctypes.c_ulong),
        ("DefaultAlert2", ctypes.c_ulong),
    ]


@dataclass
class LiveReading:
    present: bool = False
    plugged: bool = False
    charging: bool = False
    discharging: bool = False
    percent: int = -1                       # -1 = unknown
    remaining_mwh: int = 0
    full_charge_mwh: int = 0
    rate_mw: int = 0                        # signed: + charging, - discharging
    voltage_mv: int = 0
    est_secs: int = -1                      # seconds to empty (discharging)


def _read_power_status() -> LiveReading:
    """Live battery data straight from powrprof/kernel32 - no WMI needed."""
    r = LiveReading()
    try:
        sps = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            r.present = sps.BatteryFlag != 128 and sps.BatteryLifePercent != 255
            r.plugged = sps.ACLineStatus == 1
            r.percent = sps.BatteryLifePercent if sps.BatteryLifePercent <= 100 else -1
            r.charging = bool(sps.BatteryFlag & 8)
    except Exception:
        pass
    try:
        sbs = SYSTEM_BATTERY_STATE()
        # CallNtPowerInformation(InformationLevel=5 SystemBatteryState)
        rc = ctypes.windll.powrprof.CallNtPowerInformation(
            5, None, 0, ctypes.byref(sbs), ctypes.sizeof(sbs))
        if rc == 0:
            r.present = r.present or sbs.BatteryPresent
            r.plugged = r.plugged or bool(sbs.AcOnLine)
            r.charging = r.charging or bool(sbs.Charging)
            r.discharging = bool(sbs.Discharging)
            # sentinel values: 0xFFFFFFFF capacity unknown / 0x80000000 rate unknown
            if sbs.RemainingCapacity != 0xFFFFFFFF:
                r.remaining_mwh = int(sbs.RemainingCapacity)
            if sbs.MaxCapacity != 0xFFFFFFFF:
                r.full_charge_mwh = int(sbs.MaxCapacity)
            r.rate_mw = 0 if sbs.RateOfCharge == -2147483648 else int(sbs.RateOfCharge)
            r.est_secs = -1 if sbs.EstimatedTime == 0xFFFFFFFF else int(sbs.EstimatedTime)
    except Exception:
        pass
    # reconcile: some firmware leaves the flags stale - trust AC line + charging bit
    if r.present and not r.plugged and not r.charging:
        r.discharging = True
    return r


def _run_ps(script: str, timeout: int = 20) -> str:
    """Run a PowerShell snippet, return stdout (never raises)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout or ""
    except Exception:
        return ""


def _fetch_static_wmi() -> dict:
    """One-shot WMI probe for design capacity / cycle count / identity.
    Returns arrays so multi-battery systems are fully covered."""
    script = (
        "$d = @(Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData -ErrorAction SilentlyContinue);"
        "$c = @(Get-CimInstance -Namespace root/wmi -ClassName BatteryCycleCount -ErrorAction SilentlyContinue);"
        "$b = @(Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue);"
        "[pscustomobject]@{"
        "  designed  = @($d | ForEach-Object { $_.DesignedCapacity });"
        "  chemistry = @($d | ForEach-Object { $_.Chemistry });"
        "  serial    = @($d | ForEach-Object { $_.SerialNumber });"
        "  mfg       = @($d | ForEach-Object { $_.ManufactureName });"
        "  devname   = @($d | ForEach-Object { $_.DeviceName });"
        "  cycles    = @($c | ForEach-Object { $_.CycleCount });"
        "  win_name  = @($b | ForEach-Object { $_.Name });"
        "  win_devid = @($b | ForEach-Object { $_.DeviceID });"
        "} | ConvertTo-Json"
    )
    try:
        return json.loads(_run_ps(script)) or {}
    except Exception:
        return {}


_PWR_LABELS = ("NAME", "MANUFACTURER", "SERIAL NUMBER", "CHEMISTRY",
               "DESIGN CAPACITY", "FULL CHARGE CAPACITY", "CYCLE COUNT")


def _fetch_powercfg_static() -> dict:
    """Parse `powercfg /batteryreport` HTML -> one dict per installed battery."""
    path = os.path.join(os.environ.get("TEMP", APP_DIR), "voltcheck_pwr.html")
    try:
        subprocess.run(["powercfg", "/batteryreport", "/output", path],
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return {"batteries": []}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    # isolate the "Installed batteries" section (each battery is its own table)
    sec = re.search(r"Installed batteries\s*</h2>(.*?)(?=<h2>)", text, re.S)
    text = sec.group(1) if sec else text
    pairs = re.findall(
        r'<span class="label">\s*(' + "|".join(_PWR_LABELS) +
        r")\s*</span></td><td[^>]*>\s*([^<]*?)\s*</td>", text)

    def mwh(s):
        m = re.search(r"([\d,]+)\s*mWh", s or "")
        return int(m.group(1).replace(",", "")) if m else 0

    batteries, cur = [], {}
    for lbl, val in pairs:
        if lbl == "NAME" and cur:          # a new battery table begins
            batteries.append(cur)
            cur = {}
        cur[lbl] = html.unescape(val.strip())
    if cur:
        batteries.append(cur)

    for b in batteries:
        b["design_mwh"] = mwh(b.get("DESIGN CAPACITY"))
        b["full_charge_mwh"] = mwh(b.get("FULL CHARGE CAPACITY"))
    return {"batteries": batteries}


@dataclass
class StaticInfo:
    # aggregates across ALL installed batteries
    name: str = ""
    manufacturer: str = ""
    serial: str = ""
    chemistry: str = ""
    design_mwh: int = 0
    full_charge_mwh: int = 0
    cycle_count: str = "-"
    batteries: list = field(default_factory=list)  # per-battery dicts

    @property
    def count(self) -> int:
        return max(len(self.batteries), 1) if self.batteries or self.design_mwh else 0

    @property
    def health_pct(self) -> float:
        if self.design_mwh and self.full_charge_mwh:
            return self.full_charge_mwh / self.design_mwh * 100.0
        return -1.0


def fetch_static_info() -> StaticInfo:
    info = StaticInfo()
    batts = _fetch_powercfg_static().get("batteries", [])
    info.batteries = [{
        "name": b.get("NAME", ""), "manufacturer": b.get("MANUFACTURER", ""),
        "serial": b.get("SERIAL NUMBER", ""), "chemistry": b.get("CHEMISTRY", ""),
        "design_mwh": b.get("design_mwh", 0),
        "full_charge_mwh": b.get("full_charge_mwh", 0),
        "cycle_count": b.get("CYCLE COUNT", "-"),
    } for b in batts]

    info.design_mwh = sum(b["design_mwh"] for b in info.batteries)
    info.full_charge_mwh = sum(b["full_charge_mwh"] for b in info.batteries)
    if info.batteries:
        first = info.batteries[0]
        info.name = " + ".join(b["name"] or "?" for b in info.batteries) \
            if len(info.batteries) > 1 else first["name"]
        info.manufacturer = first["manufacturer"]
        info.serial = " / ".join(b["serial"] for b in info.batteries if b["serial"])
        info.chemistry = first["chemistry"]
        info.cycle_count = " / ".join(b["cycle_count"] for b in info.batteries) or "-"

    wmi = _fetch_static_wmi()  # fill gaps (BatteryStaticData when present)
    if not info.design_mwh and wmi.get("designed"):
        info.design_mwh = sum(int(x) for x in wmi["designed"] if x)
    if not info.name and wmi.get("win_name"):
        info.name = " + ".join(str(x) for x in wmi["win_name"] if x)
    if not info.manufacturer and wmi.get("mfg"):
        info.manufacturer = str(wmi["mfg"][0]).strip()
    if not info.serial and wmi.get("serial"):
        info.serial = " / ".join(str(x).strip() for x in wmi["serial"] if x)
    if not info.chemistry and wmi.get("chemistry"):
        info.chemistry = str(wmi["chemistry"][0]).strip()
    if info.cycle_count in ("-", "") and wmi.get("cycles"):
        info.cycle_count = " / ".join(str(x) for x in wmi["cycles"] if x is not None)
    return info


# ---------------------------------------------------------------- demo provider

class DemoBattery:
    """Simulated drain incl. a fake dead-cell drop at ~65% (runs 60x speed)."""

    SPEED = 60

    def __init__(self):
        self.t0 = time.time()
        self.capacity = 48000
        self.remaining = float(self.capacity)
        self.dropped = False

    def static(self) -> StaticInfo:
        return StaticInfo(name="DemoCell X1", manufacturer="VoltCheck Sim",
                          serial="SIM-0001", chemistry="Li-ion",
                          design_mwh=56000, full_charge_mwh=48000,
                          cycle_count="312")

    def read(self, dt: float) -> LiveReading:
        dt *= self.SPEED
        rate = -(15000 + 4000 * math.sin(time.time() / 30) + random.uniform(-1500, 1500))
        self.remaining += rate * dt / 3600  # mWh over dt
        pct = self.remaining / self.capacity * 100
        if not self.dropped and pct < 65:
            self.remaining -= self.capacity * 0.06       # sudden 6% drop
            self.dropped = True
        pct = max(0.0, self.remaining / self.capacity * 100)
        return LiveReading(present=True, plugged=False, discharging=True,
                           percent=int(round(pct)),
                           remaining_mwh=int(self.remaining),
                           full_charge_mwh=self.capacity,
                           rate_mw=int(rate), voltage_mv=int(11000 + pct * 12),
                           est_secs=int(self.remaining / (-rate) * 3600))


# ---------------------------------------------------------------- recording

@dataclass
class Sample:
    ts: float
    percent: int
    remaining_mwh: int
    rate_mw: int
    voltage_mv: int
    plugged: bool
    charging: bool


@dataclass
class Event:
    ts: float
    level: str      # "ok" | "warn" | "fault" | "info"
    text: str


class Recorder:
    def __init__(self):
        self.samples: list[Sample] = []
        self.events: list[Event] = []
        self.recording = False
        self.csv_path = ""
        self._csv_file = None
        self._csv_writer = None
        self.start_ts = 0.0

    def start(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.samples.clear()
        self.events.clear()
        self.recording = True
        self.start_ts = time.time()
        self.csv_path = os.path.join(
            LOG_DIR, "drain_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
        self._csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(
            ["timestamp", "iso_time", "percent", "remaining_mwh",
             "rate_mw", "voltage_mv", "plugged", "charging"])

    def stop(self):
        self.recording = False
        self._csv_writer = None
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None

    def add(self, s: Sample):
        self.samples.append(s)
        if self._csv_writer:
            self._csv_writer.writerow(
                [f"{s.ts:.3f}", datetime.fromtimestamp(s.ts).isoformat(timespec="seconds"),
                 s.percent, s.remaining_mwh, s.rate_mw, s.voltage_mv,
                 int(s.plugged), int(s.charging)])
            self._csv_file.flush()


# ---------------------------------------------------------------- report

def _svg_chart(samples: list[Sample], w=920, h=260) -> str:
    if len(samples) < 2:
        return '<p style="color:#6f8f76">Not enough samples to chart.</p>'
    pad_l, pad_r, pad_t, pad_b = 46, 46, 18, 30
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    t0, t1 = samples[0].ts, samples[-1].ts
    span = max(t1 - t0, 1)
    rates = [abs(s.rate_mw) for s in samples]
    rmax = max(max(rates), 1)

    def x(ts): return pad_l + (ts - t0) / span * iw
    def yp(p): return pad_t + (1 - p / 100) * ih
    def yr(mw): return pad_t + (1 - mw / rmax) * ih

    parts = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
             f'xmlns="http://www.w3.org/2000/svg" style="background:{PANEL_ALT};'
             f'border:1px solid {BORDER};border-radius:8px">']
    for gp in range(0, 101, 25):          # horizontal gridlines (percent)
        y = yp(gp)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
                     f'stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" fill="{TEXT_DIM}" '
                     f'font-size="10" text-anchor="end">{gp}%</text>')
    for i in range(5):                     # time labels
        ts = t0 + span * i / 4
        parts.append(f'<text x="{x(ts):.1f}" y="{h-8}" fill="{TEXT_DIM}" font-size="10" '
                     f'text-anchor="middle">{datetime.fromtimestamp(ts):%H:%M}</text>')
    pts = " ".join(f"{x(s.ts):.1f},{yp(s.percent):.1f}" for s in samples)
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{GREEN}" stroke-width="1.8"/>')
    rpts = " ".join(f"{x(s.ts):.1f},{yr(abs(s.rate_mw)):.1f}" for s in samples)
    parts.append(f'<polyline points="{rpts}" fill="none" stroke="{TEAL}" '
                 f'stroke-width="1" opacity="0.55"/>')
    parts.append(f'<text x="{w-pad_r+6}" y="{pad_t+10}" fill="{TEAL}" '
                 f'font-size="10">{rmax//1000}W</text>')
    parts.append('</svg>')
    return "".join(parts)


def generate_report(rec: Recorder, st: StaticInfo, demo: bool) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    now = datetime.now()
    path = os.path.join(REPORT_DIR, f"report_{now:%Y%m%d_%H%M%S}.html")
    samples = rec.samples
    dur = (samples[-1].ts - samples[0].ts) if len(samples) > 1 else 0
    pct_first = samples[0].percent if samples else 0
    pct_last = samples[-1].percent if samples else 0
    discharged = [s for s in samples if not s.plugged and s.rate_mw < 0]
    avg_w = (sum(-s.rate_mw for s in discharged) / len(discharged) / 1000) if discharged else 0
    drain_per_hr = ((pct_first - pct_last) / (dur / 3600)) if dur > 60 else 0
    health = st.health_pct
    h_color = GREEN if health < 0 or health >= HEALTH_GOOD else (ORANGE if health >= HEALTH_WORN else RED)
    faults = [e for e in rec.events if e.level == "fault"]
    warns = [e for e in rec.events if e.level == "warn"]
    verdict, v_color = ("PASS — no anomalies detected", GREEN)
    if health >= 0 and health < HEALTH_WORN:
        verdict, v_color = "FAIL — battery health critically low, replacement recommended", RED
    elif faults:
        verdict, v_color = f"FAIL — {len(faults)} fault(s) detected (possible dead cell / gauge fault)", RED
    elif warns or (health >= 0 and health < HEALTH_GOOD):
        verdict, v_color = "CAUTION — battery shows wear or warnings, monitor closely", ORANGE

    def card(title, value, color=TEXT):
        return (f'<div class="card"><div class="ct">{html.escape(title)}</div>'
                f'<div class="cv" style="color:{color}">{html.escape(str(value))}</div></div>')

    ev_rows = "".join(
        f'<tr><td>{datetime.fromtimestamp(e.ts):%H:%M:%S}</td>'
        f'<td class="{e.level}">{e.level.upper()}</td>'
        f'<td>{html.escape(e.text)}</td></tr>'
        for e in rec.events) or '<tr><td colspan="3" style="color:#6f8f76">No events recorded.</td></tr>'

    batt_rows = ""
    if len(st.batteries) > 1:
        for i, b in enumerate(st.batteries, 1):
            bh = (b["full_charge_mwh"] / b["design_mwh"] * 100) if b["design_mwh"] else -1
            bc = GREEN if bh < 0 or bh >= HEALTH_GOOD else (ORANGE if bh >= HEALTH_WORN else RED)
            batt_rows += (
                f'<tr><td>Battery {i}</td><td>{html.escape(b["name"] or "-")}</td>'
                f'<td>{html.escape(b["serial"] or "-")}</td>'
                f'<td>{b["design_mwh"]:,} mWh</td><td>{b["full_charge_mwh"]:,} mWh</td>'
                f'<td>{html.escape(str(b["cycle_count"]))}</td>'
                f'<td style="color:{bc};font-weight:700">'
                f'{f"{bh:.0f}%" if bh >= 0 else "n/a"}</td></tr>')
        batt_rows = ("<h2>Per-battery detail</h2><table><tr><th>#</th><th>Name</th>"
                     "<th>Serial</th><th>Design</th><th>Full charge</th>"
                     "<th>Cycles</th><th>Health</th></tr>" + batt_rows + "</table>")

    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>VoltCheck Battery Report {now:%Y-%m-%d %H:%M}</title><style>
body{{background:{BG};color:{TEXT};font-family:'Segoe UI',Arial,sans-serif;margin:32px}}
h1{{color:{GREEN};margin-bottom:4px}} h2{{color:{TEAL};border-bottom:1px solid {BORDER};padding-bottom:6px}}
.meta{{color:{TEXT_DIM};margin-bottom:24px}}
.cards{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.card{{background:{PANEL};border:1px solid {BORDER};border-radius:8px;padding:14px 20px;min-width:160px}}
.ct{{color:{TEXT_DIM};font-size:11px;text-transform:uppercase;letter-spacing:1px}}
.cv{{font-size:22px;font-weight:600;margin-top:4px}}
table{{border-collapse:collapse;width:100%;margin-top:8px}}
td,th{{border:1px solid {BORDER};padding:6px 10px;text-align:left;font-size:13px}}
th{{background:{PANEL};color:{TEAL}}} td{{background:{PANEL_ALT}}}
.ok{{color:{GREEN}}}.warn{{color:{ORANGE}}}.fault{{color:{RED}}}.info{{color:{TEAL}}}
.verdict{{font-size:20px;font-weight:700;padding:14px 20px;border-radius:8px;
background:{PANEL};border:1px solid {BORDER};margin:20px 0}}
</style></head><body>
<h1>VoltCheck Battery Report{' <span style="color:#ffab40;font-size:14px">[DEMO DATA]</span>' if demo else ''}</h1>
<div class="meta">Generated {now:%Y-%m-%d %H:%M:%S}</div>
<div class="verdict" style="color:{v_color}">VERDICT: {html.escape(verdict)}</div>
<h2>Battery{' (aggregate — ' + str(len(st.batteries)) + ' installed)' if len(st.batteries) > 1 else ''}</h2><div class="cards">
{card("Name", st.name or "-")}{card("Manufacturer", st.manufacturer or "-")}
{card("Serial", st.serial or "-")}{card("Chemistry", st.chemistry or "-")}
{card("Cycle count", st.cycle_count)}
</div><div class="cards">
{card("Design capacity", f"{st.design_mwh:,} mWh" if st.design_mwh else "-")}
{card("Full charge capacity", f"{st.full_charge_mwh:,} mWh" if st.full_charge_mwh else "-")}
{card("Health", f"{health:.1f}%" if health >= 0 else "n/a", h_color)}
</div>
{batt_rows}
<h2>Drain test</h2><div class="cards">
{card("Duration", f"{int(dur//3600)}h {int(dur%3600//60)}m {int(dur%60)}s")}
{card("Start / end charge", f"{pct_first}% → {pct_last}%")}
{card("Avg discharge rate", f"{avg_w:.1f} W")}
{card("Drain rate", f"{drain_per_hr:.1f} %/hr")}
{card("Samples", len(samples))}
{card("Faults / warnings", f"{len(faults)} / {len(warns)}", RED if faults else (ORANGE if warns else GREEN))}
</div>
<h2>Charge over time</h2>{_svg_chart(samples)}
<h2>Event log</h2>
<table><tr><th>Time</th><th>Level</th><th>Event</th></tr>{ev_rows}</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


# ---------------------------------------------------------------- UI

def fmt_dur(secs: float) -> str:
    secs = int(secs)
    return f"{secs//3600}:{secs%3600//60:02d}:{secs%60:02d}"


class App(ctk.CTk):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.title(APP_NAME)
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=BG)
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.wm_geometry(f"{min(1180, sw - 60)}x{min(980, sh - 70)}+20+20")
        self.minsize(900, 560)

        self.demo = demo
        self.rec = Recorder()
        self.demo_batt = DemoBattery() if demo else None
        self.static = StaticInfo()
        self.last_sample: Sample | None = None
        self.last_ts = 0.0
        self.charge_stall_since = 0.0
        self._report_busy = False
        self._voltages: list[int] = []
        self._wmi_packs: list = []
        self._drain_unlocked = False
        self._saved_power: dict = {}
        self._ui_queue: queue.Queue = queue.Queue()  # threads -> main thread

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("info", f"{APP_NAME} started" + (" [DEMO MODE]" if demo else ""))
        threading.Thread(target=self._load_static, daemon=True).start()
        if not self.demo_batt:
            threading.Thread(target=self._poll_wmi, daemon=True).start()
        self.after(300, self._tick)

    # ---------------- layout

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)   # body absorbs shrink, top bar fixed

        # top bar
        top = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=44)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_propagate(False)
        ctk.CTkLabel(top, text="⚡ VOLTCHECK", text_color=GREEN,
                     font=("Consolas", 20, "bold")).pack(side="left", padx=16)
        self.rec_lbl = ctk.CTkLabel(top, text="● IDLE", text_color=TEXT_DIM,
                                    font=("Consolas", 14, "bold"))
        self.rec_lbl.pack(side="right", padx=16)
        self.clock_lbl = ctk.CTkLabel(top, text="", text_color=TEXT_DIM,
                                      font=("Consolas", 12))
        self.clock_lbl.pack(side="right", padx=8)

        # bottom: content row + log
        body = ctk.CTkFrame(self, fg_color=BG)
        body.grid(row=1, column=0, columnspan=2, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=8, width=280,
                            border_color=BORDER, border_width=1)
        side.grid(row=0, column=0, sticky="ns", padx=(10, 5), pady=10)
        side.grid_propagate(False)
        side.grid_rowconfigure(0, weight=1)
        side.grid_columnconfigure(0, weight=1)

        # stats scroll if the window is too short; buttons stay pinned
        inner = ctk.CTkScrollableFrame(side, fg_color="transparent",
                                       scrollbar_button_color=BORDER,
                                       scrollbar_button_hover_color=GREEN_DIM)
        inner.grid(row=0, column=0, sticky="nsew")

        self.pct_lbl = self._mk_label(inner, "–%", ("Consolas", 44, "bold"), GREEN, pady=(10, 0))
        self.state_lbl = self._mk_label(inner, "Detecting…", ("Segoe UI", 14), TEXT_DIM)
        self.rate_lbl = self._mk_label(inner, "", ("Consolas", 16, "bold"), TEAL, pady=(4, 0))
        self.eta_lbl = self._mk_label(inner, "", ("Segoe UI", 12), TEXT_DIM, pady=(0, 8))

        self._sep(inner)
        self._mk_label(inner, "BATTERY HEALTH", ("Segoe UI", 10, "bold"), TEXT_DIM)
        self.health_bar = ctk.CTkProgressBar(inner, fg_color=PANEL_ALT,
                                           progress_color=GREEN, height=14)
        self.health_bar.pack(fill="x", padx=18, pady=(6, 2))
        self.health_bar.set(0)
        self.health_lbl = self._mk_label(inner, "–", ("Consolas", 16, "bold"), GREEN)
        self.batt_info = self._mk_label(inner, "", ("Segoe UI", 11), TEXT_DIM, pady=(2, 0))
        self.cycle_lbl = self._mk_label(inner, "", ("Segoe UI", 11), TEXT_DIM)
        self._sep(inner)
        self._mk_label(inner, "CAPACITY", ("Segoe UI", 10, "bold"), TEXT_DIM)
        self.cap_rem = self._stat(inner, "Remaining")
        self.cap_fcc = self._stat(inner, "Full charge")
        self.cap_des = self._stat(inner, "Design")

        btn = ctk.CTkFrame(side, fg_color="transparent")
        btn.grid(row=1, column=0, sticky="ew", padx=14, pady=8)
        self.test_btn = ctk.CTkButton(
            btn, text="▶  START DRAIN TEST", fg_color=GREEN_DIM, hover_color="#2e7d32",
            text_color="#eaffea", font=("Segoe UI", 13, "bold"),
            command=self._toggle_test)
        self.test_btn.pack(fill="x", pady=3)
        self.cancel_btn = ctk.CTkButton(
            btn, text="✕  CANCEL (discard)", fg_color="#3a1a1a", hover_color="#5a2626",
            text_color="#ff8a80", font=("Segoe UI", 12, "bold"),
            command=self._cancel_test, state="disabled")
        self.cancel_btn.pack(fill="x", pady=3)
        self.drain_btn = ctk.CTkButton(
            btn, text="🔋  DRAIN TO EMPTY: OFF", fg_color="#1a2b1a",
            hover_color="#274427", text_color=ORANGE,
            font=("Segoe UI", 12, "bold"), command=self._toggle_drain)
        self.drain_btn.pack(fill="x", pady=3)
        ctk.CTkButton(btn, text="📄  GENERATE REPORT", fg_color="#1a2b1a",
                      hover_color="#274427", text_color=TEAL,
                      font=("Segoe UI", 12, "bold"),
                      command=self._gen_report).pack(fill="x", pady=3)
        ctk.CTkButton(btn, text="🪟  WINDOWS REPORT", fg_color="#1a2b1a",
                      hover_color="#274427", text_color=TEXT_DIM,
                      font=("Segoe UI", 12, "bold"),
                      command=self._windows_report).pack(fill="x", pady=3)

        # graph
        graphf = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=8,
                              border_color=BORDER, border_width=1)
        graphf.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)

        self.fig = Figure(figsize=(6, 4), dpi=100, facecolor=PANEL)
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        for a in (self.ax, self.ax2):
            a.set_facecolor(PANEL_ALT)
            for s in a.spines.values():
                s.set_color(BORDER)
            a.tick_params(colors=TEXT_DIM, labelsize=8)
        self.ax.set_ylim(0, 100)
        self.ax.set_ylabel("Charge %", color=GREEN, fontsize=9)
        self.ax2.set_ylabel("Power W", color=TEAL, fontsize=9)
        self.ax.grid(True, color=GRID, linewidth=0.6)
        self.ax.set_title("Charge / discharge", color=TEXT_DIM, fontsize=10, loc="left")
        self.fig.tight_layout(pad=1.2)
        (self.line_pct,) = self.ax.plot([], [], color=GREEN, lw=1.6)
        (self.line_rate,) = self.ax2.plot([], [], color=TEAL, lw=0.9, alpha=0.55)
        self.anom_scatter = self.ax.scatter([], [], color=RED, s=18, zorder=5)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graphf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

        # event log
        logf = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=8,
                            border_color=BORDER, border_width=1, height=110)
        logf.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        logf.grid_propagate(False)
        self.log_box = ctk.CTkTextbox(logf, fg_color=PANEL_ALT, text_color=TEXT,
                                      font=("Consolas", 11), corner_radius=6)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_box.configure(state="disabled")
        self._log_tags = {"info": TEAL, "ok": GREEN, "warn": ORANGE, "fault": RED}
        inner = self.log_box._textbox
        for k, c in self._log_tags.items():
            inner.tag_configure(k, foreground=c)

    def _mk_label(self, parent, text, font, color, pady=(0, 0)):
        l = ctk.CTkLabel(parent, text=text, font=font, text_color=color)
        l.pack(pady=pady)
        return l

    def _stat(self, parent, label):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", padx=18, pady=1)
        ctk.CTkLabel(f, text=label, font=("Segoe UI", 11),
                     text_color=TEXT_DIM).pack(side="left")
        v = ctk.CTkLabel(f, text="–", font=("Consolas", 12, "bold"),
                         text_color=TEXT)
        v.pack(side="right")
        return v

    def _sep(self, parent):
        ctk.CTkFrame(parent, fg_color=BORDER, height=1).pack(fill="x", padx=14, pady=6)

    # ---------------- logging / events

    def _log(self, level: str, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.rec.events.append(Event(time.time(), level, text))
        self.log_box.configure(state="normal")
        inner = self.log_box._textbox
        inner.insert("end", f"[{ts}] ", "info")
        inner.insert("end", text + "\n", level)
        inner.see("end")
        self.log_box.configure(state="disabled")

    # ---------------- static info

    def _poll_wmi(self):
        """WMI BatteryStatus per pack every ~8s - fallback for fields the
        kernel API can't give us (voltage always; rate/capacity on some kit)."""
        while True:
            try:
                out = _run_ps(
                    "Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus"
                    " -ErrorAction SilentlyContinue | Select-Object Voltage,"
                    "ChargeRate,DischargeRate,RemainingCapacity,Discharging,"
                    "Charging,PowerOnline | ConvertTo-Json -Compress")
                data = json.loads(out) if out.strip() else None
                if data:
                    packs = data if isinstance(data, list) else [data]
                    self._wmi_packs = packs
                    volts = [b["Voltage"] for b in packs
                             if isinstance(b.get("Voltage"), int) and b["Voltage"] > 0]
                    if volts:
                        self._voltages = volts
            except Exception:
                pass
            time.sleep(8)

    def _merge_wmi(self, r: LiveReading) -> LiveReading:
        """Fill gaps in the kernel reading with the last WMI poll."""
        packs = self._wmi_packs
        if not packs:
            return r
        if not r.present:
            r.present = True
        if not r.remaining_mwh:
            r.remaining_mwh = sum(b.get("RemainingCapacity") or 0 for b in packs)
        if not r.discharging:
            r.discharging = any(b.get("Discharging") for b in packs) or \
                            (r.present and not r.plugged)
        if not r.charging:
            r.charging = any(b.get("Charging") for b in packs)
        if r.rate_mw == 0:
            UNK = -2147483648
            if r.charging:
                r.rate_mw = sum(max(b.get("ChargeRate") or 0, 0)
                                for b in packs if b.get("ChargeRate") != UNK)
            elif r.discharging:
                r.rate_mw = -sum(max(b.get("DischargeRate") or 0, 0)
                                 for b in packs if b.get("DischargeRate") != UNK)
        return r

    def _load_static(self):
        if self.demo_batt:
            st = self.demo_batt.static()
        else:
            st = fetch_static_info()
        self._ui_queue.put(lambda: self._apply_static(st))

    def _apply_static(self, st: StaticInfo):
        self.static = st
        self.cap_des.configure(text=f"{st.design_mwh:,} mWh" if st.design_mwh else "unknown")
        if st.full_charge_mwh:
            self.cap_fcc.configure(text=f"{st.full_charge_mwh:,} mWh")
        h = st.health_pct
        if h >= 0:
            col = GREEN if h >= HEALTH_GOOD else (ORANGE if h >= HEALTH_WORN else RED)
            self.health_bar.set(min(h, 100) / 100)
            self.health_bar.configure(progress_color=col)
            self.health_lbl.configure(text=f"{h:.0f}%", text_color=col)
            if h < HEALTH_WORN:
                self._log("fault", f"Battery health {h:.0f}% — critically low, replace battery")
            elif h < HEALTH_GOOD:
                self._log("warn", f"Battery health {h:.0f}% — worn ({st.full_charge_mwh:,}/{st.design_mwh:,} mWh)")
            else:
                self._log("ok", f"Battery health {h:.0f}% ({st.full_charge_mwh:,}/{st.design_mwh:,} mWh)")
        else:
            self.health_lbl.configure(text="n/a", text_color=TEXT_DIM)
            self._log("warn", "Design capacity unavailable — health cannot be computed")

        # per-battery health (matters on dual-battery machines e.g. ThinkPads)
        per = []
        for i, b in enumerate(st.batteries, 1):
            bh = (b["full_charge_mwh"] / b["design_mwh"] * 100) if b["design_mwh"] else -1
            per.append(bh)
            tag = f"Battery {i} ({b['name'] or '?'})"
            if 0 <= bh < HEALTH_WORN:
                self._log("fault", f"{tag}: health {bh:.0f}% — critically low")
            elif 0 <= bh < HEALTH_GOOD:
                self._log("warn", f"{tag}: health {bh:.0f}% — worn")
        if len(per) > 1 and min(per) >= 0 and max(per) - min(per) > 25:
            self._log("warn", f"Battery imbalance: health spread "
                              f"{min(per):.0f}%–{max(per):.0f}% across units")

        if len(st.batteries) > 1:
            desc = f"{len(st.batteries)} batteries detected · {st.manufacturer or st.chemistry}"
            self._log("info", f"{len(st.batteries)} batteries: " +
                              ", ".join(f"{b['name'] or '?'} ({b['full_charge_mwh']:,}/"
                                        f"{b['design_mwh']:,} mWh)" for b in st.batteries))
        else:
            desc = " · ".join(x for x in (st.name, st.manufacturer, st.chemistry) if x)
        self.batt_info.configure(text=desc[:64])
        if len(per) > 1:
            self.cycle_lbl.configure(
                text="Per-unit health: " + " · ".join(f"B{i} {x:.0f}%" for i, x in enumerate(per, 1)))
        else:
            self.cycle_lbl.configure(
                text=f"Cycles: {st.cycle_count}   S/N: {st.serial or '-'}")

    # ---------------- polling loop

    def _tick(self):
        while True:                          # run work queued by bg threads
            try:
                self._ui_queue.get_nowait()()
            except queue.Empty:
                break
        now = time.time()
        dt = max(now - self.last_ts, 0.1) if self.last_ts else SAMPLE_MS / 1000
        self.last_ts = now
        self.clock_lbl.configure(text=datetime.now().strftime("%H:%M:%S"))

        r = self.demo_batt.read(dt) if self.demo_batt else _read_power_status()
        if not self.demo_batt:
            r = self._merge_wmi(r)
        if not r.present and not self.demo_batt:
            self.pct_lbl.configure(text="NO\nBATT", text_color=TEXT_DIM)
            self.state_lbl.configure(text="No battery detected — use --demo")
            self.after(SAMPLE_MS, self._tick)
            return

        if not r.voltage_mv and self._voltages:
            r.voltage_mv = self._voltages[0]
        s = Sample(now, r.percent, r.remaining_mwh, r.rate_mw, r.voltage_mv,
                   r.plugged, r.charging)
        if s.rate_mw == 0:
            s.rate_mw = self._derive_rate(s)   # firmware gave no rate -> measure it
        self._analyze(s)
        self.rec.add(s)          # always buffer for the live graph
        self.last_sample = s
        self._refresh_stats(r, s)
        self._refresh_graph()
        self.after(SAMPLE_MS, self._tick)

    def _derive_rate(self, s: Sample) -> int:
        """Estimate charge rate in mW from capacity deltas over ~15s window.
        Sign convention matches firmware: + charging, - discharging."""
        for old in reversed(self.rec.samples[-40:]):
            if s.ts - old.ts >= 15 and old.remaining_mwh and s.remaining_mwh:
                return int((s.remaining_mwh - old.remaining_mwh)
                           * 3600 / (s.ts - old.ts))
        return 0

    def _analyze(self, s: Sample):
        prev = self.last_sample
        if prev:
            drop = prev.percent - s.percent
            if s.percent >= 0 and prev.percent >= 0 and not s.plugged and drop >= DROP_FAULT_PCT:
                self._log("fault", f"SUDDEN DROP {prev.percent}%→{s.percent}% "
                                   f"— possible dead cell / gauge fault")
            if s.plugged != prev.plugged:
                self._log("info", "AC adapter " + ("connected" if s.plugged else "removed"))
        if s.charging:
            if self.charge_stall_since == 0:
                self.charge_stall_since = s.ts
                self._stall_base = s.percent
            elif s.ts - self.charge_stall_since > 300 and s.percent <= getattr(self, "_stall_base", 0):
                self._log("fault", "Charging for 5+ min with no % gain — charge circuit fault?")
                self.charge_stall_since = s.ts
                self._stall_base = s.percent
        else:
            self.charge_stall_since = 0
        if s.rate_mw <= -SPIKE_W * 1000 and (not prev or prev.rate_mw > -SPIKE_W * 1000):
            self._log("warn", f"High discharge rate {-s.rate_mw/1000:.0f} W — heavy load")

    def _refresh_stats(self, r: LiveReading, s: Sample):
        pct_txt = f"{s.percent}%" if s.percent >= 0 else "–%"
        col = GREEN if s.percent > 40 else (ORANGE if s.percent > 15 else RED)
        self.pct_lbl.configure(text=pct_txt, text_color=col if not r.plugged else GREEN)
        state = ("Charging ⚡" if r.charging else
                 "Discharging" if r.discharging else
                 "On AC (idle)" if r.plugged else "On battery")
        self.state_lbl.configure(text=state)
        if r.rate_mw or self._voltages:
            w = r.rate_mw / 1000
            volts = self._voltages or ([r.voltage_mv] if r.voltage_mv else [])
            vtxt = "  ·  " + " / ".join(f"{v/1000:.1f} V" for v in volts) if volts else ""
            self.rate_lbl.configure(text=f"{w:+.1f} W{vtxt}")
        if not r.plugged and s.rate_mw < 0 and s.remaining_mwh:
            eta = s.remaining_mwh / -s.rate_mw * 3600
            self.eta_lbl.configure(text=f"~{fmt_dur(eta)} remaining")
        elif r.est_secs > 0:
            self.eta_lbl.configure(text=f"~{fmt_dur(r.est_secs)} remaining")
        else:
            self.eta_lbl.configure(text="")
        if s.remaining_mwh:
            self.cap_rem.configure(text=f"{s.remaining_mwh:,} mWh")
        fcc = self.static.full_charge_mwh or r.full_charge_mwh
        if fcc:
            self.cap_fcc.configure(text=f"{fcc:,} mWh")

    def _refresh_graph(self):
        samples = self.rec.samples
        if not samples:
            self.canvas.draw_idle()
            return
        step = max(1, len(samples) // GRAPH_MAX_POINTS)
        ss = samples[::step]
        t0 = ss[0].ts
        x = [(s.ts - t0) / 60 for s in ss]
        self.line_pct.set_data(x, [s.percent for s in ss])
        self.line_rate.set_data(x, [abs(s.rate_mw) / 1000 for s in ss])
        drops_x, drops_y = [], []  # mark samples right after a big drop
        for i in range(1, len(ss)):
            if ss[i - 1].percent - ss[i].percent >= DROP_FAULT_PCT:
                drops_x.append((ss[i].ts - t0) / 60)
                drops_y.append(ss[i].percent)
        self.anom_scatter.set_offsets(list(zip(drops_x, drops_y)) if drops_x else [[0, -10]])
        xmax = max(x[-1], 5)
        self.ax.set_xlim(0, xmax)
        self.ax2.set_xlim(0, xmax)
        rmax = max((abs(s.rate_mw) / 1000 for s in ss), default=30)
        self.ax2.set_ylim(0, max(rmax * 1.3, 20))
        self.ax.set_xlabel("minutes", color=TEXT_DIM, fontsize=8)
        self.canvas.draw_idle()

    # ---------------- actions

    def _toggle_test(self):
        if self.rec.recording:
            self.rec.stop()
            self.rec_lbl.configure(text="● IDLE", text_color=TEXT_DIM)
            self.test_btn.configure(text="▶  START DRAIN TEST", fg_color=GREEN_DIM)
            self.cancel_btn.configure(state="disabled")
            self._summarize()
        else:
            self.rec.start()
            self.rec_lbl.configure(text="● REC", text_color=RED)
            self.test_btn.configure(text="■  STOP TEST", fg_color="#5d1a1a",
                                    hover_color="#7a2222")
            self.cancel_btn.configure(state="normal")
            self.log_box.configure(state="normal")
            self.log_box._textbox.delete("1.0", "end")
            self.log_box.configure(state="disabled")
            self._log("ok", f"Drain test started → {os.path.basename(self.rec.csv_path)}")
            if self.last_sample and self.last_sample.plugged:
                self._log("warn", "AC is connected — unplug to begin discharging")

    def _cancel_test(self):
        """Abort the test: stop recording and discard the CSV + samples."""
        if not self.rec.recording:
            return
        csv_path = self.rec.csv_path
        self.rec.stop()
        self.rec.samples.clear()
        self.rec_lbl.configure(text="● IDLE", text_color=TEXT_DIM)
        self.test_btn.configure(text="▶  START DRAIN TEST", fg_color=GREEN_DIM)
        self.cancel_btn.configure(state="disabled")
        try:
            os.remove(csv_path)
            self._log("info", f"Test cancelled — discarded {os.path.basename(csv_path)}")
        except OSError:
            self._log("warn", f"Test cancelled — could not delete {csv_path}")

    # -- "drain to empty": disable Windows low/critical battery actions so the
    #    pack rides down to the hardware cutoff instead of hibernating ~7%.

    _PWR_SUB = "e73a048d-bf27-4f12-9731-8b2076e8891f"   # SUB_BATTERY
    _PWR_ITEMS = {
        "8183ba9a-e910-48da-8769-14ae6dc1170a": 5,   # BATLEVELLOW  -> 5%
        "d8742dcb-3e6a-4b3c-b3fe-374623cdcf06": 0,   # BATACTIONLOW -> do nothing
        "9a66d8d7-4ff7-4ef9-b5a2-5a326ca2a469": 3,   # BATLEVELCRIT -> 3%
        "637ea02f-bbcb-4015-8e2c-a1c7b9c0b546": 0,   # BATACTIONCRIT-> do nothing
    }

    def _powercfg(self, *args) -> int:
        try:
            return subprocess.run(
                ["powercfg", *args], capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
        except Exception:
            return -1

    def _query_dc_settings(self) -> dict:
        """Current DC indexes for the battery subgroup, keyed by GUID."""
        try:
            out = subprocess.run(
                ["powercfg", "/query", "SCHEME_CURRENT", self._PWR_SUB],
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except Exception:
            return {}
        vals, cur = {}, None
        for line in out.splitlines():
            m = re.search(r"Power Setting GUID:\s*([0-9a-f-]{36})", line, re.I)
            if m:
                cur = m.group(1).lower()
            m = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-f]+)", line, re.I)
            if m and cur:
                vals[cur] = int(m.group(1), 16)
        return vals

    def _toggle_drain(self):
        if self._drain_unlocked:
            ok = True
            for guid, val in self._saved_power.items():
                ok &= self._powercfg("/setdcvalueindex", "SCHEME_CURRENT",
                                     self._PWR_SUB, guid, str(val)) == 0
            ok &= self._powercfg("/setactive", "SCHEME_CURRENT") == 0
            self._drain_unlocked = False
            self.drain_btn.configure(text="🔋  DRAIN TO EMPTY: OFF", text_color=ORANGE)
            self._log("ok" if ok else "warn",
                      "Windows battery actions restored" if ok else
                      "Restore failed — check power settings manually")
            return

        self._saved_power = self._query_dc_settings()
        ok = all(self._powercfg("/setdcvalueindex", "SCHEME_CURRENT",
                                self._PWR_SUB, guid, str(val)) == 0
                 for guid, val in self._PWR_ITEMS.items())
        ok &= self._powercfg("/setactive", "SCHEME_CURRENT") == 0
        if ok:
            self._drain_unlocked = True
            self.drain_btn.configure(text="🔋  DRAIN TO EMPTY: ON", text_color=GREEN)
            self._log("ok", "Low/critical battery actions disabled — will drain "
                            "to hardware cutoff (~0-3%). Re-enable after testing.")
        else:
            self._log("fault", "Could not change power settings — run VoltCheck "
                               "as Administrator to unlock deep drain")

    def _on_close(self):
        if self._drain_unlocked:
            self._toggle_drain()          # restore power settings on exit
        self.destroy()

    def _summarize(self):
        ss = self.rec.samples
        if len(ss) < 2:
            self._log("info", "Test stopped — too few samples")
            return
        dur = ss[-1].ts - ss[0].ts
        dis = [s for s in ss if not s.plugged and s.rate_mw < 0]
        avg_w = sum(-s.rate_mw for s in dis) / len(dis) / 1000 if dis else 0
        drop = ss[0].percent - ss[-1].percent
        faults = sum(1 for e in self.rec.events if e.level == "fault")
        lvl = "fault" if faults else "ok"
        self._log(lvl, f"Test done: {fmt_dur(dur)}, {ss[0].percent}%→{ss[-1].percent}% "
                       f"({drop}%), avg {avg_w:.1f} W, {len(ss)} samples, "
                       f"{faults} fault(s). CSV: {os.path.basename(self.rec.csv_path)}")

    def _gen_report(self):
        if self._report_busy:
            return
        if not self.rec.samples:
            self._log("warn", "No samples yet — start a test first")
            return
        self._report_busy = True

        def work():
            try:
                path = generate_report(self.rec, self.static, self.demo)
                self._ui_queue.put(lambda: self._log("ok", f"Report saved: {path}"))
                self._ui_queue.put(lambda: webbrowser.open(
                    f"file:///{path.replace(os.sep, '/')}"))
            except Exception as e:
                self._ui_queue.put(lambda: self._log("fault", f"Report failed: {e}"))
            finally:
                self._ui_queue.put(lambda: setattr(self, "_report_busy", False))

        threading.Thread(target=work, daemon=True).start()

    def _windows_report(self):
        def work():
            path = os.path.join(REPORT_DIR,
                                f"windows_battery_{datetime.now():%Y%m%d_%H%M%S}.html")
            os.makedirs(REPORT_DIR, exist_ok=True)
            subprocess.run(["powercfg", "/batteryreport", "/output", path],
                           capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._ui_queue.put(lambda: self._log("ok", f"Windows report: {path}"))
            self._ui_queue.put(lambda: webbrowser.open(
                f"file:///{path.replace(os.sep, '/')}"))
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- entry

def main():
    demo = "--demo" in sys.argv
    app = App(demo=demo)
    app.mainloop()


if __name__ == "__main__":
    main()
