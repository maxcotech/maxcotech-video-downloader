"""Tests for zoom_downloader: link parsing + multi-connection engine against a local server."""
import hashlib, importlib.util, os, random, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("zd", APP)
zd = importlib.util.module_from_spec(spec)
sys.modules["zd"] = zd
spec.loader.exec_module(zd)

WORK = Path(sys.argv[2])
WORK.mkdir(parents=True, exist_ok=True)

# ---------------- parsing ----------------
sample = """Recording for Aug 8

https://us06web.zoom.us/rec/share/Ab12Cd34Ef56Gh78Ij90KlMnOpQrStUvWxYz-example0123456789aBcDeF.Gh1Ij2Kl3Mn4Op5Q


Passcode: Hx4=+tR9

Recording for Aug 9
https://us06web.zoom.us/rec/share/BBBB.cccc
Passcode: x&Y!9%z

https://us06web.zoom.us/rec/share/CCCC.dddd  pw1234
https://us06web.zoom.us/rec/share/DDDD.eeee,pw5678
Week 3: https://us06web.zoom.us/rec/share/EEEE.ffff (Password: Abc.123)
https://us06web.zoom.us/rec/share/Ab12Cd34Ef56Gh78Ij90KlMnOpQrStUvWxYz-example0123456789aBcDeF.Gh1Ij2Kl3Mn4Op5Q
"""
items = zd.parse_links(sample)
for it in items:
    print("PARSE", it)
exp = [("Recording for Aug 8", "Hx4=+tR9"), ("Recording for Aug 9", "x&Y!9%z"),
       ("", "pw1234"), ("", "pw5678"), ("Week 3", "Abc.123)")]
got = [(i["label"], i["passcode"]) for i in items]
assert len(items) == 5, got
for (el, ep), (gl, gp) in zip(exp, got):
    assert gl == el, (gl, el)
    assert gp == ep.rstrip(")"), (gp, ep)
print("parse: OK (duplicate link skipped)")

# ---------------- server ----------------
SIZE = 60 * 1024 * 1024 + 12345
DATA = random.Random(1).randbytes(SIZE)
SHA = hashlib.sha256(DATA).hexdigest()
MODE = {"flaky": 0.0, "ranges": True, "delay": 0.0, "require_cookie": False}
STATS = {"requests": 0, "concurrent": 0, "max_concurrent": 0}
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        with lock:
            STATS["requests"] += 1
            STATS["concurrent"] += 1
            STATS["max_concurrent"] = max(STATS["max_concurrent"], STATS["concurrent"])
        try:
            if MODE["require_cookie"] and "sess=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
            if self.headers.get("Referer") != "https://zoom.example/":
                self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
            rng = self.headers.get("Range")
            if rng and MODE["ranges"]:
                s, e = map(int, re.match(r"bytes=(\d+)-(\d+)", rng).groups())
                e = min(e, SIZE - 1)
                body = DATA[s:e + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {s}-{e}/{SIZE}")
            else:
                body = DATA
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes" if MODE["ranges"] else "none")
            self.end_headers()
            cut = len(body)
            if MODE["flaky"] and len(body) > 1 and random.random() < MODE["flaky"]:
                cut = random.randint(0, len(body) - 1)  # drop the connection mid-chunk
            step = 1 << 16
            for off in range(0, cut, step):
                self.wfile.write(body[off:min(off + step, cut)])
                if MODE["delay"]:
                    time.sleep(MODE["delay"])
            if cut < len(body):
                self.close_connection = True
        except (ConnectionError, OSError):
            pass
        finally:
            with lock:
                STATS["concurrent"] -= 1


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}/video.mp4"
HDR = {"Referer": "https://zoom.example/", "User-Agent": "test"}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fresh(name):
    p = WORK / name
    for s in ("", ".part", ".part.json"):
        Path(str(p) + s).unlink(missing_ok=True)
    return p


# 1. plain multi-connection download
dest = fresh("a.mp4")
STATS.update(requests=0, max_concurrent=0)
t = time.time()
zd.SegmentedDownloader(URL, dest, HDR, [], 16).run()
assert sha(dest) == SHA
print(f"multi-connection: OK  {SIZE/1e6:.0f} MB in {time.time()-t:.2f}s, "
      f"{STATS['requests']} requests, up to {STATS['max_concurrent']} at once")
assert STATS["max_concurrent"] >= 4  # local server is so fast connections rarely all overlap

# 2. cookies passed through (Zoom needs its session cookies)
import http.cookiejar
MODE["require_cookie"] = True
c = http.cookiejar.Cookie(0, "sess", "ok", None, False, "127.0.0.1", False, False, "/", True,
                          False, None, False, None, None, {})
dest = fresh("cookie.mp4")
zd.SegmentedDownloader(URL, dest, HDR, [c], 8).run()
assert sha(dest) == SHA
try:
    zd.SegmentedDownloader(URL, fresh("nocookie.mp4"), HDR, [], 8).run()
    raise AssertionError("should have failed without cookie")
except IOError as e:
    print("cookies: OK  (without cookie ->", e, ")")
MODE["require_cookie"] = False

# 3. flaky server: 30% of responses cut mid-way -> retries must fix it
MODE["flaky"] = 0.3
dest = fresh("flaky.mp4")
zd.CHUNK_RETRIES = 12
zd.SegmentedDownloader(URL, dest, HDR, [], 16).run()
assert sha(dest) == SHA
print("flaky connections: OK  (file intact after retries)")
MODE["flaky"] = 0.0

# 4. pause half-way, then resume from saved chunks
MODE["delay"] = 0.002
dest = fresh("resume.mp4")
cancel = threading.Event()
prog = {"d": 0}
def on_prog(d, tot):
    prog["d"] = d
    if d > SIZE * 0.4:
        cancel.set()
try:
    zd.SegmentedDownloader(URL, dest, HDR, [], 8, cancel, on_prog).run()
    raise AssertionError("expected pause")
except zd.Cancelled:
    pass
import json
st = json.loads(Path(str(dest) + ".part.json").read_text())
print(f"paused at {prog['d']/SIZE:.0%}; {len(st['done'])} chunks saved")
MODE["delay"] = 0.0
first = {}
def on_prog2(d, tot):
    first.setdefault("d", d)
zd.SegmentedDownloader(URL, dest, HDR, [], 8, None, on_prog2).run()
assert sha(dest) == SHA and not Path(str(dest) + ".part.json").exists()
print(f"resume: OK  (restarted at {first['d']/SIZE:.0%}, not 0%)")
assert first["d"] > 0

# 5. server without range support -> single stream fallback
MODE["ranges"] = False
dest = fresh("norange.mp4")
zd.SegmentedDownloader(URL, dest, HDR, [], 16).run()
assert sha(dest) == SHA
print("no-range fallback: OK")

# 6. helpers
assert zd.safe_name('Week 1: Intro/Basics?') == "Week 1_ Intro_Basics_"
assert zd.friendly_error(Exception("ERROR: [zoom] abc: Wrong passcode")) == "Wrong or missing passcode"
print("helpers: OK")
print("ALL TESTS PASSED")
