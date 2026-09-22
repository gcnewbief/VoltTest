"""Drive the VoltCheck UI and screenshot it for visual check."""
import time
import battery_monitor as bm
from PIL import ImageGrab

app = bm.App(demo=True)
app.geometry("1180x760+40+30")
app.lift()
app.focus_force()


def pump(n, delay=0.05):
    for _ in range(n):
        app.update()
        time.sleep(delay)


pump(60)                      # static info + ~6 demo samples
ImageGrab.grab().save("ui_idle.png")

app._toggle_test()            # START
pump(160)                     # ~8s real -> ~8 min simulated
ImageGrab.grab().save("ui_recording.png")

app._toggle_test()            # STOP -> summary
pump(10)
ImageGrab.grab().save("ui_stopped.png")

app._toggle_test()            # START again
pump(10)
app._cancel_test()            # CANCEL -> discard
pump(10)
ImageGrab.grab().save("ui_cancelled.png")

print("samples:", len(app.rec.samples))
print("csv_exists:", bm.os.path.exists(app.rec.csv_path) if app.rec.csv_path else "n/a")
for e in app.rec.events:
    print("event:", e.level, e.text[:70].encode("ascii", "replace").decode())
app.destroy()
print("DONE")
