import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from surya_distro import render

FAKE = r'''#!%(py)s
import base64, json, os, struct, sys, zlib
log = open(os.environ["FAKE_LOG"], "a")
buf = b""
size = [1, 1]
shade = [32]
def png(w, h):
    raw = b"".join(b"\x00" + bytes([shade[0]]) * 3 * w for _ in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
def reply(obj):
    os.write(4, json.dumps(obj).encode() + b"\0")
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        m = json.loads(raw)
        log.write(raw.decode() + "\n"); log.flush()
        meth, p = m["method"], m.get("params", {})
        res = {}
        if meth == "Target.createTarget": res = {"targetId": "T1"}
        elif meth == "Target.attachToTarget": res = {"sessionId": "S1"}
        elif meth == "Emulation.setDeviceMetricsOverride":
            size[:] = [round(p["width"] * p["deviceScaleFactor"]), round(p["height"] * p["deviceScaleFactor"])]
        elif meth == "Page.navigate":
            import urllib.parse
            path = urllib.parse.unquote(p["url"][len("file://"):])
            shade[0] = sum(open(path, "rb").read()) %% 200
        elif meth == "Runtime.evaluate": res = {"result": {"type": "boolean", "value": True}}
        elif meth == "Page.captureScreenshot": res = {"data": base64.b64encode(png(*size)).decode()}
        out = {"id": m["id"], "result": res}
        if "sessionId" in m: out["sessionId"] = m["sessionId"]
        reply(out)
        if meth == "Page.navigate":
            reply({"method": "Page.loadEventFired", "params": {}, "sessionId": "S1"})
'''


class Fake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.chrome = self.dir / "fakechrome"
        self.chrome.write_text(FAKE % {"py": sys.executable})
        self.chrome.chmod(self.chrome.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "log.txt"
        os.environ["FAKE_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "FAKE_LOG", None)
        self.html = self.dir / "p.html"
        self.html.write_text("<html></html>")

    def sent(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]


class Protocol(Fake):
    def test_framing_is_json_then_nul(self):
        self.assertEqual(render.frame_message({"id": 1, "method": "A.b"}), b'{"id":1,"method":"A.b"}\0')

    def test_pipe_splits_messages_and_keeps_partials(self):
        r, w = os.pipe()
        p = render.Pipe(r, os.dup(w))
        os.write(w, b'{"id":1}\0{"id"')
        self.assertEqual(p.recv(1), {"id": 1})
        os.write(w, b':2}\0')
        self.assertEqual(p.recv(1), {"id": 2})
        with self.assertRaises(render.RenderError):
            p.recv(0.05)
        os.close(w)

    def test_launch_args_put_the_pipe_on_fds_3_and_4(self):
        a = render.launch_args("/x/chrome", "/tmp/ud", 11, 12)
        self.assertEqual(a[:2], ["/bin/sh", "-c"])
        self.assertIn("3<&11 4>&12", a[2])
        self.assertIn("--remote-debugging-pipe", a)
        self.assertIn("--headless=new", a)

    def test_png_through_a_fake_chrome(self):
        out = self.dir / "o.png"
        render.render_png(self.html, out, 1200, 630, 1.0, str(self.chrome))
        self.assertEqual(render.png_size(out), (1200, 630))
        methods = [m["method"] for m in self.sent()]
        self.assertEqual(methods[:3], ["Browser.getVersion", "Target.createTarget", "Target.attachToTarget"])
        self.assertIn("Page.captureScreenshot", methods)
        metrics = next(m for m in self.sent() if m["method"] == "Emulation.setDeviceMetricsOverride")
        self.assertEqual((metrics["params"]["width"], metrics["params"]["height"]), (1200, 630))
        self.assertEqual(metrics["sessionId"], "S1")

    def test_scale_changes_the_pixels(self):
        out = self.dir / "o2.png"
        render.render_png(self.html, out, 100, 50, 2.0, str(self.chrome))
        self.assertEqual(render.png_size(out), (200, 100))

    def test_video_seeks_every_frame(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("no ffmpeg")
        out = self.dir / "o.mp4"
        render.render_video(self.html, out, 64, 64, 0.5, 10, chrome=str(self.chrome))
        seeks = [m for m in self.sent() if "currentTime" in m.get("params", {}).get("expression", "")]
        shots = [m for m in self.sent() if m["method"] == "Page.captureScreenshot"]
        self.assertEqual(len(seeks), 5)
        self.assertEqual(len(shots), 5)
        self.assertIn("const t = 100.0", seeks[1]["params"]["expression"])

    def test_missing_chrome_says_what_to_install(self):
        self.assertIsNone(render.find_chrome({}, which=lambda n: None, exists=lambda p: False, platform="linux"))
        with self.assertRaises(render.RenderUnavailable) as e:
            old = os.environ.get(render.CHROME_ENV)
            os.environ[render.CHROME_ENV] = "/nope/chrome"
            try:
                render.Browser().__enter__()
            finally:
                os.environ.pop(render.CHROME_ENV) if old is None else os.environ.__setitem__(render.CHROME_ENV, old)
        self.assertIn("SURYA_DISTRO_CHROME", str(e.exception))

    def test_finders_take_env(self):
        self.assertEqual(render.find_chrome({render.CHROME_ENV: "/a/b"}, exists=lambda p: True), "/a/b")
        self.assertEqual(render.find_ffmpeg({render.FFMPEG_ENV: "/a/ff"}, exists=lambda p: True), "/a/ff")
        self.assertEqual(render.find_chrome({}, which=lambda n: "/usr/bin/chromium" if n == "chromium" else None,
                                            exists=lambda p: False, platform="linux"), "/usr/bin/chromium")


class Ffmpeg(unittest.TestCase):
    def test_command_line(self):
        c = render.ffmpeg_command("ffmpeg", Path("o.mp4"), 2.0, 30)
        self.assertEqual(c[:2], ["ffmpeg", "-y"])
        pairs = list(zip(c, c[1:]))
        for pair in (("-f", "image2pipe"), ("-framerate", "30"), ("-c:v", "libx264"), ("-movflags", "+faststart"), ("-t", "2.000")):
            self.assertIn(pair, pairs)
        self.assertIn("format=yuv420p", c[c.index("-vf") + 1])
        self.assertEqual(c[-1], "o.mp4")
        self.assertNotIn("-filter_complex", c)

    def test_voice_is_mixed_at_each_start(self):
        c = render.ffmpeg_command("ffmpeg", Path("o.mp4"), 10, 30, [("a.aiff", 400), ("b.aiff", 5000)])
        f = c[c.index("-filter_complex") + 1]
        self.assertIn("adelay=400|400", f)
        self.assertIn("adelay=5000|5000", f)
        self.assertIn("amix=inputs=2", f)
        self.assertIn("aac", c)


@unittest.skipUnless(render.find_chrome() and shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs Chrome and ffmpeg")
class RealRender(unittest.TestCase):
    def test_png_and_video(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            h = t / "a.html"
            h.write_text("<!doctype html><body style='margin:0;background:#123'><div style='width:50px;height:50px;background:#f80;"
                         "animation:m 2s linear both'></div><style>@keyframes m{to{transform:translateX(300px)}}</style>")
            png = render.render_png(h, t / "a.png", 1200, 630)
            self.assertEqual(render.png_size(png), (1200, 630))
            mp4 = render.render_video(h, t / "a.mp4", 640, 360, 2.0, 30)
            info = render.probe(mp4)
            self.assertEqual((info["width"], info["height"], info["codec"], info["pix_fmt"]), (640, 360, "h264", "yuv420p"))
            self.assertAlmostEqual(info["seconds"], 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
