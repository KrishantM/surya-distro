"""The app: its guards, its routes, draft saving, the media manifest, jobs and range requests.

A real server runs on 127.0.0.1 in a thread, against a temporary home. Nothing here touches the network.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from surya_distro import stages, app, cli
from surya_distro.kit import Draft, Kit

HN = {"title": "Show HN: Lantern, a log of what your agent changed", "url": "https://github.com/example/lantern",
      "text": "Lantern writes one plain file per session.\n\n[[fill: why you built it]]"}
REPO = "example/lantern"


def make_draft(home: Path, draft_id="hn", status="draft", fields=None, **meta):
    kit = Kit(home, REPO)
    d = Draft(draft_id, kit.drafts_dir / f"{draft_id}.md", {"draft": draft_id, "channel": "hn", "status": status, "created": "2026-11-12T00:00:00Z",
                                                            "playbook": "none", **meta}, dict(fields or HN))
    if status in ("approved", "posted"):
        d.meta.update(approved_at="2026-11-12T01:00:00Z", approved_hash=d.digest())
    if status == "posted":
        d.meta.update(posted_at="2026-11-13T01:00:00Z", post_url="https://news.ycombinator.com/item?id=1")
    d.save()
    return d


class Live:
    """A running app on a free port, and a small client that can send any headers."""

    def __init__(self, home, argv_for=None):
        self.distro = app.Distro(home, argv_for=argv_for)
        self.httpd = app.make_server(self.distro)
        self.port = self.distro.port
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def stop(self):
        self.distro.jobs.stop_all()
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, token=True, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Token"] = self.distro.token
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        return res.status, dict(res.getheaders()), raw

    def get(self, path, **kw):
        status, headers, raw = self.req("GET", path, **kw)
        try:
            return status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return status, raw

    def post(self, path, body, **kw):
        status, headers, raw = self.req("POST", path, body=body, **kw)
        return status, json.loads(raw.decode("utf-8"))


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        make_draft(self.home)
        self.live = Live(self.home)
        self.addCleanup(self.live.stop)

    @property
    def draft_path(self):
        return self.home / "example" / "lantern" / "kit" / "drafts" / "hn.md"


class Guards(AppCase):
    def test_the_api_needs_the_token(self):
        self.assertEqual(self.live.get("/api/state", token=False)[0], 403)
        self.assertEqual(self.live.get("/api/state", headers={"X-Token": "wrong"})[0], 403)
        status, data = self.live.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["home"], str(self.home))

    def test_post_needs_the_token_and_json(self):
        self.assertEqual(self.live.post("/api/check", {"repo": REPO}, token=False)[0], 403)
        status, _, _ = self.live.req("POST", "/api/check", body=None, headers={"Content-Type": "text/plain", "Content-Length": "2"})
        self.assertEqual(status, 415)

    def test_host_and_origin_are_checked(self):
        self.assertEqual(self.live.get("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.live.get("/api/state", host=f"127.0.0.1.evil.example:{self.live.port}")[0], 403)
        self.assertEqual(self.live.get("/api/state", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.live.get("/api/state", headers={"Origin": f"http://127.0.0.1:{self.live.port}"})[0], 200)
        self.assertEqual(self.live.get("/api/state", host=f"localhost:{self.live.port}")[0], 200)
        self.assertEqual(self.live.post("/api/check", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.live.get("/", host="evil.example")[0], 403)

    def test_the_page_has_a_strict_policy(self):
        status, headers, raw = self.live.req("GET", "/", token=False)
        self.assertEqual(status, 200)
        csp = headers["Content-Security-Policy"]
        nonce = self.live.distro.nonce
        self.assertIn("default-src 'none'", csp)
        self.assertIn(f"script-src 'nonce-{nonce}'", csp)
        self.assertIn(f"style-src 'nonce-{nonce}'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        for bad in ("unsafe-inline", "unsafe-eval", "http:", "https:", "*"):
            self.assertNotIn(bad, csp)
        self.assertIn(f'nonce="{nonce}"'.encode(), raw)
        self.assertNotIn(b"__NONCE__", raw)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_the_page_loads_nothing_from_elsewhere(self):
        html = app.PAGE.read_text(encoding="utf-8")
        for needle in ("<script src", "src=\"http", "@import", "url(http", "<link rel=\"stylesheet", "cdn."):
            self.assertNotIn(needle, html)

    def test_it_listens_on_loopback_only(self):
        self.assertEqual(self.live.httpd.server_address[0], "127.0.0.1")


class Paths(AppCase):
    def media_dir(self):
        d = self.home / "example" / "lantern" / "kit" / "media"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_bad_repos_and_draft_names_are_refused(self):
        for repo in ("../x", "a/../b", "..", "a", "a/b/c", "a/..", "", "a b/c", "/etc"):
            status, data = self.live.get("/api/launch?repo=" + repo.replace(" ", "%20"))
            self.assertEqual(status, 400, repo)
        for name in ("../hn", "..%2Fhn", "hn/../hn", "a b", ""):
            status, _ = self.live.post("/api/check", {"repo": REPO, "id": name.replace("%2F", "/"), "fields": {"title": "x"}})
            self.assertIn(status, (400, 404), name)

    def test_media_paths_cannot_leave_the_kit(self):
        outside = Path(self.tmp.name) / "secret.png"
        outside.write_bytes(b"\x89PNG secret")
        media = self.media_dir()
        (media / "ok.png").write_bytes(b"\x89PNG fine")
        (media / "link.png").symlink_to(outside)
        t = self.live.distro.token
        base = "/media/example/lantern/kit/media/"
        self.assertEqual(self.live.req("GET", f"{base}ok.png?t={t}", token=False)[0], 200)
        for path in (f"{base}link.png?t={t}", f"/media/example/lantern/kit/media/../../../../secret.png?t={t}",
                     f"/media/example/lantern/kit/media/%2e%2e/%2e%2e/%2e%2e/%2e%2e/secret.png?t={t}",
                     f"/media/example/lantern/study/x.png?t={t}", f"/media/example/lantern/kit/drafts/hn.md?t={t}",
                     f"/media/../../{outside.name}?t={t}", f"/media/example/lantern/kit/media/missing.png?t={t}",
                     f"/media/example/lantern/kit/media/%5C..%5Csecret.png?t={t}"):
            self.assertEqual(self.live.req("GET", path, token=False)[0], 404, path)

    def test_media_needs_the_token(self):
        (self.media_dir() / "ok.png").write_bytes(b"\x89PNG fine")
        self.assertEqual(self.live.req("GET", "/media/example/lantern/kit/media/ok.png", token=False)[0], 403)
        self.assertEqual(self.live.req("GET", "/media/example/lantern/kit/media/ok.png?t=nope", token=False)[0], 403)

    def test_a_manifest_path_outside_the_kit_gets_no_file(self):
        outside = Path(self.tmp.name) / "secret.png"
        outside.write_bytes(b"x")
        (self.media_dir() / "media.json").write_text(json.dumps({"version": 1, "assets": [
            {"id": "a", "kind": "image", "path": "../../../../../secret.png"}, {"id": "b", "kind": "image", "path": str(outside)}]}))
        for a in self.live.get(f"/api/media?repo={REPO}")[1]["assets"]:
            self.assertEqual(a["src"], "", a["id"])
            self.assertTrue(a["problem"])

    def test_only_media_types_are_served(self):
        (self.media_dir() / "notes.txt").write_text("hello")
        self.assertEqual(self.live.req("GET", f"/media/example/lantern/kit/media/notes.txt?t={self.live.distro.token}", token=False)[0], 404)


class NoApproveNoSend(AppCase):
    def test_the_route_tables_have_no_approve_or_send(self):
        for route in app.GET_ROUTES + app.POST_ROUTES:
            for word in ("approve", "posted", "send", "submit", "publish", "outbox", "reply", "comment"):
                self.assertNotIn(word, route)
        self.assertEqual(set(app.POST_ROUTES), {"check", "draft/save", "run", "cancel"})
        self.assertEqual({m for m in dir(app.Handler) if m.startswith("do_")}, {"do_GET", "do_POST"})

    def test_no_such_route_answers(self):
        for route in ("approve", "posted", "send", "outbox", "draft/approve", "draft/post"):
            status, data = self.live.post("/api/" + route, {"repo": REPO, "id": "hn", "draft": "hn"})
            self.assertEqual(status, 404, route)
            self.assertEqual(self.live.get("/api/" + route + "?repo=" + REPO)[0], 404, route)
        for method in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.live.req(method, "/api/state")[0], 501)

    def test_steps_that_approve_or_post_cannot_run(self):
        self.assertEqual(set(app.RUNNABLE), {"study", "strategy", "draft", "check", "create", "targets", "measure", "learn"})
        for step in ("approve", "posted", "outbox", "schedule", "../approve", "check; approve"):
            status, data = self.live.post("/api/run", {"repo": REPO, "step": step})
            self.assertEqual(status, 403, step)
        self.assertEqual(self.live.distro.jobs.items, {})

    def test_the_cli_it_runs_is_the_package_with_the_same_home(self):
        d = app.Distro(self.home, engine="heuristic", memory="off")
        argv = d.argv_for("check", REPO, ["hn"])
        self.assertEqual(argv[:3], [sys.executable, "-m", "surya_distro"])
        self.assertEqual(argv[3:], ["--home", str(self.home), "--engine", "heuristic", "--memory", "off", "check", REPO, "hn"])

    def test_no_approve_by_a_check_run_with_a_draft_name(self):
        for draft in ("--approve", "-h", "a b"):
            self.assertEqual(self.live.post("/api/run", {"repo": REPO, "step": "check", "draft": draft})[0], 400, draft)
        for repo in ("-h/x", "x/--version", "--help/x"):
            self.assertEqual(self.live.post("/api/run", {"repo": repo, "step": "study"})[0], 400, repo)

    def test_approval_commands_are_text_for_your_terminal(self):
        data = self.live.get(f"/api/drafts?repo={REPO}")[1]
        self.assertEqual(data["drafts"][0]["approve_command"], f"surya-distro --home {app.shell_quote(str(self.home))} approve {REPO} hn")
        self.assertIn("approve", self.live.get(f"/api/playbook?repo={REPO}")[1]["approve_command"])


class SaveDraft(AppCase):
    def fields(self, **over):
        return {**HN, **over}

    def test_check_does_not_write(self):
        before = self.draft_path.read_text()
        status, res = self.live.post("/api/check", {"repo": REPO, "id": "hn", "fields": {"title": "Show HN: " + "x" * 90}})
        self.assertEqual(status, 200)
        messages = [i["message"] for i in res["issues"]]
        self.assertTrue(any("the limit is 80" in m for m in messages), messages)
        self.assertTrue(any("fill in" in m for m in messages))
        self.assertEqual([c for c in res["counts"] if c["field"] == "title"][0]["max"], 80)
        self.assertEqual(self.draft_path.read_text(), before)

    def test_saving_a_draft_keeps_it_a_draft(self):
        status, res = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": "Plain words."}})
        self.assertEqual(status, 200)
        self.assertTrue(res["saved"])
        self.assertFalse(res["reset"])
        self.assertEqual(Draft.load(self.draft_path).fields["text"], "Plain words.")
        self.assertEqual(Draft.load(self.draft_path).state, "draft")

    def test_saving_an_approved_draft_resets_it_and_says_so_first(self):
        make_draft(self.home, status="approved")
        self.assertEqual(Draft.load(self.draft_path).state, "approved")
        before = self.draft_path.read_text()
        status, data = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": "Changed."}})
        self.assertEqual(status, 409)
        self.assertIn("resets it to draft", data["error"])
        self.assertEqual(self.draft_path.read_text(), before)
        self.assertTrue(self.live.post("/api/check", {"repo": REPO, "id": "hn", "fields": {"text": "Changed."}})[1]["resets"])
        status, res = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": "Changed."}, "reset_ok": True})
        self.assertEqual(status, 200)
        self.assertTrue(res["reset"])
        d = Draft.load(self.draft_path)
        self.assertEqual(d.state, "draft")
        self.assertEqual(d.meta["status"], "draft")
        self.assertNotIn("approved_hash", d.meta)
        self.assertNotIn("approved_at", d.meta)
        self.assertEqual(res["draft"]["state"], "draft")

    def test_saving_an_approved_draft_without_a_change_keeps_the_approval(self):
        make_draft(self.home, status="approved")
        status, res = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": HN["text"]}})
        self.assertEqual(status, 200)
        self.assertFalse(res["saved"])
        self.assertEqual(Draft.load(self.draft_path).state, "approved")

    def test_a_posted_draft_is_not_edited(self):
        make_draft(self.home, status="posted")
        before = self.draft_path.read_text()
        status, data = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": "Rewritten."}, "reset_ok": True})
        self.assertEqual(status, 409)
        self.assertEqual(self.draft_path.read_text(), before)

    def test_bad_fields_are_refused(self):
        for fields in ({"nope": "x"}, {"text": 5}, {"text": "a\n::: url\nhttps://evil.example"}, {}, "text", {"text": "x" * (app.MAX_FIELD + 1)}):
            status, _ = self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": fields})
            self.assertIn(status, (400, 413), str(fields)[:30])
        self.assertEqual(Draft.load(self.draft_path).fields, HN)

    def test_a_draft_is_written_inside_its_folder_only(self):
        self.live.post("/api/draft/save", {"repo": REPO, "id": "hn", "fields": {"text": "One."}})
        files = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*") if p.is_file())
        self.assertTrue(all(f.startswith("example/lantern/") for f in files), files)


class Manifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_dir = Path(self.tmp.name) / "example" / "lantern"
        self.media = self.repo_dir / "kit" / "media"
        self.media.mkdir(parents=True)

    def write(self, text):
        (self.media / "media.json").write_text(text)

    def test_missing_is_empty_not_an_error(self):
        m = app.read_manifest(self.repo_dir)
        self.assertEqual((m["exists"], m["error"], m["assets"]), (False, "", []))

    def test_malformed_is_an_error_not_a_crash(self):
        for text in ("{not json", "[]", '{"assets": 5}', '"x"', ""):
            self.write(text)
            m = app.read_manifest(self.repo_dir)
            self.assertTrue(m["exists"])
            self.assertTrue(m["error"], text)
            self.assertEqual(m["assets"], [])

    def test_the_documented_shape_reads(self):
        (self.media / "a.png").write_bytes(b"png")
        (self.media / "b.mp4").write_bytes(b"mp4")
        self.write(json.dumps({"version": 1, "assets": [
            {"id": "a", "channel": "x", "kind": "image", "path": "a.png", "html": "a.html", "width": 1200, "height": 630, "seconds": None,
             "template": "card", "follows": ["x.kind.release"], "status": "ready", "sha256": "ab" * 32, "created": "2026-11-13T00:00:00Z"},
            {"id": "b", "channel": "bluesky", "kind": "video", "path": "b.mp4", "width": 640, "height": 360, "seconds": 6.5, "follows": "draft:bluesky"}]}))
        m = app.read_manifest(self.repo_dir)
        self.assertEqual(m["error"], "")
        a, b = m["assets"]
        self.assertEqual((a["kind"], a["src"], a["width"], a["seconds"], a["follows"]), ("image", "kit/media/a.png", 1200, None, ["x.kind.release"]))
        self.assertEqual((b["kind"], b["src"], b["seconds"], b["follows"]), ("video", "kit/media/b.mp4", 6.5, ["draft:bluesky"]))

    def test_odd_entries_are_kept_or_skipped_plainly(self):
        (self.media / "ok.png").write_bytes(b"png")
        self.write(json.dumps({"assets": ["junk", {"kind": "image"}, {"id": "gone", "kind": "image", "path": "gone.png"},
                                          {"id": "weird", "kind": "gif", "path": "ok.png"}, {"id": "nopath", "kind": "image"},
                                          {"id": "ok", "kind": "image", "path": "ok.png", "width": "wide", "seconds": True}]}))
        m = app.read_manifest(self.repo_dir)
        self.assertIn("2 entries", m["error"])
        by = {a["id"]: a for a in m["assets"]}
        self.assertEqual(set(by), {"gone", "weird", "nopath", "ok"})
        self.assertIn("missing", by["gone"]["problem"])
        self.assertEqual(by["weird"]["kind"], "other")
        self.assertTrue(by["nopath"]["problem"])
        self.assertEqual((by["ok"]["width"], by["ok"]["seconds"], by["ok"]["src"]), (None, None, "kit/media/ok.png"))

    def test_the_api_serves_the_manifest(self):
        make_draft(Path(self.tmp.name))
        (self.media / "a.png").write_bytes(b"png")
        self.write(json.dumps({"version": 1, "assets": [{"id": "a", "channel": "hn", "kind": "image", "path": "a.png"}]}))
        live = Live(Path(self.tmp.name))
        self.addCleanup(live.stop)
        data = live.get(f"/api/media?repo={REPO}")[1]
        self.assertEqual(data["assets"][0]["src"], "kit/media/a.png")
        self.assertEqual(data["channels"]["hn"], "Show HN")


class Ranges(AppCase):
    def setUp(self):
        super().setUp()
        media = self.home / "example" / "lantern" / "kit" / "media"
        media.mkdir(parents=True)
        self.blob = bytes(range(256)) * 4
        (media / "clip.mp4").write_bytes(self.blob)
        self.path = f"/media/example/lantern/kit/media/clip.mp4?t={self.live.distro.token}"

    def get(self, rng=None):
        status, headers, raw = self.live.req("GET", self.path, token=False, headers={"Range": rng} if rng else None)
        return status, headers, raw

    def test_whole_file(self):
        status, h, raw = self.get()
        self.assertEqual((status, raw, h["Content-Type"], h["Accept-Ranges"]), (200, self.blob, "video/mp4", "bytes"))
        self.assertEqual(h["Content-Length"], "1024")

    def test_a_slice_for_seeking(self):
        status, h, raw = self.get("bytes=100-199")
        self.assertEqual((status, raw, h["Content-Range"]), (206, self.blob[100:200], "bytes 100-199/1024"))
        self.assertEqual(h["Content-Length"], "100")

    def test_open_ended_and_suffix(self):
        status, h, raw = self.get("bytes=1000-")
        self.assertEqual((status, raw, h["Content-Range"]), (206, self.blob[1000:], "bytes 1000-1023/1024"))
        status, h, raw = self.get("bytes=-24")
        self.assertEqual((status, raw, h["Content-Range"]), (206, self.blob[-24:], "bytes 1000-1023/1024"))
        status, h, raw = self.get("bytes=1000-99999")
        self.assertEqual((status, raw), (206, self.blob[1000:]))

    def test_out_of_range_and_nonsense(self):
        status, h, raw = self.get("bytes=2000-")
        self.assertEqual((status, raw, h["Content-Range"]), (416, b"", "bytes */1024"))
        self.assertEqual(self.get("bytes=500-100")[0], 200)
        self.assertEqual(self.get("pages=1-2")[0], 200)
        self.assertEqual(self.get("bytes=1-2,5-6")[0], 200)

    def test_parse_range(self):
        self.assertIsNone(app.parse_range(None, 10))
        self.assertEqual(app.parse_range("bytes=0-0", 10), (0, 0))
        self.assertEqual(app.parse_range("bytes=-3", 10), (7, 9))
        self.assertEqual(app.parse_range("bytes=-30", 10), (0, 9))
        self.assertEqual(app.parse_range("bytes=10-", 10), "bad")
        self.assertEqual(app.parse_range("bytes=-0", 10), "bad")

    def test_media_is_sandboxed(self):
        status, h, raw = self.get()
        self.assertIn("sandbox", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")


def fake(code: str):
    return lambda step, repo, extra: [sys.executable, "-u", "-c", code, step, repo] + extra


class JobsRun(unittest.TestCase):
    def start(self, code):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        make_draft(Path(self.tmp.name))
        self.live = Live(Path(self.tmp.name), argv_for=fake(code))
        self.addCleanup(self.live.stop)

    def stream(self, job_id):
        status, _, raw = self.live.req("GET", f"/api/job/stream?id={job_id}&from=0")
        self.assertEqual(status, 200)
        return [json.loads(line) for line in raw.decode().splitlines() if line]

    def wait(self, job_id, state="running", seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            snap = self.live.get(f"/api/job?id={job_id}")[1]
            if snap["state"] != state:
                return snap
            time.sleep(0.05)
        self.fail("the job did not leave " + state)

    def test_output_streams_and_the_job_finishes(self):
        self.start("import sys; print('hello'); print('args', sys.argv[1:]); print('oops', file=sys.stderr)")
        status, job = self.live.post("/api/run", {"repo": REPO, "step": "check", "draft": "hn"})
        self.assertEqual(status, 200)
        events = self.stream(job["id"])
        lines = [e["text"] for e in events if e["type"] == "line"]
        self.assertEqual(lines[0], "hello")
        self.assertIn("args ['check', 'example/lantern', 'hn']", lines)
        self.assertIn("oops", lines)
        self.assertEqual(events[-1], {"type": "end", "state": "done", "code": 0})
        snap = self.live.get(f"/api/job?id={job['id']}")[1]
        self.assertEqual((snap["state"], snap["code"]), ("done", 0))
        self.assertEqual(snap["output"][0], "hello")

    def test_a_failing_command_shows_plainly_as_failed(self):
        self.start("import sys; print('no such thing'); sys.exit(3)")
        job = self.live.post("/api/run", {"repo": REPO, "step": "create"})[1]
        snap = self.wait(job["id"])
        self.assertEqual((snap["state"], snap["code"], snap["output"]), ("failed", 3, ["no such thing"]))

    def test_one_job_per_repo_at_a_time_and_cancel(self):
        self.start("import time; print('started', flush=True); time.sleep(60)")
        job = self.live.post("/api/run", {"repo": REPO, "step": "study"})[1]
        end = time.time() + 10
        while time.time() < end and "started" not in self.live.get(f"/api/job?id={job['id']}")[1]["output"]:
            time.sleep(0.05)
        status, data = self.live.post("/api/run", {"repo": REPO, "step": "check"})
        self.assertEqual(status, 409)
        self.assertIn("still running", data["error"])
        self.assertEqual(self.live.post("/api/run", {"repo": "other/repo", "step": "study"})[0], 200)  # another repo is free
        t0 = time.time()
        status, data = self.live.post("/api/cancel", {"id": job["id"]})
        self.assertEqual(status, 200)
        snap = self.wait(job["id"])
        self.assertEqual(snap["state"], "cancelled")
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(self.live.post("/api/run", {"repo": REPO, "step": "check"})[0], 200)  # free again

    def test_cancel_only_reaches_jobs_the_app_started(self):
        self.start("print('x')")
        status, data = self.live.post("/api/cancel", {"id": "12345"})
        self.assertEqual(status, 404)
        self.assertEqual(self.live.post("/api/cancel", {"id": os.getpid()})[0], 404)

    def test_the_stream_can_resume_from_a_line(self):
        self.start("print('one'); print('two'); print('three')")
        job = self.live.post("/api/run", {"repo": REPO, "step": "check"})[1]
        self.wait(job["id"])
        status, _, raw = self.live.req("GET", f"/api/job/stream?id={job['id']}&from=2")
        events = [json.loads(l) for l in raw.decode().splitlines() if l]
        self.assertEqual([e["text"] for e in events if e["type"] == "line"], ["three"])

    def test_a_missing_program_is_reported(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.live = Live(Path(self.tmp.name), argv_for=lambda s, r, e: ["/no/such/program"])
        self.addCleanup(self.live.stop)
        job = self.live.post("/api/run", {"repo": REPO, "step": "check"})[1]
        snap = self.wait(job["id"])
        self.assertEqual(snap["state"], "failed")
        self.assertIn("Couldn't start", snap["output"][0])


class Views(AppCase):
    def test_launches_list_each_repo_with_one_next_step(self):
        data = self.live.get("/api/launches")[1]
        self.assertEqual([l["repo"] for l in data["launches"]], [REPO])
        l = data["launches"][0]
        self.assertEqual(l["next"]["key"], "study")
        self.assertEqual(l["next"]["how"], "run")
        self.assertEqual(len(l["steps"]), len(stages.FLOW))
        self.assertEqual(l["counts"]["drafts"], 1)

    def test_drafts_come_with_live_checks_and_markers(self):
        d = self.live.get(f"/api/drafts?repo={REPO}")[1]["drafts"][0]
        self.assertEqual((d["id"], d["state"], d["channel_name"]), ("hn", "draft", "Show HN"))
        self.assertEqual(d["submit"], "https://news.ycombinator.com/submit")
        self.assertTrue(any("fill in" in i["message"] for i in d["issues"]))
        self.assertEqual(d["fields"][0]["max"], 80)

    def test_empty_states_do_not_fail(self):
        for route in ("study", "playbook", "targets", "media", "results"):
            status, _ = self.live.get(f"/api/{route}?repo={REPO}")
            self.assertEqual(status, 200, route)

    def test_a_home_with_nothing_in_it(self):
        empty = Path(self.tmp.name) / "empty"
        live = Live(empty)
        self.addCleanup(live.stop)
        self.assertEqual(live.get("/api/launches")[1]["launches"], [])
        self.assertFalse(empty.exists())  # looking at it makes nothing


class Cli(unittest.TestCase):
    def test_the_app_command_parses(self):
        p = cli.build_parser()
        a = p.parse_args(["app", "--home", "/x", "--port", "5", "--no-open"])
        self.assertEqual((a.home, a.port, a.no_open), ("/x", 5, True))
        self.assertEqual(p.parse_args(["--home", "/y", "app"]).home, "/y")
        self.assertEqual(p.parse_args(["app"]).port, 0)
        self.assertIn("app", cli.HELP)

    def test_it_is_in_the_installed_files(self):
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml")
        if pyproject.exists():
            self.assertIn("app.html", pyproject.read_text())
        self.assertTrue(app.PAGE.is_file())


if __name__ == "__main__":
    unittest.main()
