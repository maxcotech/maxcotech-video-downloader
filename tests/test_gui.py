"""End-to-end GUI test: paste text -> queue -> yt-dlp resolve -> multi-connection download -> Done."""
import hashlib, importlib.util, random, re, sys, threading, time, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
APP = Path(sys.argv[1]); WORK = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("zd", APP)
zd = importlib.util.module_from_spec(spec); sys.modules["zd"] = zd; spec.loader.exec_module(zd)

# keep the test away from the real settings file
WORK.mkdir(parents=True, exist_ok=True)
import shutil
for f in WORK.glob("*"):
    shutil.rmtree(f) if f.is_dir() else f.unlink()
zd.DATA_DIR = WORK / "data"; zd.CONFIG_PATH = zd.DATA_DIR / "settings.json"; zd.LOG_PATH = zd.DATA_DIR / "errors.log"
OUT = WORK / "out"
zd.DATA_DIR.mkdir(exist_ok=True)
zd.CONFIG_PATH.write_text(json.dumps({"out_dir": str(OUT), "engine_checked": time.time()}))

SIZE = 40 * 1024 * 1024 + 777
DATA = random.Random(2).randbytes(SIZE)
SHA = hashlib.sha256(DATA).hexdigest()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, head_only):
        rng = self.headers.get("Range")
        if rng:
            s, e = map(int, re.match(r"bytes=(\d+)-(\d*)", rng).groups(default=str(SIZE - 1)))
            e = min(e, SIZE - 1); body = DATA[s:e + 1]
            self.send_response(206); self.send_header("Content-Range", f"bytes {s}-{e}/{SIZE}")
        else:
            body = DATA; self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            try:
                for off in range(0, len(body), 1 << 16):
                    self.wfile.write(body[off:off + (1 << 16)]); time.sleep(0.05)
            except OSError:
                pass
    def do_GET(self): self._send(False)
    def do_HEAD(self): self._send(True)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_port

PASTE = f"""Recording for Aug 8

http://127.0.0.1:{port}/lecture1.mp4

Passcode: Hx4=+tR9

Recording for Aug 9: Week 2
http://127.0.0.1:{port}/lecture2.mp4
Passcode: abc

Broken one
https://us06web.zoom.invalid/rec/share/nope
Passcode: zzz
"""

result = {}

def on_ready(app, root):
    app.text.insert("1.0", PASTE)
    app.add_from_box()
    t0 = time.time()
    samples = []
    def poll():
        states = [j.state for j in app.jobs.values()]
        for iid, j in app.jobs.items():
            if j.state == "Downloading" and j.speed:
                samples.append(app.tree.item(iid, "values"))
                if len(samples) == 3:
                    print("MID-DOWNLOAD ROW", samples[-1])
        if all(s in ("Done", "Failed") for s in states) or time.time() - t0 > 120:
            app._tick()
            result["rows"] = [app.tree.item(i, "values") for i in app.tree.get_children()]
            result["status"] = app.status.get()
            result["speed_seen"] = bool(samples)
            app.save()
            root.destroy()
        else:
            root.after(300, poll)
    root.after(300, poll)

zd.run_gui(on_ready)

for r in result["rows"]:
    print("ROW", r)
print("STATUS", result["status"])
files = sorted(p.name for p in OUT.iterdir())
print("FILES", files)
assert files == ["Recording for Aug 8.mp4", "Recording for Aug 9_ Week 2.mp4"], files
for f in files:
    assert hashlib.sha256((OUT / f).read_bytes()).hexdigest() == SHA, f
states = [r[1] for r in result["rows"]]
assert states[0] == "Done" and states[1] == "Done" and states[2].startswith("Failed"), states
assert result["speed_seen"]
saved = json.loads(zd.CONFIG_PATH.read_text())
print("saved queue states:", [q["state"] for q in saved["queue"]])
print("GUI END-TO-END PASSED")
