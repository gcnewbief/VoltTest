# VoltCheck

Laptop battery drain-test & health monitor for Windows. A modern black/green
replacement for BatteryMon / BatteryInfoView workflows.

## Features

- Live stats: charge %, charge/discharge rate (W), voltage, ETA, capacity (mWh)
- Battery health: full-charge vs design capacity, green/orange/red
- Multi-battery support (e.g. dual-battery ThinkPads) with per-pack health
- Live charge/discharge graph with fault markers
- Drain-test mode: logs samples to CSV, detects sudden % drops (dead cells),
  charge stalls, high discharge rate, battery health imbalance
- "Drain to empty" toggle: disables Windows low/critical battery actions so the
  pack drains to hardware cutoff (restores settings on toggle-off/exit)
- Self-contained HTML report with verdict, stats, chart and event log
- One-click Windows `powercfg` battery report

## Run

Prebuilt exe (no Python needed):

```
VoltCheck.exe            # normal
VoltCheck.exe --demo     # simulated drain, no battery required
```

Logs go to `logs/`, reports to `reports/` next to the exe.

From source:

```
pip install -r requirements.txt
python battery_monitor.py [--demo]
```

## Build the exe

```
build.bat    # produces dist\VoltCheck.exe
```

## Notes

- Live data: `CallNtPowerInformation`/`GetSystemPowerStatus` (ctypes), WMI
  `root\wmi` as fallback, rate derived from mWh deltas when firmware doesn't
  report one
- Design capacity/serial/chemistry/cycle count parsed from
  `powercfg /batteryreport` (works where WMI `BatteryStaticData` is empty)
- Changing power settings may require running as administrator
