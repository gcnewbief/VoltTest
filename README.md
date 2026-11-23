# VoltCheck

Laptop battery drain-test & health monitor for Windows. A modern black/green
replacement for BatteryMon / BatteryInfoView workflows.

## Features

- Live stats: charge %, charge/discharge rate (W), voltage, ETA, capacity (mWh)
- Battery health: full-charge vs design capacity, green/orange/red
- Live charge/discharge graph: power line is blue while discharging, orange
  while charging, continuous through state flips with transition dots; fault
  markers for drops/stalls
- Dual-battery systems (e.g. ThinkPads): per-pack % lines (B1 green / B2 teal)
  with legend, per-pack readout, per-pack health — detected only when exactly
  two packs are present, single-battery machines are unchanged
- Drain-test mode: logs samples to CSV, detects sudden % drops (dead cells),
  charge stalls, high discharge rate, battery health imbalance
- DRAIN TO EMPTY: disables Windows low/critical battery actions so the pack
  drains to hardware cutoff (restores settings on toggle-off/exit)
- KEEP AWAKE: sets display-off/sleep/hibernate to "never" on battery so a long
  test isn't napped off (saves & restores, skips settings the scheme lacks)
- Self-contained HTML report with verdict, stats, chart and event log
- One-click Windows `powercfg` battery report
- Update checker: flags newer GitHub releases in the title bar (clickable)
- Auto-saves a report when battery crosses 10% mid-test; OPEN LOG reloads and
  can continue a partial test
- USER SIM: wandering CPU load to simulate use; YT VIDEO TEST launches a video
  (URL saved in `voltcheck.ini`, right-click the button to change)
- High-DPI friendly: sidebar scrolls only when content can't fit, tiles scroll
  horizontally under a minimum width

## Run

Download `VoltCheck.exe` from
[Releases](https://github.com/gcnewbief/VoltTest/releases) (no Python needed):

```
VoltCheck.exe            # normal
VoltCheck.exe --demo     # simulated drain, incl. a second pack (no battery needed)
```

Logs go to `logs/`, reports to `reports/` next to the exe.

From source:

```
pip install -r requirements.txt
py battery_monitor.py [--demo]
```

## Build the exe

```
build.bat    # produces dist\VoltCheck.exe
```

## Notes

- Live data: `CallNtPowerInformation`/`GetSystemPowerStatus` (ctypes), WMI
  `root\wmi` as fallback, rate derived from mWh deltas when firmware doesn't
  report one; per-pack % uses `BatteryStatus` + `BatteryFullChargedCapacity`
- Design capacity/serial/chemistry/cycle count parsed from
  `powercfg /batteryreport` (works where WMI `BatteryStaticData` is empty)
- Changing power settings may require running as administrator
- CSV log files gained a `pack_pcts` column in v1.3.0; older logs still load
