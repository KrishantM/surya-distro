"""Render HTML to a PNG or an MP4 with a browser you already have. No pip packages.

Chrome, Chromium, Edge or Brave is driven over `--remote-debugging-pipe`. That
uses two inherited file descriptors, 3 (commands in) and 4 (replies out), and
each message is one JSON object ended by a NUL byte. So this file speaks the
DevTools protocol itself and needs no websocket library.

A video is made frame by frame. Every animation on the page is paused, then
seeked to the frame's time with the Web Animations API, and the browser takes a
screenshot. The frames go to ffmpeg, which writes H.264 in yuv420p with
`+faststart`. The same page gives the same frames, because nothing runs on a
clock. Pages use system fonts and local or inlined assets, so nothing loads from
the network.
"""
from __future__ import annotations

import base64
import json
import os
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

CHROME_ENV, FFMPEG_ENV, TTS_ENV = "SURYA_DISTRO_CHROME", "SURYA_DISTRO_FFMPEG", "SURYA_DISTRO_TTS"
FPS = 30

MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
LINUX_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                  "microsoft-edge-stable", "brave-browser")

CHROME_HINT = ("Chrome, Chromium, Edge or Brave wasn't found. Install one, or set SURYA_DISTRO_CHROME to its path. "
               "On a Mac: brew install --cask google-chrome. On Debian or Ubuntu: sudo apt install chromium.")
FFMPEG_HINT = ("ffmpeg wasn't found. Install it, or set SURYA_DISTRO_FFMPEG to its path. "
               "On a Mac: brew install ffmpeg. On Debian or Ubuntu: sudo apt install ffmpeg.")


class RenderUnavailable(RuntimeError):
    """Chrome or ffmpeg is missing. The message says what to install."""


class RenderError(RuntimeError):
    pass


# ----------------------------------------------------------------- finding ---
def find_chrome(env: Optional[dict] = None, which: Callable = shutil.which, exists: Callable = os.path.exists,
                platform: str = sys.platform) -> Optional[str]:
    env = os.environ if env is None else env
    given = env.get(CHROME_ENV)
    if given:
        return given if exists(given) else (which(given) or None)
    if platform == "darwin":
        for p in MAC_BROWSERS:
            if exists(p):
                return p
    for name in LINUX_BROWSERS:
        found = which(name)
        if found:
            return found
    return None


def find_ffmpeg(env: Optional[dict] = None, which: Callable = shutil.which, exists: Callable = os.path.exists) -> Optional[str]:
    env = os.environ if env is None else env
    given = env.get(FFMPEG_ENV)
    if given:
        return given if exists(given) else (which(given) or None)
    return which("ffmpeg")


def find_ffprobe(ffmpeg: Optional[str] = None, which: Callable = shutil.which, exists: Callable = os.path.exists) -> Optional[str]:
    if ffmpeg:
        sib = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.path.dirname(ffmpeg) and exists(sib):
            return sib
    return which("ffprobe")


def need_chrome() -> str:
    c = find_chrome()
    if not c:
        raise RenderUnavailable(CHROME_HINT)
    return c


def need_ffmpeg() -> str:
    f = find_ffmpeg()
    if not f:
        raise RenderUnavailable(FFMPEG_HINT)
    return f


# ---------------------------------------------------------------- protocol ---
def frame_message(msg: dict) -> bytes:
    """One DevTools message on the pipe: JSON, then a NUL byte."""
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\0"


class Pipe:
    """Reads and writes NUL-delimited JSON on two file descriptors."""

    def __init__(self, read_fd: int, write_fd: int):
        self.r, self.w = read_fd, write_fd
        self.buf = b""

    def send(self, msg: dict) -> None:
        data = frame_message(msg)
        while data:
            n = os.write(self.w, data)
            data = data[n:]

    def recv(self, timeout: float) -> dict:
        end = time.monotonic() + timeout
        while b"\0" not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                raise RenderError("the browser didn't answer in time")
            ready, _, _ = select.select([self.r], [], [], left)
            if not ready:
                continue
            chunk = os.read(self.r, 1 << 20)
            if not chunk:
                raise RenderError("the browser closed its pipe")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw.decode("utf-8"))

    def close(self) -> None:
        for fd in (self.w, self.r):
            try:
                os.close(fd)
            except OSError:
                pass


class Session:
    """Numbered requests and replies over a Pipe. Events that arrive meanwhile are kept."""

    def __init__(self, pipe: Pipe):
        self.pipe = pipe
        self.n = 0
        self.events: List[dict] = []

    def call(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None, timeout: float = 60) -> dict:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.pipe.send(msg)
        while True:
            d = self.pipe.recv(timeout)
            if d.get("id") == self.n:
                if "error" in d:
                    raise RenderError(f"{method}: {d['error'].get('message', d['error'])}")
                return d.get("result", {})
            if "method" in d:
                self.events.append(d)

    def wait_event(self, method: str, session_id: Optional[str] = None, timeout: float = 60) -> dict:
        end = time.monotonic() + timeout
        while True:
            for i, e in enumerate(self.events):
                if e.get("method") == method and (session_id is None or e.get("sessionId") == session_id):
                    return self.events.pop(i)
            d = self.pipe.recv(max(end - time.monotonic(), 0.01))
            if "method" in d:
                self.events.append(d)


# ----------------------------------------------------------------- browser ---
def launch_args(chrome: str, user_data_dir: str, read_fd: int, write_fd: int) -> List[str]:
    """Chrome expects its pipe on fds 3 and 4. `sh` places ours there, so no preexec hook is needed."""
    flags = ["--headless=new", "--remote-debugging-pipe", f"--user-data-dir={user_data_dir}", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
             "--force-color-profile=srgb", "--font-render-hinting=none", "--disable-background-timer-throttling",
             "--disable-renderer-backgrounding", "--disable-extensions", "--allow-file-access-from-files"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        flags.append("--no-sandbox")
    return ["/bin/sh", "-c", f'exec "$@" 3<&{read_fd} 4>&{write_fd}', "sh", chrome] + flags + ["about:blank"]


class Browser:
    """A headless browser on a pipe. Leaving the block ends the pipe, which makes Chrome exit."""

    def __init__(self, chrome: Optional[str] = None):
        self.chrome = chrome or need_chrome()
        self.proc: Optional[subprocess.Popen] = None
        self.session: Optional[Session] = None
        self.dir = ""

    def __enter__(self) -> "Browser":
        self.dir = tempfile.mkdtemp(prefix="distro-chrome-")
        to_child_r, to_child_w = os.pipe()
        from_child_r, from_child_w = os.pipe()
        try:
            self.proc = subprocess.Popen(
                launch_args(self.chrome, self.dir, to_child_r, from_child_w), pass_fds=(to_child_r, from_child_w),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            os.close(to_child_r)
            os.close(from_child_w)
        self.session = Session(Pipe(from_child_r, to_child_w))
        try:
            self.session.call("Browser.getVersion", timeout=30)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.session:
            self.session.pipe.close()          # EOF on fd 3 ends Chrome
        if self.proc:
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        shutil.rmtree(self.dir, ignore_errors=True)

    def page(self, width: int, height: int, scale: float = 1.0) -> "Page":
        s = self.session
        target = s.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = s.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        s.call("Page.enable", session_id=sid)
        s.call("Emulation.setDeviceMetricsOverride", {"width": int(width), "height": int(height),
                                                      "deviceScaleFactor": scale, "mobile": False}, sid)
        return Page(s, sid)


PAUSE_JS = "document.getAnimations().forEach(a => a.pause()); document.getAnimations().length"
SEEK_JS = ("(() => { const t = %s; const all = document.getAnimations();"
           " for (const a of all) { a.pause(); a.currentTime = t; } return all.length; })()")
READY_JS = ("(async () => { await document.fonts.ready;"
            " await Promise.all([...document.images].map(i => i.decode().catch(() => null)));"
            " if (window.__ready) await window.__ready; return true; })()")


class Page:
    def __init__(self, session: Session, sid: str):
        self.s, self.sid = session, sid

    def eval(self, expr: str, wait: bool = False, timeout: float = 60):
        r = self.s.call("Runtime.evaluate", {"expression": expr, "awaitPromise": wait, "returnByValue": True},
                        self.sid, timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RenderError("the page raised an error: " + str((d.get("exception") or {}).get("description") or d.get("text")))
        return (r.get("result") or {}).get("value")

    def load(self, html_path: Path, timeout: float = 60) -> None:
        url = Path(html_path).resolve().as_uri()
        self.s.call("Page.navigate", {"url": url}, self.sid, timeout)
        self.s.wait_event("Page.loadEventFired", self.sid, timeout)
        self.eval(READY_JS, wait=True, timeout=timeout)
        self.eval(PAUSE_JS)

    def seek(self, ms: float) -> int:
        return self.eval(SEEK_JS % json.dumps(round(ms, 3)))

    def shot(self) -> bytes:
        r = self.s.call("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                        self.sid, 60)
        return base64.b64decode(r["data"])


def png_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        head = Path(path).read_bytes()[:24]
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def render_png(html: Path, out: Path, width: int, height: int, scale: float = 1.0, chrome: Optional[str] = None,
               at_ms: float = 0.0) -> Path:
    """The page at `at_ms` (the start, by default) as a PNG of width*scale by height*scale pixels."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Browser(chrome) as b:
        page = b.page(width, height, scale)
        page.load(html)
        page.seek(at_ms)
        data = page.shot()
    out.write_bytes(data)
    want = (round(width * scale), round(height * scale))
    got = png_size(out)
    if got != want:
        raise RenderError(f"{out.name} came out {got}, not {want}")
    return out


# ------------------------------------------------------------------ ffmpeg ---
def ffmpeg_command(ffmpeg: str, out: Path, seconds: float, fps: int = FPS,
                   audio: Sequence[Tuple[str, int]] = ()) -> List[str]:
    """Frames arrive as PNGs on stdin. `audio` is (file, start in ms) pairs, each placed at its start."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "png", "-i", "-"]
    for path, _ in audio:
        cmd += ["-i", str(path)]
    vf = "scale=in_range=full:out_range=limited:out_color_matrix=bt709,format=yuv420p"
    if audio:
        parts = [f"[{i + 1}:a]aresample=44100,adelay={ms}|{ms}[a{i}]" for i, (_, ms) in enumerate(audio)]
        mix = "".join(f"[a{i}]" for i in range(len(audio)))
        parts.append(f"{mix}amix=inputs={len(audio)}:duration=longest:normalize=0[aout]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[aout]"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "18", "-r", str(fps),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "44100"]
    cmd += ["-t", f"{seconds:.3f}", "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    return cmd


def render_video(html: Path, out: Path, width: int, height: int, seconds: float, fps: int = FPS,
                 audio: Sequence[Tuple[str, int]] = (), chrome: Optional[str] = None, ffmpeg: Optional[str] = None,
                 progress: Callable[[str], None] = lambda s: None) -> Path:
    chrome = chrome or need_chrome()
    ffmpeg = ffmpeg or need_ffmpeg()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = max(int(round(seconds * fps)), 1)
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(ffmpeg_command(ffmpeg, out, seconds, fps, audio), stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=errlog)
        try:
            with Browser(chrome) as b:
                page = b.page(width, height, 1.0)
                page.load(html)
                for i in range(frames):
                    page.seek(i * 1000.0 / fps)
                    proc.stdin.write(page.shot())
                    if i and i % (fps * 5) == 0:
                        progress(f"{out.name}: {i}/{frames} frames")
            proc.stdin.close()
            code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            out.unlink(missing_ok=True)
            raise
        if code != 0:
            errlog.seek(0)
            raise RenderError("ffmpeg failed: " + errlog.read().decode("utf-8", "replace")[-400:])
    return out


def probe(path: Path, ffprobe: Optional[str] = None) -> Optional[dict]:
    """Width, height, codec, pixel format and duration, from ffprobe. None when ffprobe isn't there."""
    ffprobe = ffprobe or find_ffprobe(find_ffmpeg())
    if not ffprobe:
        return None
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        j = json.loads(r.stdout)
        s = (j.get("streams") or [{}])[0]
        return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"), "pix_fmt": s.get("pix_fmt"),
                "fps": s.get("r_frame_rate"), "seconds": float((j.get("format") or {}).get("duration") or 0)}
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------- voice ---
def tts_command(env: Optional[dict] = None) -> str:
    """`say -o {out}` on a Mac, or SURYA_DISTRO_TTS. The text goes in on stdin and {out} is the file to write."""
    env = os.environ if env is None else env
    return env.get(TTS_ENV) or ("say -o {out}" if sys.platform == "darwin" else "")


def speak(text: str, out: Path, command: Optional[str] = None) -> Path:
    command = command or tts_command()
    if not command:
        raise RenderUnavailable("No voice available. On a Mac, `say` is used. Elsewhere set SURYA_DISTRO_TTS to a command "
                                "that reads text on stdin and writes the file named by {out}, such as: espeak-ng --stdin -w {out}")
    argv = [a.replace("{out}", str(out)) for a in shlex.split(command)]
    if shutil.which(argv[0]) is None and not os.path.exists(argv[0]):
        raise RenderUnavailable(f"The voice command {argv[0]!r} wasn't found.")
    r = subprocess.run(argv, input=text.encode("utf-8"), capture_output=True)
    if r.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size == 0:
        raise RenderError("the voice command failed: " + r.stderr.decode("utf-8", "replace")[-200:])
    return Path(out)
