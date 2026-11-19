"""Window test for playlists: paste a YouTube playlist link with quality 1080p ->
one row per video, each keeping 1080p, all downloaded. The playlist reader is
replaced with three local test videos, so nothing is fetched from YouTube."""
import hashlib, importlib.util, json, random, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
APP = Path(sys.argv[1]); WORK = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("zd", APP)
zd = importlib.util.module_from_spec(spec); sys.modules["zd"] = zd; spec.loader.exec_module(zd)

import shutil
shutil.rmtree(WORK, ignore_errors=True); WORK.mkdir(parents=True)
zd.DATA_DIR = WORK / "data"; zd.CONFIG_PATH = zd.DATA_DIR / "settings.json"; zd.LOG_PATH = zd.DATA_DIR / "errors.log"
OUT = WORK / "out"; zd.DATA_DIR.mkdir()
zd.CONFIG_PATH.write_text(json.dumps({"out_dir": str(OUT), "engine_checked": time.time()}))

DATA = random.Random(3).randbytes(6 * 1024 * 1024 + 99)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, head):
        rng = self.headers.get("Range")
        if rng:
            s, e = map(int, re.match(r"bytes=(\d+)-(\d*)", rng).groups(default=str(len(DATA) - 1)))
            e = min(e, len(DATA) - 1); body = DATA[s:e + 1]; self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{len(DATA)}")
        else:
            body = DATA; self.send_response(200)
        self.send_header("Content-Type", "video/mp4"); self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except OSError:
                pass
    def do_GET(self): self._send(False)
    def do_HEAD(self): self._send(True)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
VIDEOS = [(f"http://127.0.0.1:{srv.server_port}/film{i}.mp4", t)
          for i, t in enumerate(["Sintel", "Tears of Steel", "Spring"])]
asked = []
zd.list_playlist = lambda url, depth=0: (asked.append(url), VIDEOS)[1]

PLAYLIST = "https://www.youtube.com/playlist?list=PL6B3937A5D230E335"
result = {}


def on_ready(app, root):
    app.q_var.set("1080p")
    app.text.insert("1.0", f"Blender open movies: {PLAYLIST}")
    app.add_from_box()
    app.q_var.set("Audio only (MP3)")  # changing it afterwards must not affect queued rows
    t0 = time.time()

    def poll():
        jobs = list(app.jobs.values())
        if (len(jobs) == 4 and all(j.state in ("Done", "Failed") for j in jobs)) or time.time() - t0 > 90:
            app._tick()
            result["rows"] = [app.tree.item(i, "values")[:2] for i in app.tree.get_children()]
            result["qualities"] = [j.quality for j in jobs]
            root.destroy()
        else:
            root.after(300, poll)
    root.after(300, poll)


zd.run_gui(on_ready)
for r in result["rows"]:
    print("ROW", r)
assert asked == [PLAYLIST], asked
assert result["rows"][0][1].startswith("Done – playlist: 3 videos added below"), result["rows"][0]
assert [r[0] for r in result["rows"][1:]] == ["Sintel", "Tears of Steel", "Spring"]
assert all(r[1] == "Done" for r in result["rows"][1:]), result["rows"]
assert result["qualities"] == ["1080p"] * 4, result["qualities"]
files = sorted(p.name for p in OUT.iterdir())
assert files == ["Sintel.mp4", "Spring.mp4", "Tears of Steel.mp4"], files
assert all((OUT / f).read_bytes() == DATA for f in files)
print("files:", files)
print("PLAYLIST GUI TEST PASSED")
