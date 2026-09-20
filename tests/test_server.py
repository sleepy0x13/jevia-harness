"""Server tests that touch no network: health, planning, and error handling."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from jevharness.config import Config
from jevharness.server import Handler, HarnessPool

KEY = "sk-or-v1-abcdefghijklmnop"


def settings_payload():
    return {
        "roster": {
            "credentials": [{"ref": "k", "api_key": KEY}],
            "models": [{"id": "m", "model": "v/m", "capability": 2,
                        "price_in": 0.01, "price_out": 0.02, "credential_ref": "k"}],
        }
    }


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.config = Config(api_key="")
        Handler.pool = HarnessPool()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as response:
            return response.status, response.headers, response.read()

    def post(self, path, payload, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.url(path), data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class TestStatic(ServerTestCase):
    def test_index_is_served_as_utf8(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b"<title>JEVia</title>", body)

    def test_the_copy_module_keeps_its_chinese(self):
        status, headers, body = self.get("/ui/js/copy.js")
        self.assertEqual(status, 200)
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertIn("设置".encode(), body, "Chinese copy must survive the byte trip")

    def test_fonts_are_served_to_sandboxed_previews(self):
        status, headers, body = self.get("/ui/fonts/Space-400.ttf")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "font/ttf")
        # An app preview has no origin of its own; without this it gets no font.
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertGreater(len(body), 10000)

    def test_health_reports_the_ladder(self):
        status, _, body = self.get("/api/health")
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["capability_levels"]), 5)
        self.assertFalse(data["env_key_present"])

    def test_path_traversal_is_refused(self):
        for path in ("/ui/../../../etc/passwd", "/ui/../.env", "/ui/nope.txt"):
            try:
                status, _, _ = self.get(path)
            except urllib.error.HTTPError as exc:
                status = exc.code
            self.assertEqual(status, 404, path)

    def test_unknown_routes_are_404_json(self):
        status, data = self.post("/api/nope", {})
        self.assertEqual(status, 404)
        self.assertEqual(data["error"]["code"], "not_found")


class TestPlanEndpoint(ServerTestCase):
    def test_a_recognised_task_is_planned_with_no_upstream_call(self):
        status, data = self.post("/api/plan", {
            "settings": settings_payload(),
            "task": {"prompt": "Classify this ticket into billing, technical or sales."},
        })
        self.assertEqual(status, 200)
        self.assertEqual(data["plan"]["source"], "deterministic")
        self.assertEqual(data["plan"]["answer_from"], "decisions")
        self.assertEqual(len(data["plan"]["decisions"]), 1)
        self.assertTrue(data["shape_key"])

    def test_missing_key_is_reported_as_a_config_error(self):
        status, data = self.post("/api/plan", {
            "settings": {"roster": {"credentials": [], "models": []}},
            "task": {"prompt": "Classify this into a, b or c."},
        })
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "config_error")

    def test_a_bad_question_is_rejected_with_a_schema_error(self):
        status, data = self.post("/api/plan", {
            "settings": settings_payload(),
            "task": {"prompt": "p", "questions": {"d": {"type": "choice"}}},
        })
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "schema_error")

    def test_decide_requires_typed_questions(self):
        status, data = self.post("/api/decide", {
            "settings": settings_payload(), "task": {"prompt": "no questions here"},
        })
        self.assertEqual(status, 400)
        self.assertIn("questions", data["error"]["message"])

    def test_malformed_json_is_a_clean_400(self):
        status, data = self.post("/api/plan", None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "schema_error")

    def test_a_non_object_body_is_refused(self):
        status, data = self.post("/api/plan", None, raw=b"[1,2,3]")
        self.assertEqual(status, 400)


class TestPool(unittest.TestCase):
    def test_different_keys_get_different_fingerprints(self):
        from jevharness.harness import Settings

        a = Settings.from_request(settings_payload())
        payload = settings_payload()
        payload["roster"]["credentials"][0]["api_key"] = "sk-or-v1-zzzzzzzzzzzzz"
        b = Settings.from_request(payload)
        self.assertNotEqual(HarnessPool.fingerprint(a), HarnessPool.fingerprint(b))

    def test_the_fingerprint_does_not_contain_the_key(self):
        from jevharness.harness import Settings

        fingerprint = HarnessPool.fingerprint(Settings.from_request(settings_payload()))
        self.assertNotIn(KEY, fingerprint)

    def test_the_same_settings_share_a_plan_cache(self):
        from jevharness.harness import Settings

        pool = HarnessPool()
        first = pool.get(Settings.from_request(settings_payload()))
        second = pool.get(Settings.from_request(settings_payload()))
        self.assertIs(first.cache, second.cache)


if __name__ == "__main__":
    unittest.main()


class TestScheduledRunsUseTheEnvKey(unittest.TestCase):
    def test_a_job_with_no_key_of_its_own_falls_back_to_env(self):
        from jevharness.server import Schedules
        from jevharness.scheduler import Job

        seen = {}

        class Recorder(Schedules):
            def _run_job(self, job):
                from jevharness.harness import Settings

                seen["settings"] = Settings.from_request(job.settings, fallback=self.config)
                return {"output": "ok"}

        schedules = Recorder(Config(api_key="sk-or-v1-fromenvfromenv"))
        schedules._run_job(Job(id="j", name="n", prompt="p", settings={"workspace": "w"}))
        self.assertEqual(seen["settings"].roster.credentials["default"].api_key,
                         "sk-or-v1-fromenvfromenv")


class TestRecordedRuns(ServerTestCase):
    """vNext: run records are read from the workspace only, never by path, and
    reading them calls nothing."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.ws = Path(tempfile.mkdtemp())
        folder = self.ws / ".jevia" / "runs"
        folder.mkdir(parents=True)
        event = {"type": "decision_event", "event_version": 1, "event_id": "run-abcdef-e1", "seq": 1,
                 "run_id": "run-abcdef", "kind": "run.started", "actor": "policy", "stage": "run",
                 "payload": {"note": f"key {KEY} leaked"}}
        (folder / "run-abcdef.jsonl").write_text(json.dumps(event) + "\n")

    def body(self, **extra):
        payload = {"settings": {**settings_payload(), "workspace": str(self.ws)}}
        payload.update(extra)
        return payload

    def test_a_record_is_listed_and_read_back_scrubbed(self):
        status, data = self.post("/api/runs", self.body(action="list"))
        self.assertEqual(status, 200)
        self.assertEqual([r["run_id"] for r in data["runs"]], ["run-abcdef"])
        status, data = self.post("/api/runs", self.body(action="get", run_id="run-abcdef"))
        self.assertEqual(status, 200)
        self.assertEqual(data["events"][0]["kind"], "run.started")
        self.assertNotIn(KEY, json.dumps(data))

    def test_ids_that_look_like_paths_are_refused(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "run-abcdef/../../x", "..", ""):
            status, data = self.post("/api/runs", self.body(action="get", run_id=bad))
            self.assertEqual(status, 400, bad)

    def test_cancelling_an_unknown_run_changes_nothing(self):
        status, data = self.post("/api/runs", self.body(action="cancel", run_id="run-nothere"))
        self.assertEqual((status, data["status"]), (200, "not_running"))

    def test_a_cross_site_post_is_refused(self):
        request = urllib.request.Request(
            self.url("/api/runs"), data=json.dumps(self.body(action="list")).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example",
                     "Sec-Fetch-Site": "cross-site"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
