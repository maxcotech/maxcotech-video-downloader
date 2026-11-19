"""Optional live check against real YouTube (needs internet). Use a video you may
download, e.g. a Creative Commons one.

usage: python -I tests/test_youtube_live.py zoom_downloader.py <out_dir> <youtube_url> [quality ...]
"""
import importlib.util, json, subprocess, sys, threading, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
APP, OUT, URL = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
QUALITIES = sys.argv[4:] or ["480p", "Audio only (MP3)"]
spec = importlib.util.spec_from_file_location("zd", APP)
zd = importlib.util.module_from_spec(spec); sys.modules["zd"] = zd; spec.loader.exec_module(zd)
OUT.mkdir(parents=True, exist_ok=True)
ffprobe = zd.tool_path("ffmpeg").with_name("ffprobe.exe")


class J:
    done = total = 0; state = detail = ""


for q in QUALITIES:
    t0 = time.time()
    targets, cookies = zd.resolve(URL, "", q)
    t = targets[0]
    print(f"[{q}] title={t['title']!r} parts={len(t['parts'])} post={t['post']} "
          f"est={t['filesize'] / 1e6:.1f} MB direct={t['direct']} (resolved in {time.time() - t0:.1f}s)")
    dest = OUT / f"{zd.safe_name(t['title'])} [{q.split()[0]}].{t['ext']}"
    for f in OUT.glob(dest.name + "*"):
        f.unlink()
    j = J(); stop = threading.Event()
    t0 = time.time()

    def show():
        while not stop.wait(2):
            print(f"   {j.state or 'Downloading'}: {j.done / 1e6:6.1f} / {j.total / 1e6:.1f} MB")
    threading.Thread(target=show, daemon=True).start()
    zd.download_target(t, dest, cookies, 8, j, threading.Event())
    stop.set()
    secs = time.time() - t0
    info = json.loads(subprocess.run([str(ffprobe), "-v", "error", "-show_entries",
                                      "stream=codec_type,codec_name,height:format=duration",
                                      "-of", "json", str(dest)], capture_output=True, text=True).stdout)
    streams = [(s["codec_type"], s.get("codec_name"), s.get("height")) for s in info["streams"]]
    size = dest.stat().st_size
    print(f"   saved {dest.name}: {size / 1e6:.1f} MB in {secs:.1f}s ({size / 1e6 / secs:.1f} MB/s), "
          f"duration {float(info['format']['duration']):.0f}s, streams {streams}")
