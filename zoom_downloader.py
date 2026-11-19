"""Maxcotech Video Downloader

Paste Zoom recording links (with their passcodes) and YouTube videos or
playlists - whole messages are fine - and download them all at once.

How it is fast:
  * yt-dlp opens each link (and handles Zoom passcodes).
  * A built-in multi-connection engine (the same idea as Internet Download
    Manager) splits every file into chunks and pulls them over many
    connections in parallel, retrying failed chunks on its own.
  * YouTube's separate video and audio streams are fetched the same way and
    joined with FFmpeg (no re-encoding, takes seconds).
  * Several videos download at the same time.
  * Interrupted downloads resume where they stopped, even after closing the app.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from glob import escape as glob_escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "Maxcotech Video Downloader"
APP_VERSION = "1.1.0"
# Folder names stay as in 1.0.x so upgrades keep everyone's settings and list.
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "ZoomBatchDownloader"
CONFIG_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "errors.log"
DEFAULT_OUT = Path.home() / "Videos"
QUALITIES = ["Best", "1080p", "720p", "480p", "Audio only (MP3)"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Newer copies of the yt-dlp engine, downloaded by the app itself.
ENGINE_DIR = Path(os.environ.get("LOCALAPPDATA") or DATA_DIR) / "ZoomBatchDownloader" / "engine"
ACTIVE_ENGINE: Path | None = None

KB = 1024
MB = 1024 * KB
CHUNK_RETRIES = 8


class Cancelled(Exception):
    """Raised inside a download when the user pauses or removes it."""


# --------------------------------------------------------------------------
# Helper programs (FFmpeg, Deno) shipped with the app
# --------------------------------------------------------------------------

def tool_path(name: str) -> Path | None:
    """Find ffmpeg.exe / deno.exe: bundled copy first, then a dev build, then PATH."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools")
    roots.append(Path(__file__).resolve().parent / "tools")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "ZBD-build" / "tools")
    for root in roots:
        p = root / name / f"{name}.exe"
        if p.exists():
            return p
    found = shutil.which(name)
    return Path(found) if found else None


def base_ydl_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            # If the bundled YouTube challenge solver is older than the engine
            # needs (after an engine self-update), fetch the matching one.
            "remote_components": ["ejs:github"]}
    ff = tool_path("ffmpeg")
    if ff:
        opts["ffmpeg_location"] = str(ff.parent)
    deno = tool_path("deno")
    if deno:
        opts["js_runtimes"] = {"deno": {"path": str(deno)}}
    return opts


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------

YT_RE = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtube-nocookie\.com|youtu\.be)/", re.I)


def is_youtube(url: str) -> bool:
    return bool(YT_RE.match(url))


def is_youtube_single(url: str) -> bool:
    """A link to one video (a playlist id alongside a video id still means that one video)."""
    u = urlparse(url)
    host = u.netloc.lower()
    if host.endswith("youtu.be"):
        return len(u.path.strip("/")) > 0
    if parse_qs(u.query).get("v"):
        return True
    return bool(re.match(r"/(shorts|live|embed|v)/[\w-]+", u.path))


def quality_opts(quality: str) -> dict:
    """yt-dlp format choice for a quality label.

    Resolution comes first; at that resolution prefer H.264 + AAC in MP4, which
    plays on every laptop, phone and TV (AV1/VP9 are used only when they are the
    only way to get the resolution, e.g. 4K on "Best").
    """
    if quality.startswith("Audio"):
        return {"format": "ba[ext=m4a]/ba/b"}
    m = re.match(r"(\d+)p", quality)
    res = [f"res:{m.group(1)}"] if m else ["res"]
    return {"format": "bv*+ba/b", "format_sort": res + ["vcodec:h264", "ext:mp4:m4a"],
            "merge_output_format": "mp4"}


def list_playlist(url: str, depth: int = 0) -> list[tuple[str, str]]:
    """(video url, title) for every video in a playlist / channel, without downloading."""
    import yt_dlp

    opts = base_ydl_opts()
    opts.update({"extract_flat": "in_playlist", "skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in info.get("entries") or []:
        if not e:
            continue
        link = e.get("url") or e.get("webpage_url") or ""
        if not link:
            continue
        if e.get("ie_key") == "YoutubeTab" or "/playlist" in link or link.rstrip("/").endswith(("/videos", "/shorts", "/streams")):
            if depth < 2:
                out.extend(list_playlist(link, depth + 1))  # channel -> its Videos/Shorts tabs
            continue
        if not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={link}"
        out.append((link, e.get("title") or ""))
    return out


def ffmpeg_run(args: list[str]):
    ff = tool_path("ffmpeg")
    if not ff:
        raise IOError("FFmpeg is missing, so video and audio can't be joined. Please reinstall the app.")
    r = subprocess.run([str(ff), "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise IOError("FFmpeg failed: " + (r.stderr.strip()[-300:] or f"exit {r.returncode}"))


# --------------------------------------------------------------------------
# Engine (yt-dlp) version management
# --------------------------------------------------------------------------

def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def bundled_engine_version() -> str:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return "0"


def use_newest_engine():
    """Load a newer yt-dlp downloaded by the updater instead of the bundled one.

    Must run before anything imports yt_dlp.
    """
    global ACTIVE_ENGINE
    try:
        wheels = [(_ver(w.name.split("-")[1]), w) for w in ENGINE_DIR.glob("yt_dlp-*.whl")]
        if wheels:
            v, wheel = max(wheels)
            if v > _ver(bundled_engine_version()):
                sys.path.insert(0, str(wheel))
                ACTIVE_ENGINE = wheel
    except Exception:
        pass


def fetch_engine_update() -> str | None:
    """Download the newest yt-dlp from PyPI if it is newer than the one in use.

    Returns the new version number, or None if already up to date.
    """
    import hashlib
    import requests
    import yt_dlp

    current = yt_dlp.version.__version__
    r = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=20)
    r.raise_for_status()
    data = r.json()
    latest = data["info"]["version"]
    if _ver(latest) <= _ver(current):
        return None
    f = next(u for u in data["urls"]
             if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("py3-none-any.whl"))
    blob = requests.get(f["url"], timeout=180).content
    if hashlib.sha256(blob).hexdigest() != f["digests"]["sha256"]:
        raise IOError("Engine update failed its checksum; ignored.")
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_DIR / (f["filename"] + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, ENGINE_DIR / f["filename"])
    for old in ENGINE_DIR.glob("yt_dlp-*.whl"):
        if old.name != f["filename"] and old != ACTIVE_ENGINE:
            try:
                old.unlink()
            except OSError:
                pass
    return latest


# --------------------------------------------------------------------------
# Link parsing
# --------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"',]+", re.I)
PASS_RE = re.compile(
    r"\b(?:pass\s*code|pass\s*word|passwd|pwd|access\s*code)\b\s*[:=\-]?\s*(\S+)",
    re.I,
)


def _passcode_from(match: re.Match, segment: str) -> str:
    code = match.group(1)
    # "(Passcode: abc)" -> drop the closing bracket that belongs to the text
    if code.endswith(")") and "(" not in code and "(" in segment[max(0, match.start() - 2):match.start()]:
        code = code[:-1]
    return code


def _clean_title(line: str) -> str:
    line = line.strip().strip(":-|•*>").strip()
    return line[:120]


def parse_links(text: str) -> list[dict]:
    """Find every link in pasted text and pair it with its passcode and title.

    Understands Zoom's own share format::

        Recording for Aug 8
        https://us06web.zoom.us/rec/share/...
        Passcode: Hx4=+tR9

    as well as one-per-line ``link passcode`` or ``link,passcode`` lists.
    """
    matches = list(URL_RE.finditer(text))
    items: list[dict] = []
    seen: set[str] = set()
    consumed_segments: set[int] = set()  # segment i = text between url i-1 and url i
    for i, m in enumerate(matches):
        url = m.group(0).rstrip(").,;]>}")
        prev_end = matches[i - 1].end() if i else 0
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        before = text[prev_end:m.start()]
        after = text[m.end():next_start]

        passcode = ""
        same_line = after.split("\n", 1)[0].strip(" \t,;|")
        pm = PASS_RE.search(after)
        if pm:
            passcode = _passcode_from(pm, after)
            consumed_segments.add(i + 1)
        elif same_line and " " not in same_line:
            passcode = same_line
        elif i not in consumed_segments:
            pm = PASS_RE.search(before)
            if pm:
                passcode = _passcode_from(pm, before)

        # Title: last meaningful line before the link. For links after the
        # first, the first "line" of `before` is the tail of the previous
        # link's line, so skip it.
        lines = before.split("\n")
        if i:
            lines = lines[1:]
        title = ""
        for line in reversed(lines):
            if line.strip() and not PASS_RE.search(line):
                title = _clean_title(line)
                if title:
                    break

        if url in seen:
            continue
        seen.add(url)
        items.append({"url": url, "passcode": passcode, "label": title})
    return items


# --------------------------------------------------------------------------
# Download engine
# --------------------------------------------------------------------------

def make_session(headers: dict | None, cookies: list | None):
    import requests
    from requests.adapters import HTTPAdapter

    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=0))
    s.mount("http://", HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=0))
    for k, v in (headers or {}).items():
        if k.lower() not in ("range", "accept-encoding", "cookie"):
            s.headers[k] = v
    s.headers["Accept-Encoding"] = "identity"
    for c in cookies or []:
        s.cookies.set_cookie(c)
    return s


class SegmentedDownloader:
    """Downloads one file over many parallel HTTP range connections.

    Progress is kept in ``<file>.part.json`` so a paused or interrupted
    download continues from the finished chunks next time.
    """

    def __init__(self, url, dest: Path, headers=None, cookies=None, connections=16,
                 cancel: threading.Event | None = None, on_progress=None, max_chunk: int | None = None):
        self.max_chunk = max_chunk  # YouTube throttles requests bigger than ~10 MB
        self.url = url
        self.dest = Path(dest)
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.state_path = self.dest.with_name(self.dest.name + ".part.json")
        self.headers = headers or {}
        self.cookies = cookies or []
        self.connections = max(1, int(connections))
        self.cancel = cancel or threading.Event()
        self.on_progress = on_progress or (lambda done, total: None)
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self._error: BaseException | None = None
        self._done_bytes = 0
        self._last_state_save = 0.0

    # -- helpers ------------------------------------------------------------
    def _stopped(self) -> bool:
        return self.cancel.is_set() or self._abort.is_set()

    def _add(self, n: int):
        with self._lock:
            self._done_bytes += n
            done = self._done_bytes
        self.on_progress(done, self.total)

    def _probe(self):
        s = make_session(self.headers, self.cookies)
        try:
            r = s.get(self.url, headers={"Range": "bytes=0-0"}, stream=True,
                      timeout=(20, 30), allow_redirects=True)
            try:
                if r.status_code == 206:
                    m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
                    if m:
                        return int(m.group(1)), True
                if r.status_code in (401, 403):
                    raise IOError(f"The server refused the download (HTTP {r.status_code}).")
                r.raise_for_status()
                return int(r.headers.get("Content-Length") or 0), False
            finally:
                r.close()
        finally:
            s.close()

    def _load_state(self, chunk: int) -> set[int]:
        try:
            st = json.loads(self.state_path.read_text("utf-8"))
            if (st.get("total") == self.total and st.get("chunk") == chunk
                    and self.part.exists() and self.part.stat().st_size == self.total):
                return set(st.get("done", []))
        except Exception:
            pass
        return set()

    def _save_state(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_state_save < 1.0:
            return
        self._last_state_save = now
        with self._lock:
            data = {"total": self.total, "chunk": self.chunk, "done": sorted(self.done)}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, self.state_path)

    # -- main ---------------------------------------------------------------
    def run(self) -> Path:
        self.total, ranged = self._probe()
        if not ranged or self.total < 4 * MB:
            return self._single_stream()

        # Chunks small enough to balance work across connections (and keep the
        # slow "last chunk" short), big enough to avoid request overhead.
        target = self.total // (self.connections * 6) or self.total
        self.chunk = int(min(max(target, 2 * MB), 32 * MB))
        if self.max_chunk:
            self.chunk = max(1 * MB, min(self.chunk, int(self.max_chunk)))
        n_chunks = (self.total + self.chunk - 1) // self.chunk

        self.done = self._load_state(self.chunk)
        if not self.done:
            with open(self.part, "wb") as f:
                f.truncate(self.total)
        self._done_bytes = sum(self._chunk_len(i) for i in self.done)
        self.on_progress(self._done_bytes, self.total)

        todo: queue.Queue[int] = queue.Queue()
        for i in range(n_chunks):
            if i not in self.done:
                todo.put(i)

        threads = [threading.Thread(target=self._worker, args=(todo,), daemon=True)
                   for _ in range(min(self.connections, todo.qsize()))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self._save_state(force=True)
        if self.cancel.is_set():
            raise Cancelled()
        if self._error:
            raise self._error
        if len(self.done) != n_chunks:
            raise IOError("Download incomplete; press Retry to continue it.")

        os.replace(self.part, self.dest)
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
        return self.dest

    def _chunk_len(self, i: int) -> int:
        start = i * self.chunk
        return min(self.chunk, self.total - start)

    def _worker(self, todo: queue.Queue):
        s = make_session(self.headers, self.cookies)
        try:
            with open(self.part, "r+b") as fh:
                while not self._stopped():
                    try:
                        i = todo.get_nowait()
                    except queue.Empty:
                        return
                    self._fetch_chunk(s, fh, i)
        except Cancelled:
            pass
        except BaseException as e:  # first error stops the whole file
            with self._lock:
                if self._error is None:
                    self._error = e
            self._abort.set()
        finally:
            s.close()

    def _fetch_chunk(self, s, fh, i: int):
        start = i * self.chunk
        length = self._chunk_len(i)
        end = start + length - 1
        last_err: Exception | None = None
        for attempt in range(CHUNK_RETRIES):
            got = 0
            try:
                with s.get(self.url, headers={"Range": f"bytes={start}-{end}"},
                           stream=True, timeout=(20, 40)) as r:
                    if r.status_code != 206:
                        raise IOError(f"HTTP {r.status_code}")
                    cr = r.headers.get("Content-Range", "")
                    if cr and not cr.replace(" ", "").startswith(f"bytes{start}-"):
                        raise IOError(f"Unexpected range {cr}")
                    fh.seek(start)
                    for data in r.iter_content(256 * KB):
                        if self._stopped():
                            raise Cancelled()
                        if got + len(data) > length:
                            raise IOError("Server sent too much data")
                        fh.write(data)
                        got += len(data)
                        self._add(len(data))
                if got != length:
                    raise IOError("Connection closed early")
                fh.flush()
                with self._lock:
                    self.done.add(i)
                self._save_state()
                return
            except Cancelled:
                self._add(-got)
                raise
            except Exception as e:
                self._add(-got)
                last_err = e
                for _ in range(int(min(2 ** attempt, 20) * 10)):
                    if self._stopped():
                        raise Cancelled()
                    time.sleep(0.1)
        raise IOError(f"A part of the file kept failing ({last_err})")

    def _single_stream(self) -> Path:
        """Fallback for servers that do not allow split downloads."""
        s = make_session(self.headers, self.cookies)
        got = 0
        try:
            with s.get(self.url, stream=True, timeout=(20, 60)) as r:
                r.raise_for_status()
                self.total = int(r.headers.get("Content-Length") or self.total or 0)
                with open(self.part, "wb") as fh:
                    for data in r.iter_content(512 * KB):
                        if self.cancel.is_set():
                            raise Cancelled()
                        fh.write(data)
                        got += len(data)
                        self._done_bytes = got
                        self.on_progress(got, self.total)
        finally:
            s.close()
        if self.total and got != self.total:
            raise IOError("Connection closed early")
        os.replace(self.part, self.dest)
        return self.dest


ZOOM_URL_RE = re.compile(r"https?://(?:[^./]+\.)?zoom\.us/rec(?:ording)?/(?:play|share)/", re.I)
_zoom_ie_class = None


def zoom_passcode_ie():
    """yt-dlp's Zoom extractor plus support for Zoom's newer passcode screen.

    Since late 2026 a passcode-protected share link no longer contains the old
    ``password_form``. Instead ``share-info`` answers ``componentName:
    need-password`` and the browser posts the passcode to
    ``/nws/recording/1.0/validate-meeting-passwd`` (or ``validate-passwd`` for
    file-level passcodes), then reloads the share link. We do the same, so the
    session cookies are unlocked before the normal extraction runs.
    """
    global _zoom_ie_class
    if _zoom_ie_class:
        return _zoom_ie_class

    from yt_dlp.extractor.zoom import ZoomIE
    from yt_dlp.utils import ExtractorError, urlencode_postdata

    class ZoomPasscodeIE(ZoomIE):
        IE_NAME = "zoom"

        def _real_extract(self, url):
            base_url, url_type, video_id = self._match_valid_url(url).group("base_url", "type", "id")
            if url_type == "share":
                self._unlock_share(url, base_url, video_id)
            return super()._real_extract(url)

        def _unlock_share(self, url, base_url, video_id):
            webpage = self._download_webpage(url, video_id, "Downloading share webpage")
            data = self._search_json(r"window\.__data__\s*=", webpage, "data", video_id,
                                     transform_source=self._js_to_json_lenient, default={})
            meeting_id = data.get("meetingId")
            if not meeting_id:
                return  # unknown page layout; let the normal extractor report it
            info = self._download_json(
                f"{base_url}nws/recording/1.0/play/share-info/{meeting_id}", video_id,
                "Checking whether a passcode is needed", fatal=False) or {}
            info = info.get("result") or {}
            if info.get("componentName") != "need-password":
                return
            password = (self.get_param("videopassword") or "").strip()
            if not password:
                raise ExtractorError("This recording needs a passcode", expected=True)
            is_meeting = info.get("useWhichPasswd") == "meeting"
            endpoint = "validate-meeting-passwd" if is_meeting else "validate-passwd"
            res = self._download_json(
                f"{base_url}nws/recording/1.0/{endpoint}", video_id, "Submitting passcode",
                data=urlencode_postdata({
                    "id": info.get("meetingId") if is_meeting else info.get("fileId"),
                    "passwd": password,
                    "action": info.get("action") or "",
                    "recaptcha": "",
                }))
            if not res.get("status"):
                raise ExtractorError(f"Zoom: {res.get('errorMessage') or 'Wrong passcode'}", expected=True)
            if res.get("result") in ("needRecaptcha", "captcha_error"):
                raise ExtractorError(
                    "Zoom: wrong passcode too many times, so Zoom now wants a captcha. "
                    "Open the link once in your browser, then press Retry.", expected=True)

        @staticmethod
        def _js_to_json_lenient(s):
            from yt_dlp.utils import js_to_json
            return js_to_json(s)

    _zoom_ie_class = ZoomPasscodeIE
    return ZoomPasscodeIE


def _ydl_extract(ydl, url, download):
    """extract_info, routing Zoom links through the passcode-aware extractor."""
    if ZOOM_URL_RE.match(url):
        ydl.add_info_extractor(zoom_passcode_ie()())
        return ydl.extract_info(url, download=download, ie_key="ZoomPasscode")
    return ydl.extract_info(url, download=download)


def _part(f: dict) -> dict:
    return {
        "url": f.get("url"),
        "headers": f.get("http_headers") or {},
        "ext": f.get("ext") or "mp4",
        "filesize": f.get("filesize") or f.get("filesize_approx") or 0,
        "max_chunk": (f.get("downloader_options") or {}).get("http_chunk_size"),
        "http": str(f.get("protocol") or "https") in ("http", "https") and bool(f.get("url")),
    }


def resolve(url: str, passcode: str, quality: str = "Best"):
    """Open a link with yt-dlp. Returns (targets, cookies).

    Each target is one output file made of one or more ``parts`` (YouTube HD =
    a video part + an audio part that FFmpeg joins afterwards).
    """
    import yt_dlp

    opts = base_ydl_opts()
    if ZOOM_URL_RE.match(url):
        opts["format"] = "best"  # Zoom files already contain audio + video
    else:
        opts.update(quality_opts(quality))
        opts["noplaylist"] = True
    if passcode:
        opts["videopassword"] = passcode
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = _ydl_extract(ydl, url, download=False)
        cookies = list(ydl.cookiejar)

    if info.get("_type") in ("playlist", "multi_video") and info.get("entries") is not None:
        infos = [e for e in info["entries"] if e]
    else:
        infos = [info]

    audio_only = quality.startswith("Audio") and not ZOOM_URL_RE.match(url)
    targets = []
    for e in infos:
        parts = [_part(f) for f in e["requested_formats"]] if e.get("requested_formats") else [_part(e)]
        if audio_only:
            ext, post = "mp3", "mp3"
        elif len(parts) > 1:
            ext, post = e.get("ext") or "mp4", "merge"
        else:
            ext, post = parts[0]["ext"], None
        targets.append({
            "title": e.get("title") or "",
            "ext": ext,
            "post": post,
            "parts": parts,
            "direct": all(p["http"] for p in parts),
            "filesize": sum(p["filesize"] for p in parts),
        })
    return targets, cookies


def download_target(t: dict, dest: Path, cookies, connections: int, job, cancel):
    """Download every part of a target with the multi-connection engine, then
    join (video + audio) or convert (MP3) with FFmpeg."""
    parts = t["parts"]
    total_est = sum(p["filesize"] for p in parts)
    job.total = total_est
    base = 0
    files = []
    for i, p in enumerate(parts):
        single = len(parts) == 1 and not t["post"]
        part_dest = dest if single else dest.with_name(f"{dest.name}.p{i}.{p['ext']}")
        files.append(part_dest)
        if part_dest.exists() and not single:  # finished earlier, before a pause
            base += part_dest.stat().st_size
            continue

        def prog(done, total, base=base, p=p):
            rest = sum(q["filesize"] for q in parts[parts.index(p) + 1:])
            job.done = base + done
            job.total = max(base + (total or p["filesize"]) + rest, job.done)

        SegmentedDownloader(p["url"], part_dest, p["headers"], cookies, connections,
                            cancel, prog, max_chunk=p.get("max_chunk")).run()
        base += part_dest.stat().st_size

    if t["post"]:
        if cancel.is_set():
            raise Cancelled()
        job.state, job.detail = "Processing", ("making MP3" if t["post"] == "mp3" else "joining video and audio")
        tmp = dest.with_name(dest.name + ".tmp." + t["ext"])
        if t["post"] == "merge":
            ffmpeg_run(["-i", str(files[0]), "-i", str(files[1]), "-map", "0:v:0", "-map", "1:a:0",
                        "-c", "copy", "-movflags", "+faststart", str(tmp)])
        else:
            ffmpeg_run(["-i", str(files[0]), "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(tmp)])
        os.replace(tmp, dest)
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
    job.done = job.total = dest.stat().st_size
    return dest


def ytdlp_download(url, passcode, dest_noext: Path, connections, job, cancel, quality="Best"):
    """Fallback: let yt-dlp download (used for streaming formats)."""
    import yt_dlp

    def hook(d):
        if cancel.is_set():
            raise Cancelled()
        if d.get("status") == "downloading":
            job.done = d.get("downloaded_bytes") or 0
            job.total = d.get("total_bytes") or d.get("total_bytes_estimate") or job.total

    tmpl = str(dest_noext).replace("%", "%%") + ".%(ext)s"
    opts = base_ydl_opts()
    opts.update({
        "outtmpl": tmpl, "progress_hooks": [hook],
        "concurrent_fragment_downloads": connections,
        "retries": 10, "fragment_retries": 10, "continuedl": True,
    })
    if not ZOOM_URL_RE.match(url):
        opts.update(quality_opts(quality))
        opts["noplaylist"] = True
        if quality.startswith("Audio"):
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                       "preferredquality": "2"}]
    if passcode:
        opts["videopassword"] = passcode
    with yt_dlp.YoutubeDL(opts) as ydl:
        _ydl_extract(ydl, url, download=True)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

ACTIVE = ("Opening link", "Downloading", "Processing")


@dataclass
class Job:
    url: str
    passcode: str = ""
    label: str = ""
    state: str = "Queued"
    detail: str = ""
    done: int = 0
    total: int = 0
    speed: float = 0.0
    dests: dict = field(default_factory=dict)      # part index -> file path
    parts_done: list = field(default_factory=list)
    quality: str = "Best"
    removed: bool = False
    error_full: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    _t: float = 0.0
    _d: int = 0

    def to_json(self):
        return {"url": self.url, "passcode": self.passcode, "label": self.label,
                "state": "Done" if self.state == "Done" else
                         ("Failed" if self.state == "Failed" else "Queued"),
                "detail": self.detail if self.state in ("Failed", "Done") else "",
                "dests": self.dests, "parts_done": self.parts_done, "quality": self.quality}


INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    name = INVALID.sub("_", name).strip().rstrip(". ")
    return (name or "Video")[:150]


def human_size(n: float) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_eta(sec: float) -> str:
    if sec <= 0 or sec > 360000:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def friendly_error(e: BaseException) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    low = msg.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return "YouTube is asking to confirm you're not a bot. Wait a while, then Retry (or download fewer at once)"
    if "private video" in low or "age" in low and "sign in" in low or "members-only" in low or "join this channel" in low:
        return "This video needs a YouTube sign-in (private, members-only or age-restricted); not supported"
    if "video unavailable" in low or "has been removed" in low:
        return "Video unavailable (removed, private, or blocked in your country)"
    if "ffmpeg" in low:
        return "Couldn't join video and audio (FFmpeg). Double-click for details"
    if "captcha" in low:
        return "Zoom wants a captcha: open the link once in your browser, then Retry"
    if "passcode" in low or "password" in low:
        return "Wrong or missing passcode"
    if "unable to extract" in low or "unsupported url" in low:
        return "The site's page wasn't understood (it may have changed). Double-click for details"
    if "requested format is not available" in low:
        return "That quality isn't available for this video. Pick another quality and Retry"
    if "404" in msg or "does not exist" in low or "not found" in low:
        return "Recording not found (link expired or deleted?)"
    if "403" in msg or "forbidden" in low:
        return "Access denied (link expired or downloads blocked)"
    if "getaddrinfo" in low or "failed to resolve" in low:
        return "Can't reach the site (no internet, or the link is mistyped)"
    if "connection" in low or "timed out" in low:
        return "Network problem: " + msg[:120]
    return msg[:200] or e.__class__.__name__


def log_error(job: Job, e: BaseException):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.ctime()} {job.label or ''} {job.url}\n")
            f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    except Exception:
        pass


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Screen size (in real pixels; the process is DPI-aware)
# --------------------------------------------------------------------------

# Tests set this to (x, y, width, height) to pretend to be another screen.
SCREEN_OVERRIDE: tuple[int, int, int, int] | None = None


def work_area(root=None) -> tuple[int, int, int, int]:
    """Usable area of the main screen (without the taskbar)."""
    if SCREEN_OVERRIDE:
        return SCREEN_OVERRIDE
    try:
        rect = (ctypes.c_long * 4)()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 40


def desktop_area(root=None) -> tuple[int, int, int, int]:
    """Bounding box of all monitors together."""
    if SCREEN_OVERRIDE:
        return SCREEN_OVERRIDE
    try:
        m = ctypes.windll.user32.GetSystemMetrics
        if m(78) > 0:
            return m(76), m(77), m(78), m(79)  # SM_X/Y/CX/CYVIRTUALSCREEN
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

def run_gui(on_ready=None, tk_scaling: float | None = None):
    """Open the window. Tests pass ``tk_scaling`` to imitate Windows display scaling."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            self.cfg = load_config()
            self.jobs: dict[str, Job] = {}
            self.paused_all = False
            self.reserved: set[str] = set()
            self.name_lock = threading.Lock()
            self.row_cache: dict[str, tuple] = {}
            self.dirty = False
            self.engine_msg: str | None = None
            self.pending_adds: queue.Queue = queue.Queue()  # videos found in playlists

            root.title(f"{APP_NAME} {APP_VERSION}")
            try:
                icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "app.ico"
                if icon.exists():
                    root.iconbitmap(default=str(icon))
            except Exception:
                pass
            # 1.0 at 100% Windows scaling, 1.5 at 150%, ...
            self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
            self._place_window()
            self._build()
            self._restore_queue()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(300, self.tick)
            if "out_dir" not in self.cfg:
                root.after(500, self.first_run)
            threading.Thread(target=self.update_engine, daemon=True).start()

        # -- window size & position -----------------------------------------
        def _place_window(self):
            """Fit the window to this screen: ~90% of a small laptop, a comfortable
            size on big monitors, and never off-screen (e.g. after unplugging a monitor)."""
            root, s = self.root, self.scale
            wx, wy, ww, wh = work_area(root)
            w = min(int(1060 * s), int(ww * 0.94))
            h = min(int(780 * s), int(wh * 0.94))
            root.minsize(min(int(620 * s), ww), min(int(430 * s), wh))
            saved = self.cfg.get("geometry", "")
            m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", saved)
            if m:
                gw, gh, gx, gy = map(int, m.groups())
                vx, vy, vw, vh = desktop_area(root)
                title_visible = (vx - 50 <= gx and gx + 150 <= vx + vw and vy - 10 <= gy and gy + 60 <= vy + vh)
                if title_visible and gw <= vw and gh <= vh:
                    root.geometry(saved)
                    if self.cfg.get("maximized"):
                        root.after(50, lambda: root.state("zoomed"))
                    return
            root.geometry(f"{w}x{h}+{wx + (ww - w) // 2}+{wy + max(0, (wh - h) // 2)}")

        # -- layout ---------------------------------------------------------
        def _build(self):
            root, s = self.root, self.scale
            style = ttk.Style()
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass
            style.configure("Treeview", rowheight=int(26 * s))
            style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

            pad = {"padx": 10, "pady": 5}
            # Short screens (small laptops, or high scaling) get a shorter paste box
            # so the download list keeps enough room.
            logical_h = work_area(root)[3] / s
            box_lines = 6 if logical_h >= 860 else (4 if logical_h >= 700 else 3)

            # Status bar first, so it can never be pushed off the bottom.
            self.status = tk.StringVar(value="Ready.")
            ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken",
                      padding=(8, 3)).pack(fill="x", side="bottom")

            top = ttk.LabelFrame(root, text=" 1. Paste links: Zoom recordings (with passcodes), "
                                            "YouTube videos or playlists ")
            top.pack(fill="x", **pad)
            self.text = tk.Text(top, height=box_lines, wrap="word", font=("Segoe UI", 10), undo=True,
                                relief="solid", borderwidth=1)
            self.text.pack(fill="x", padx=8, pady=(6, 4))
            self.text.bind("<Control-Return>", lambda e: (self.add_from_box(), "break")[1])
            row = ttk.Frame(top)
            row.pack(fill="x", padx=8, pady=(0, 8))
            ttk.Button(row, text="Add to queue and download  (Ctrl+Enter)", style="Accent.TButton",
                       command=self.add_from_box).pack(side="left")
            ttk.Button(row, text="Import from text file…", command=self.import_file).pack(side="left", padx=6)
            ttk.Button(row, text="Clear box", command=lambda: self.text.delete("1.0", "end")).pack(side="left")

            opts = ttk.LabelFrame(root, text=" 2. Settings ")
            opts.pack(fill="x", **pad)
            self.out_var = tk.StringVar(value=self.cfg.get("out_dir", str(DEFAULT_OUT)))
            self.par_var = tk.IntVar(value=int(self.cfg.get("parallel", 3)))
            self.con_var = tk.IntVar(value=int(self.cfg.get("connections", 16)))
            ttk.Label(opts, text="Save to:").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=6)
            ttk.Entry(opts, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", pady=6)
            ttk.Button(opts, text="Browse…", command=self.pick_folder).grid(row=0, column=2, padx=4)
            ttk.Button(opts, text="Open folder", command=self.open_folder).grid(row=0, column=3, padx=(0, 8))
            sub = ttk.Frame(opts)
            sub.grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 4))
            ttk.Label(sub, text="Videos at the same time:").pack(side="left")
            ttk.Spinbox(sub, from_=1, to=10, width=4, textvariable=self.par_var).pack(side="left", padx=(4, 18))
            ttk.Label(sub, text="Connections per video:").pack(side="left")
            ttk.Spinbox(sub, from_=1, to=32, width=4, textvariable=self.con_var).pack(side="left", padx=4)
            ttk.Label(sub, text="(more = faster)", foreground="#666").pack(side="left", padx=8)
            yt = ttk.Frame(opts)
            yt.grid(row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))
            q = self.cfg.get("quality", "Best")
            self.q_var = tk.StringVar(value=q if q in QUALITIES else "Best")
            ttk.Label(yt, text="YouTube quality:").pack(side="left")
            ttk.Combobox(yt, textvariable=self.q_var, values=QUALITIES, state="readonly",
                         width=17).pack(side="left", padx=(4, 10))
            ttk.Label(yt, text="Only download videos you have the right to save.",
                      foreground="#666").pack(side="left")
            self.q_var.trace_add("write", lambda *a: setattr(self, "dirty", True))
            opts.columnconfigure(1, weight=1)

            mid = ttk.LabelFrame(root, text=" 3. Downloads ")
            mid.pack(fill="both", expand=True, **pad)
            bar = ttk.Frame(mid)
            bar.pack(fill="x", padx=8, pady=(6, 4))
            self.pause_btn = ttk.Button(bar, text="Pause all", command=self.toggle_pause)
            self.pause_btn.pack(side="left")
            ttk.Button(bar, text="Retry failed", command=self.retry_failed).pack(side="left", padx=6)
            ttk.Button(bar, text="Remove selected", command=self.remove_selected).pack(side="left")
            ttk.Button(bar, text="Clear finished", command=self.clear_finished).pack(side="left", padx=6)

            cols = ("name", "status", "progress", "size", "speed", "eta")
            frame = ttk.Frame(mid)
            frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended", height=3)
            heads = {"name": ("Video", 280), "status": ("Status", 210), "progress": ("Progress", 180),
                     "size": ("Size", 75), "speed": ("Speed", 85), "eta": ("Time left", 75)}
            for c in cols:
                text, w = heads[c]
                self.tree.heading(c, text=text)
                self.tree.column(c, width=int(w * s), minwidth=int(50 * s), stretch=(c in ("name", "status")),
                                 anchor="w" if c in ("name", "status", "progress") else "e")
            sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            self.tree.tag_configure("done", foreground="#0a7d32")
            self.tree.tag_configure("failed", foreground="#c62828")
            self.tree.tag_configure("paused", foreground="#8a6d00")
            self.tree.bind("<Double-1>", self.on_double)
            self.tree.bind("<Button-3>", self.on_right_click)
            self.tree.bind("<Delete>", lambda e: self.remove_selected())

            self.menu = tk.Menu(root, tearoff=0)
            self.menu.add_command(label="Open file", command=self.open_selected_file)
            self.menu.add_command(label="Show in folder", command=self.show_selected_in_folder)
            self.menu.add_separator()
            self.menu.add_command(label="Pause", command=lambda: self.set_selected(pause=True))
            self.menu.add_command(label="Resume / Retry", command=lambda: self.set_selected(pause=False))
            self.menu.add_command(label="Copy link", command=self.copy_link)
            self.menu.add_command(label="Show error details", command=self.show_error)
            self.menu.add_separator()
            self.menu.add_command(label="Remove", command=self.remove_selected)

        # -- queue management -----------------------------------------------
        def add_job(self, url, passcode="", label="", state="Queued", detail="",
                    dests=None, parts_done=None, quality=None) -> str | None:
            if any(j.url == url and not j.removed for j in self.jobs.values()):
                return None
            job = Job(url=url, passcode=passcode, label=label, state=state, detail=detail,
                      dests={int(k): v for k, v in (dests or {}).items()},
                      parts_done=list(parts_done or []),
                      quality=quality or self.q_var.get())
            iid = self.tree.insert("", "end", values=(label or url, state, "", "", "", ""))
            self.jobs[iid] = job
            self.dirty = True
            return iid

        def add_from_box(self):
            items = parse_links(self.text.get("1.0", "end"))
            if not items:
                messagebox.showinfo(APP_NAME, "No links found. Paste Zoom recording links (with "
                                              "their passcodes) or YouTube links into the box.")
                return
            added = 0
            no_pass = 0
            for it in items:
                if is_youtube(it["url"]):
                    # YouTube: use the video's real title; nothing needs a passcode.
                    it["label"], it["passcode"] = "", ""
                elif ZOOM_URL_RE.match(it["url"]) and not it["passcode"]:
                    no_pass += 1
                if self.add_job(it["url"], it["passcode"], it["label"]):
                    added += 1
            self.text.delete("1.0", "end")
            msg = f"Added {added} link(s)."
            if added < len(items):
                msg += f" {len(items) - added} already in the list."
            if no_pass:
                msg += f" {no_pass} Zoom link(s) had no passcode (fine if they don't need one)."
            self.status.set(msg)
            if self.paused_all:
                self.toggle_pause()

        def import_file(self):
            path = filedialog.askopenfilename(title="Choose a text file with links",
                                              filetypes=[("Text files", "*.txt *.csv"), ("All files", "*.*")])
            if path:
                self.text.insert("end", "\n" + Path(path).read_text("utf-8", errors="replace"))
                self.add_from_box()

        def _restore_queue(self):
            for j in self.cfg.get("queue", []):
                self.add_job(j["url"], j.get("passcode", ""), j.get("label", ""),
                             j.get("state", "Queued"), j.get("detail", ""),
                             j.get("dests"), j.get("parts_done"), j.get("quality", "Best"))
            if self.cfg.get("queue"):
                self.status.set("Restored your list from last time. Unfinished downloads resume automatically.")

        def selected_jobs(self):
            return [(iid, self.jobs[iid]) for iid in self.tree.selection() if iid in self.jobs]

        def toggle_pause(self):
            self.paused_all = not self.paused_all
            self.pause_btn.config(text="Resume all" if self.paused_all else "Pause all")
            for job in self.jobs.values():
                if self.paused_all and job.state in ACTIVE + ("Queued",):
                    job.cancel.set()
                    if job.state == "Queued":
                        job.state = "Paused"
                elif not self.paused_all and job.state == "Paused":
                    job.state = "Queued"

        def set_selected(self, pause: bool):
            for _, job in self.selected_jobs():
                if pause and job.state in ACTIVE + ("Queued",):
                    job.cancel.set()
                    if job.state == "Queued":
                        job.state = "Paused"
                elif not pause and job.state in ("Paused", "Failed"):
                    job.state, job.detail = "Queued", ""
            if not pause and self.paused_all:
                self.paused_all = False
                self.pause_btn.config(text="Pause all")

        def retry_failed(self):
            for job in self.jobs.values():
                if job.state == "Failed":
                    job.state, job.detail = "Queued", ""

        def remove_selected(self):
            sel = self.selected_jobs()
            if not sel:
                return
            for iid, job in sel:
                job.removed = True
                if job.state in ACTIVE:
                    job.cancel.set()  # worker cleans up its partial file
                elif job.state != "Done":
                    self._delete_partials(job)
                self.tree.delete(iid)
                del self.jobs[iid]
                self.row_cache.pop(iid, None)
            self.dirty = True

        def clear_finished(self):
            for iid in [i for i, j in self.jobs.items() if j.state == "Done"]:
                self.tree.delete(iid)
                del self.jobs[iid]
                self.row_cache.pop(iid, None)
            self.dirty = True

        @staticmethod
        def _delete_partials(job: Job):
            for p in job.dests.values():
                dest = Path(p)
                leftovers = [Path(p + ".part"), Path(p + ".part.json")]
                leftovers += list(dest.parent.glob(glob_escape(dest.name) + ".p*"))   # YouTube video/audio pieces
                leftovers += list(dest.parent.glob(glob_escape(dest.name) + ".tmp.*"))
                for f in leftovers:
                    try:
                        f.unlink()
                    except OSError:
                        pass

        # -- worker ---------------------------------------------------------
        def reserve(self, job: Job, k: int, name: str, ext: str) -> Path:
            if k in job.dests:
                return Path(job.dests[k])
            out = Path(self.out_var.get()).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            base = safe_name(name)
            with self.name_lock:
                n = 1
                while True:
                    cand = out / (f"{base}.{ext}" if n == 1 else f"{base} ({n}).{ext}")
                    key = str(cand).lower()
                    if key not in self.reserved and not cand.exists() and \
                            not Path(str(cand) + ".part").exists():
                        self.reserved.add(key)
                        job.dests[k] = str(cand)
                        self.dirty = True
                        return cand
                    n += 1

        def run_job(self, job: Job):
            try:
                connections = max(1, min(32, int(self.con_var.get())))
            except Exception:
                connections = 16
            try:
                job.detail = ""
                if is_youtube(job.url) and not is_youtube_single(job.url):
                    # Playlist / channel: one row per video, added below.
                    job.detail = "reading playlist…"
                    entries = list_playlist(job.url)
                    if job.cancel.is_set():
                        raise Cancelled()
                    for link, title in entries:
                        self.pending_adds.put((link, title, job.quality))
                    job.state, job.detail = "Done", f"playlist: {len(entries)} videos added below"
                    return
                if is_youtube(job.url):
                    connections = min(connections, 8)  # YouTube dislikes many connections per address
                targets, cookies = resolve(job.url, job.passcode, job.quality)
                if job.cancel.is_set():
                    raise Cancelled()
                if all(t["direct"] for t in targets):
                    for k, t in enumerate(targets):
                        if k in job.parts_done:
                            continue
                        name = job.label or t["title"] or "Video"
                        if len(targets) > 1:
                            name += f" - part {k + 1}"
                        dest = self.reserve(job, k, name, t["ext"])
                        job.state, job.done, job.total = "Downloading", 0, t["filesize"] or 0
                        job.detail = f"part {k + 1} of {len(targets)}" if len(targets) > 1 else ""
                        download_target(t, dest, cookies, connections, job, job.cancel)
                        job.parts_done.append(k)
                        self.dirty = True
                else:
                    name = job.label or targets[0]["title"] or "Video"
                    ext = "mp3" if job.quality.startswith("Audio") and not ZOOM_URL_RE.match(job.url) else "mp4"
                    dest = self.reserve(job, 0, name, ext)
                    job.state = "Downloading"
                    ytdlp_download(job.url, job.passcode, dest.with_suffix(""), connections, job,
                                   job.cancel, job.quality)
                    job.parts_done.append(0)
                job.state, job.detail = "Done", ""
                if job.total:
                    job.done = job.total
            except BaseException as e:
                if job.cancel.is_set() or isinstance(e, Cancelled):
                    job.state = "Paused"
                    if job.removed:
                        self._delete_partials(job)
                else:
                    job.state, job.detail = "Failed", friendly_error(e)
                    job.error_full = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
                    log_error(job, e)
            finally:
                with self.name_lock:
                    for p in job.dests.values():
                        self.reserved.discard(p.lower())
                self.dirty = True

        def start_job(self, job: Job):
            job.cancel = threading.Event()
            job.state, job.speed, job._t, job._d = "Opening link", 0.0, time.monotonic(), job.done
            threading.Thread(target=self.run_job, args=(job,), daemon=True).start()

        # -- periodic refresh -----------------------------------------------
        def tick(self):
            try:
                self._tick()
            finally:
                self.root.after(400, self.tick)

        def _tick(self):
            now = time.monotonic()
            added = 0
            while not self.pending_adds.empty():
                link, title, quality = self.pending_adds.get_nowait()
                if self.add_job(link, "", title, quality=quality):
                    added += 1
            if added:
                self.status.set(f"Added {added} video(s) from the playlist.")
                self.msg_until = now + 4
            try:
                parallel = max(1, min(10, int(self.par_var.get())))
            except Exception:
                parallel = 3
            running = [j for j in self.jobs.values() if j.state in ACTIVE]
            if not self.paused_all:
                for iid in self.tree.get_children():
                    if len(running) >= parallel:
                        break
                    job = self.jobs.get(iid)
                    if job and job.state == "Queued":
                        self.start_job(job)
                        running.append(job)

            total_speed = 0.0
            counts = {"Done": 0, "Failed": 0}
            for iid, job in self.jobs.items():
                counts[job.state] = counts.get(job.state, 0) + 1
                if job.state == "Downloading":
                    dt = now - job._t
                    if dt >= 0.8:
                        inst = max(0.0, (job.done - job._d) / dt)
                        job.speed = inst if job.speed == 0 else 0.6 * job.speed + 0.4 * inst
                        job._t, job._d = now, job.done
                    total_speed += job.speed
                else:
                    job.speed = 0.0
                    job._t, job._d = now, job.done

                pct = (job.done / job.total * 100) if job.total else 0.0
                if job.state == "Done":
                    pct = 100.0
                filled = int(pct / 12.5)
                # Number first: if the column is narrow, the end of the bar is cut, not the %.
                progress = (f"{pct:5.1f}%  " + "█" * filled + "─" * (8 - filled)) \
                    if (job.total or job.state == "Done") and not job.detail.startswith("playlist") else ""
                status = job.state + (f" – {job.detail}" if job.detail else "")
                speed = (human_size(job.speed) + "/s") if job.state == "Downloading" and job.speed else ""
                eta = human_eta((job.total - job.done) / job.speed) \
                    if job.state == "Downloading" and job.speed > 0 and job.total else ""
                name = job.label or (Path(job.dests[0]).stem if 0 in job.dests else job.url)
                vals = (name, status, progress, human_size(job.total), speed, eta)
                tag = {"Done": "done", "Failed": "failed", "Paused": "paused"}.get(job.state, "")
                if self.row_cache.get(iid) != (vals, tag):
                    self.tree.item(iid, values=vals, tags=(tag,) if tag else ())
                    self.row_cache[iid] = (vals, tag)

            active = len([j for j in self.jobs.values() if j.state in ACTIVE])
            waiting = counts.get("Queued", 0)
            if self.engine_msg:
                self.status.set(self.engine_msg)
                self.engine_msg = None
                self.msg_until = now + 10
            elif now < getattr(self, "msg_until", 0):
                pass
            elif active or waiting:
                self.status.set(f"Downloading {active}, waiting {waiting}, finished {counts['Done']}, "
                                f"failed {counts['Failed']}   •   Total speed: {human_size(total_speed) or '0 B'}/s")
            elif self.jobs and not self.status.get().startswith(("Added", "Restored", "Engine")):
                self.status.set(f"All done. Finished {counts['Done']}, failed {counts['Failed']}. "
                                f"Files are in: {self.out_var.get()}")

            if self.dirty:
                self.dirty = False
                self.save()

        # -- misc actions ---------------------------------------------------
        def save(self):
            state = self.root.state()
            self.cfg.update({
                "out_dir": self.out_var.get(),
                "parallel": self._int(self.par_var, 3),
                "connections": self._int(self.con_var, 16),
                "quality": self.q_var.get(),
                "maximized": state == "zoomed",
                "queue": [self.jobs[i].to_json() for i in self.tree.get_children() if i in self.jobs],
            })
            if state == "normal":
                self.cfg["geometry"] = self.root.geometry()
            save_config(self.cfg)

        @staticmethod
        def _int(var, default):
            try:
                return int(var.get())
            except Exception:
                return default

        def pick_folder(self):
            d = filedialog.askdirectory(initialdir=self.out_var.get(), title="Where to save videos")
            if d:
                self.out_var.set(d)
                self.dirty = True

        def open_folder(self):
            p = Path(self.out_var.get()).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            os.startfile(p)

        def _first_file(self):
            for _, job in self.selected_jobs():
                for k in sorted(job.dests):
                    return job, Path(job.dests[k])
            return None, None

        def open_selected_file(self):
            job, p = self._first_file()
            if p and p.exists():
                os.startfile(p)

        def show_selected_in_folder(self):
            job, p = self._first_file()
            if p:
                target = p if p.exists() else p.parent
                subprocess.Popen(["explorer", "/select,", str(target)])

        def copy_link(self):
            sel = self.selected_jobs()
            if sel:
                self.root.clipboard_clear()
                self.root.clipboard_append("\n".join(j.url for _, j in sel))

        def on_double(self, event):
            iid = self.tree.identify_row(event.y)
            job = self.jobs.get(iid)
            if job and job.state == "Done":
                self.tree.selection_set(iid)
                self.open_selected_file()
            elif job and job.state == "Failed":
                self.tree.selection_set(iid)
                self.show_error()

        def show_error(self):
            for _, job in self.selected_jobs():
                if job.state == "Failed":
                    messagebox.showerror(
                        APP_NAME,
                        f"{job.label or job.url}\n\n{job.detail}\n\nFull message:\n"
                        f"{job.error_full or job.detail}\n\nError log: {LOG_PATH}")
                    return

        def on_right_click(self, event):
            iid = self.tree.identify_row(event.y)
            if iid:
                if iid not in self.tree.selection():
                    self.tree.selection_set(iid)
                self.menu.tk_popup(event.x_root, event.y_root)

        def update_engine(self):
            """Keep yt-dlp current (Zoom changes its site now and then). Once a day."""
            if time.time() - float(self.cfg.get("engine_checked", 0)) < 24 * 3600:
                return
            try:
                new = fetch_engine_update()
                if new:
                    self.engine_msg = (f"Engine updated to {new}. It takes effect the "
                                       "next time you open the app.")
                self.cfg["engine_checked"] = time.time()
                self.dirty = True
            except Exception:
                pass

        def first_run(self):
            messagebox.showinfo(
                APP_NAME, "Welcome!\n\nFirst, choose the folder where your downloaded "
                          "videos should be saved.\n\nYou can change it any time under "
                          "Settings → Save to.")
            start = DEFAULT_OUT if DEFAULT_OUT.exists() else DEFAULT_OUT.parent
            d = filedialog.askdirectory(initialdir=str(start), mustexist=False,
                                        title="Choose where to save your videos")
            if d:
                self.out_var.set(str(Path(d)))
            self.status.set(f"Videos will be saved to: {self.out_var.get()}")
            self.dirty = True

        def on_close(self):
            active = [j for j in self.jobs.values() if j.state in ACTIVE]
            if active and not messagebox.askyesno(
                    APP_NAME, f"{len(active)} download(s) are running.\n\n"
                              "Quit anyway? They will resume from where they stopped "
                              "next time you open the app."):
                return
            for j in active:
                j.cancel.set()
                j.state = "Queued"
            self.save()
            self.root.destroy()

    root = tk.Tk()
    if tk_scaling:
        root.tk.call("tk", "scaling", tk_scaling)
    app = App(root)
    if on_ready:
        root.after(100, lambda: on_ready(app, root))
    root.mainloop()
    return app


def write_engine_info(path: str):
    """Diagnostics (used by the build tests): which engine is loaded and does it work."""
    import yt_dlp
    info = {"version": yt_dlp.version.__version__, "file": yt_dlp.__file__,
            "active_engine": str(ACTIVE_ENGINE) if ACTIVE_ENGINE else None,
            "app_version": APP_VERSION}
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            info["zoom_extractor"] = ydl.get_info_extractor("Zoom").IE_NAME
        import requests  # noqa: F401
        for name in ("ffmpeg", "deno"):
            p = tool_path(name)
            info[name] = str(p) if p else None
            if p:
                r = subprocess.run([str(p), "-version" if name == "ffmpeg" else "--version"],
                                   capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60)
                info[name + "_version"] = (r.stdout.splitlines() or [""])[0]
        try:
            import yt_dlp_ejs
            info["ejs"] = yt_dlp_ejs.version
        except Exception:
            info["ejs"] = None
        info["ok"] = bool(info["ffmpeg"] and info["deno"])
    except Exception as e:
        info["ok"] = False
        info["error"] = repr(e)
    Path(path).write_text(json.dumps(info, indent=2), "utf-8")


if __name__ == "__main__":
    use_newest_engine()
    if "--engine-info" in sys.argv:
        write_engine_info(sys.argv[sys.argv.index("--engine-info") + 1])
    else:
        run_gui()
