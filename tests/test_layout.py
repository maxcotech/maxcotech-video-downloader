"""Opens the real window as if on different screens / Windows scaling levels and
checks it fits: nothing clipped, status bar visible, enough download rows.

usage: python -I tests/test_layout.py zoom_downloader.py <work_dir> [screenshot_dir]
"""
import ctypes, importlib.util, json, sys, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
APP = Path(sys.argv[1]); WORK = Path(sys.argv[2])
SHOTS = Path(sys.argv[3]) if len(sys.argv) > 3 else None
spec = importlib.util.spec_from_file_location("zd", APP)
zd = importlib.util.module_from_spec(spec); sys.modules["zd"] = zd; spec.loader.exec_module(zd)

WORK.mkdir(parents=True, exist_ok=True)
zd.DATA_DIR = WORK; zd.CONFIG_PATH = WORK / "settings.json"; zd.LOG_PATH = WORK / "errors.log"

ctypes.windll.shcore.SetProcessDpiAwareness(1)
real = zd.work_area()
print("real screen work area:", real)

SCREENS = [(1280, 720), (1366, 768), (1536, 864), (1920, 1080), (2560, 1440), (3840, 2160)]
SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]
SHOT_SET = {((1366, 768), 1.25), ((1920, 1080), 1.5), ((1920, 1080), 1.0)}
TASKBAR = 48


def all_widgets(w):
    for c in w.winfo_children():
        yield c
        yield from all_widgets(c)


results, failures = [], []
for (sw, sh) in SCREENS:
    for scale in SCALES:
        lw, lh = sw / scale, sh / scale
        if scale > 1 and (lw < 1024 or lh < 600):
            continue  # Windows doesn't offer this scaling on this screen
        zd.CONFIG_PATH.write_text(json.dumps({"out_dir": str(WORK / "out"), "engine_checked": time.time()}))
        zd.SCREEN_OVERRIDE = (0, 0, sw, sh - int(TASKBAR * scale))
        r = {}

        def on_ready(app, root, sw=sw, sh=sh, scale=scale, r=r):
            app.paused_all = True
            app.pause_btn.config(text="Resume all")
            for i, (st, label) in enumerate([("Done", "Recording for Aug 8"), ("Paused", "Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film"),
                                             ("Failed", "Week 3 lecture"), ("Queued", "Recording for Aug 9"),
                                             ("Queued", "Week 4: Data structures")]):
                iid = app.add_job(f"https://example.com/{i}", "", label, state=st)
                if st == "Failed":
                    app.jobs[iid].detail = "Wrong or missing passcode"
            app._tick()
            root.update_idletasks(); root.update()
            W, H = root.winfo_width(), root.winfo_height()
            ox, oy = root.winfo_rootx(), root.winfo_rooty()
            clipped = []
            for w in all_widgets(root):
                cls = w.winfo_class()
                if cls not in ("TButton", "TLabel", "TCombobox", "TSpinbox", "TLabelframe", "TEntry"):
                    continue
                if not w.winfo_ismapped():
                    continue
                x = w.winfo_rootx() - ox; y = w.winfo_rooty() - oy
                txt = ""
                try:
                    txt = str(w.cget("text"))[:30]
                except Exception:
                    pass
                if x + w.winfo_width() > W + 1 or y + w.winfo_height() > H + 1:
                    clipped.append(f"{cls}:{txt!r} outside window")
                elif cls in ("TButton", "TLabel") and w.winfo_reqwidth() > w.winfo_width() + 2 and txt:
                    clipped.append(f"{cls}:{txt!r} cut off")
            row_h = int(26 * app.scale)
            rows = max(0, app.tree.winfo_height() // row_h - 1)
            wa = zd.work_area()
            r.update(W=W, H=H, fits=(W <= wa[2] and H <= wa[3]), rows=rows, clipped=clipped,
                     box_lines=int(app.text.cget("height")))
            if SHOTS and ((sw, sh), scale) in SHOT_SET and W <= real[2] and H <= real[3]:
                from PIL import ImageGrab
                root.geometry(f"+{real[0]}+{real[1]}"); root.attributes("-topmost", True)
                root.update(); time.sleep(0.6); root.update()
                x0, y0 = root.winfo_rootx(), root.winfo_rooty()
                SHOTS.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(bbox=(x0, y0, x0 + W, y0 + H), all_screens=True).save(
                    SHOTS / f"layout_{sw}x{sh}_{int(scale * 100)}.png")
            root.destroy()

        zd.run_gui(on_ready, tk_scaling=96 * scale / 72)
        ok = r["fits"] and not r["clipped"] and r["rows"] >= 3
        results.append(((sw, sh), scale, r, ok))
        if not ok:
            failures.append(((sw, sh), scale, r))
        print(f"{sw}x{sh} @{int(scale*100)}%  window {r['W']}x{r['H']}  fits={r['fits']}  "
              f"rows={r['rows']}  paste_lines={r['box_lines']}  {'OK' if ok else 'PROBLEM: ' + '; '.join(r['clipped'])}")

print(f"\n{len(results) - len(failures)}/{len(results)} layouts OK")
sys.exit(1 if failures else 0)
