"""Download the helper programs bundled with the app, verified by checksum.

  * FFmpeg (LGPL, shared build) - joins YouTube's separate video + audio, makes MP3s.
    https://github.com/BtbN/FFmpeg-Builds
  * Deno - JavaScript runtime yt-dlp needs to unlock YouTube formats.
    https://github.com/denoland/deno

Usage: python fetch_tools.py <tools_dir>
Result: <tools_dir>/ffmpeg/{ffmpeg.exe,ffprobe.exe,*.dll,LICENSE.txt}
        <tools_dir>/deno/{deno.exe,LICENSE.md}
        <tools_dir>/versions.json
Already-downloaded tools are kept; delete the folder to refresh them.
"""
import hashlib
import io
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

import requests

OUT = Path(sys.argv[1])
S = requests.Session()
S.headers["User-Agent"] = "build-script"
S.headers["Accept"] = "application/vnd.github+json"


def latest_release(repo):
    r = S.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=30)
    r.raise_for_status()
    return r.json()


def get(url):
    r = S.get(url, timeout=600, headers={"Accept": "application/octet-stream"})
    r.raise_for_status()
    return r.content


def check(blob, expected, name):
    got = hashlib.sha256(blob).hexdigest()
    if got.lower() != expected.lower():
        raise SystemExit(f"Checksum mismatch for {name}: {got} != {expected}")
    print(f"  checksum OK  {name}  ({len(blob)/1e6:.1f} MB)")


versions = {}
if (OUT / "versions.json").exists():
    versions = json.loads((OUT / "versions.json").read_text())

# ---------------- FFmpeg ----------------
ff_dir = OUT / "ffmpeg"
if not (ff_dir / "ffmpeg.exe").exists():
    rel = latest_release("BtbN/FFmpeg-Builds")
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    # newest numbered release branch, LGPL, shared libraries (not "master")
    cands = [n for n in assets if re.fullmatch(r"ffmpeg-n(\d+)\.(\d+)-latest-win64-lgpl-shared-\1\.\2\.zip", n)]
    if not cands:
        raise SystemExit("No LGPL shared FFmpeg build found in the latest release")
    name = max(cands, key=lambda n: tuple(map(int, re.search(r"n(\d+)\.(\d+)", n).groups())))
    sums = get(assets["checksums.sha256"]).decode()
    expected = next(l.split()[0] for l in sums.splitlines() if l.strip().endswith(name))
    print("FFmpeg:", name)
    blob = get(assets[name])
    check(blob, expected, name)
    ff_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for m in z.namelist():
            base = m.split("/")
            if len(base) >= 3 and base[-2] == "bin" and base[-1].lower().endswith((".exe", ".dll")):
                if base[-1].lower() == "ffplay.exe":
                    continue
                (ff_dir / base[-1]).write_bytes(z.read(m))
            elif base[-1] == "LICENSE.txt":
                (ff_dir / "LICENSE.txt").write_bytes(z.read(m))
    versions["ffmpeg"] = name
else:
    print("FFmpeg: already present")

# ---------------- Deno ----------------
deno_dir = OUT / "deno"
if not (deno_dir / "deno.exe").exists():
    rel = latest_release("denoland/deno")
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    name = "deno-x86_64-pc-windows-msvc.zip"
    # The .sha256sum file may be plain "<hash>  <file>" or PowerShell Get-FileHash output.
    expected = re.search(r"\b[0-9a-fA-F]{64}\b", get(assets[name + ".sha256sum"]).decode()).group(0)
    print("Deno:", rel["tag_name"])
    blob = get(assets[name])
    check(blob, expected, name)
    deno_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        (deno_dir / "deno.exe").write_bytes(z.read("deno.exe"))
    lic = S.get(f"https://raw.githubusercontent.com/denoland/deno/{rel['tag_name']}/LICENSE.md", timeout=30)
    if lic.ok:
        (deno_dir / "LICENSE.md").write_text(lic.text, "utf-8")
    versions["deno"] = rel["tag_name"]
else:
    print("Deno: already present")

(OUT / "versions.json").write_text(json.dumps(versions, indent=2))
total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
print(f"Tools ready in {OUT}  ({total/1e6:.0f} MB uncompressed): {versions}")
