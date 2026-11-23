# VoltCheck — dev notes

Single-file app: `battery_monitor.py` (~1600 lines), customtkinter + embedded
matplotlib. Everything below was learned the hard way.

## Environment (Gcnewbief machine)

- Shell is Git Bash. Use `py` — `python`/`python3` are not on PATH.
- No global git identity. Commit with:
  `git -c user.name="Devin" -c user.email="158243242+devin-ai-integration[bot]@users.noreply.github.com" commit ...`
- `.env` holds `GITHUB_TOKEN` + `GITHUB_REPO` (gitignored — never print).
  `gh` is authenticated as `gcnewbief`; refresh `.env` from `gh auth token` if
  pushes 401.
- Remote: `https://github.com/gcnewbief/VoltTest.git`
- This machine runs 150% widget scaling — good for reproducing DPI issues.
- The "other machine" (Dell laptop): stale/blocked cert store — update check
  needed the certifi fallback. Test TLS changes there.

## Release flow

1. Bump `APP_VERSION` in `battery_monitor.py` (must be higher than the latest
   tag — `release.py` doesn't overwrite).
2. Commit, then build:
   `py -m PyInstaller --noconfirm --clean --onefile --windowed --name VoltCheck --collect-all customtkinter --collect-all matplotlib battery_monitor.py`
   (`build.bat` ends in `pause` — don't run it in automation)
3. `py push.py` then `py release.py` (release.py's "Not Found" 404 is just it
   checking the tag doesn't exist — normal).
- If the exe is locked by a running instance, rename it to `VoltCheck_old.exe`
  (Windows allows renaming a running exe) and build fresh — don't kill
  someone's test mid-run.

## Git Bash gotchas

- `powercfg /list` etc. FAIL in Git Bash ("Invalid Parameters") — MSYS mangles
  `/args` into paths. For manual testing use `cmd //c "powercfg /list"` or
  PowerShell. In-app `subprocess.run(["powercfg", ...])` is unaffected.
- `pip` via `py -m pip` if plain `pip` misbehaves.

## customtkinter / Tk traps (all bit us)

- `winfo_*` = **physical px**; CTk `width=`/`pady=`/`wraplength` = logical
  units × widget scaling (150% here). Convert via
  `ScalingTracker.get_widget_scaling(widget)` before mixing them.
- `CTkScrollableFrame`: scroll region is `bbox("all")` — padding inside the
  content counts, so centering `pady` on a child forces the scrollbar on.
  Viewport height is the **internal canvas**: `inner._parent_canvas.configure(
  height=px)` (raw tk, physical px). `inner.configure(height=)` does NOT work.
  `inner._scrollbar` never auto-hides — `grid_remove()`/`grid()` it yourself.
- Empty `CTkLabel` still requests full height → `pack_forget()` it when
  `text=""` or it leaves dead space (was the "3 buttons of padding" bug).
- Re-parenting is impossible in Tk — if content must swap between a centered
  frame and a scrollable, use spacer rows + viewport sizing instead.

## Python / Windows API traps

- `urllib.request.urlopen` wraps TLS errors in `URLError(reason=SSLError)` —
  `except ssl.SSLError` alone will NOT catch cert failures. Check `e.reason`.
- `VERIFY_X509_STRICT` is default in Python 3.13+ — chains browsers accept
  (missing intermediates, AV/proxy inspection) can fail. `_upd_ctxs()` tries
  default → certifi → non-strict, all still verifying.
- Exception vars die after `except ... as e` — bind them:
  `lambda e=e: self._log(...)` in thread→UI queue lambdas.
- Power-setting GUIDs: verify against `powercfg /query` on a real machine —
  a single wrong digit fails silently with a misleading message.
  (STANDBYIDLE is `...b1f44da`, not `...b1f4980`.)
- Modern Standby machines may lack sleep/hibernate-idle settings — write only
  what `_query_dc_settings` says exists.

## Testing UI

Drive it headless-ish: create `App(demo=True)`, `app.state('zoomed')` or
`app.geometry(...)`, loop `app.update()` + `time.sleep()`, then
`PIL.ImageGrab.grab()`. Demo mode simulates TWO packs so dual-battery paths
are always exercised. For single-battery UI, call `_refresh_stats` with a
hand-made `LiveReading`/`Sample`.
