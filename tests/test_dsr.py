"""Tests for scripts/dsr.py. No network: urllib is mocked everywhere.

Run: python3 -m unittest discover tests
"""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dsr  # noqa: E402

FAKE_KEY = "sk-test1234567890abcdWXYZ"


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=None, headers=None):
    raw = json.dumps(body or {"error": {"message": "err %d" % code}}).encode("utf-8")
    return urllib.error.HTTPError("https://api.deepseek.com", code, "status %d" % code,
                                  headers or {}, io.BytesIO(raw))


def result(url, title="T", authority=3, rerank=2.0, **extra):
    r = {"title": title, "url": url, "snippet": "snippet for " + title, "summary": "summary " + title,
         "content": "", "site_name": "Site", "site_icon": None, "date": "2025-05-01T00:00:00Z",
         "authority": authority, "rerank_score": rerank}
    r.update(extra)
    return r


def make_client(**kw):
    """Client on a fake clock that advances when the code sleeps; returns (client, sleeps)."""
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    client = dsr.Client(FAKE_KEY, sleep=sleep, clock=lambda: now[0], rng=lambda: 0.5, **kw)
    return client, sleeps


class KeyLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "deepseek-research" / "config.json"
        self.patches = [mock.patch.object(dsr, "CONFIG_PATH", self.cfg),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for p in self.patches:
            p.start()
        os.environ.pop(dsr.ENV_KEY, None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_no_key(self):
        self.assertEqual(dsr.load_key(), (None, None))
        with self.assertRaises(dsr.NoKeyError):
            dsr.require_key()

    def test_env_wins_over_config(self):
        dsr.save_key("sk-fromconfig0000")
        os.environ[dsr.ENV_KEY] = "  sk-fromenv11111  "
        self.assertEqual(dsr.load_key(), ("sk-fromenv11111", "env"))

    def test_config_file(self):
        dsr.save_key("sk-fromconfig0000")
        self.assertEqual(dsr.load_key(), ("sk-fromconfig0000", "config"))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_config_file_is_private(self):
        path = dsr.save_key("sk-fromconfig0000")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_broken_config(self):
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text("{not json", encoding="utf-8")
        with self.assertRaises(dsr.DsrError):
            dsr.load_key()

    def test_redact(self):
        text = "failed with key %s and Bearer %s" % (FAKE_KEY, FAKE_KEY)
        out = dsr.redact(text, FAKE_KEY)
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("WXYZ", out)
        # unknown keys are redacted by pattern too
        self.assertNotIn("sk-otherkey123456", dsr.redact("x sk-otherkey123456 y"))

    def test_mask_key_shows_last_four_only(self):
        self.assertEqual(dsr.mask_key(FAKE_KEY), "sk-...WXYZ")
        self.assertEqual(dsr.mask_key("short"), "****")

    def test_setup_never_echoes_key(self):
        out = io.StringIO()
        with mock.patch.object(dsr.getpass, "getpass", return_value=FAKE_KEY), \
                mock.patch.object(dsr.webbrowser, "open", return_value=True) as wb, \
                mock.patch.object(dsr.Client, "search", return_value=[]), \
                redirect_stdout(out):
            code = dsr.main(["setup"])
        self.assertEqual(code, 0)
        wb.assert_called_once_with(dsr.KEYS_URL)
        self.assertNotIn(FAKE_KEY, out.getvalue())
        self.assertIn("WXYZ", out.getvalue())
        self.assertIn(dsr.KEYS_URL, out.getvalue())
        self.assertEqual(dsr.load_key()[0], FAKE_KEY)

    def test_setup_bad_key_saves_nothing(self):
        out = io.StringIO()
        with mock.patch.object(dsr.getpass, "getpass", return_value=FAKE_KEY), \
                mock.patch.object(dsr.webbrowser, "open", return_value=True), \
                mock.patch.object(dsr.Client, "search", side_effect=dsr.AuthError("bad")), \
                redirect_stdout(out):
            code = dsr.main(["setup"])
        self.assertEqual(code, dsr.AuthError.exit_code)
        self.assertIn("wrong", out.getvalue())
        self.assertFalse(self.cfg.exists())

    def test_setup_low_balance(self):
        out = io.StringIO()
        with mock.patch.object(dsr.getpass, "getpass", return_value=FAKE_KEY), \
                mock.patch.object(dsr.webbrowser, "open", return_value=True), \
                mock.patch.object(dsr.Client, "search", side_effect=dsr.BalanceError("low")), \
                redirect_stdout(out):
            code = dsr.main(["setup"])
        self.assertEqual(code, dsr.BalanceError.exit_code)
        self.assertIn("top up", out.getvalue().lower())
        self.assertFalse(self.cfg.exists())

    def test_check_without_key(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = dsr.main(["check"])
        self.assertEqual(code, dsr.NoKeyError.exit_code)
        self.assertIn("setup", err.getvalue())

    def test_errors_are_redacted(self):
        os.environ[dsr.ENV_KEY] = FAKE_KEY
        err = io.StringIO()
        boom = http_error(400, {"error": {"message": "invalid key " + FAKE_KEY}})
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=boom), redirect_stderr(err):
            code = dsr.main(["search", "hello"])
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, err.getvalue())


class ThrottleTests(unittest.TestCase):
    def test_spaces_calls(self):
        now = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            now[0] += s

        t = dsr.Throttle(4.0, clock=lambda: now[0], sleep=sleep)
        for _ in range(5):
            t.wait()
        self.assertEqual(len(sleeps), 4)
        for s in sleeps:
            self.assertAlmostEqual(s, 0.25)
        self.assertAlmostEqual(now[0], 1.0)  # 5 calls in 1s window -> <= 4 QPS + first

    def test_no_wait_when_idle(self):
        now = [0.0]
        sleeps = []
        t = dsr.Throttle(4.0, clock=lambda: now[0], sleep=sleeps.append)
        t.wait()
        now[0] = 10.0
        t.wait()
        self.assertEqual(sleeps, [])

    def test_endpoints_throttled_separately(self):
        client, sleeps = make_client()
        with mock.patch.object(dsr.urllib.request, "urlopen",
                               return_value=FakeResponse({"results": [result("https://a.com")]})):
            client.search("one")
        with mock.patch.object(dsr.urllib.request, "urlopen",
                               return_value=FakeResponse({"title": "x", "content": "y"})):
            client.fetch("https://a.com/page")
        self.assertEqual(sleeps, [])  # first call on each endpoint never waits
        self.assertLessEqual(dsr.MAX_QPS, 4)

    def test_keyed_throttle(self):
        now = [0.0]
        sleeps = []
        t = dsr.KeyedThrottle(1.0, clock=lambda: now[0], sleep=sleeps.append)
        t.wait("a.com")
        t.wait("b.com")
        self.assertEqual(sleeps, [])
        t.wait("a.com")
        self.assertEqual(sleeps, [1.0])


class ClientTests(unittest.TestCase):
    def test_search_payload_and_headers(self):
        client, _ = make_client()
        captured = {}

        def fake_urlopen(req, timeout):
            captured["req"] = req
            captured["timeout"] = timeout
            return FakeResponse({"results": [result("https://a.com", authority="4", rerank="2.5")]})

        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = client.search("ev sales after:2025", count=99, ori_question="q" * 200, need_content=True)
        req = captured["req"]
        self.assertEqual(req.full_url, dsr.SEARCH_URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(captured["timeout"], 30)
        body = json.loads(req.data)
        self.assertEqual(body["count"], 50)
        self.assertEqual(len(body["ori_question"]), 128)
        self.assertTrue(body["need_content"])
        self.assertEqual(out[0]["authority"], 4)
        self.assertEqual(out[0]["rerank_score"], 2.5)
        self.assertEqual(client.billed["search"], 1)

    def test_query_length_checked(self):
        client, _ = make_client()
        with self.assertRaises(dsr.DsrError):
            client.search("x" * 129)
        with self.assertRaises(dsr.DsrError):
            client.search("   ")

    def test_empty_results_not_billed(self):
        client, _ = make_client()
        with mock.patch.object(dsr.urllib.request, "urlopen", return_value=FakeResponse({"results": []})):
            self.assertEqual(client.search("nothing"), [])
        self.assertEqual(client.billed["search"], 0)

    def test_retry_on_429_then_success(self):
        client, sleeps = make_client()
        responses = [http_error(429), http_error(429), FakeResponse({"results": [result("https://a.com")]})]
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=responses) as m:
            out = client.search("retry me")
        self.assertEqual(m.call_count, 3)
        self.assertEqual(len(out), 1)
        self.assertEqual(sleeps, [1.0, 2.0])  # rng 0.5 -> exactly the base: 1s, 2s

    def test_retry_honors_retry_after(self):
        client, sleeps = make_client()
        responses = [http_error(429, headers={"Retry-After": "7"}), FakeResponse({"results": []})]
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=responses):
            client.search("q")
        self.assertEqual(sleeps, [7.0])

    def test_gives_up_after_max_retries(self):
        client, sleeps = make_client()
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=http_error(503)) as m:
            with self.assertRaises(dsr.ApiError) as ctx:
                client.search("q")
        self.assertEqual(m.call_count, 5)  # 1 try + 4 retries
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(ctx.exception.status, 503)

    def test_retry_on_network_error(self):
        client, sleeps = make_client()
        responses = [urllib.error.URLError("timed out"), FakeResponse({"results": []})]
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=responses):
            client.search("q")
        self.assertEqual(len(sleeps), 1)

    def test_jitter_range(self):
        lo = dsr.Client(FAKE_KEY, rng=lambda: 0.0).backoff(2)
        hi = dsr.Client(FAKE_KEY, rng=lambda: 0.999).backoff(2)
        self.assertAlmostEqual(lo, 2.0)
        self.assertAlmostEqual(hi, 5.996)

    def test_401_and_402(self):
        client, sleeps = make_client()
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=http_error(401)):
            with self.assertRaises(dsr.AuthError):
                client.search("q")
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=http_error(402)):
            with self.assertRaises(dsr.BalanceError):
                client.search("q")
        insufficient = http_error(400, {"error": {"message": "Insufficient Balance"}})
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=insufficient):
            with self.assertRaises(dsr.BalanceError):
                client.search("q")
        # only throttle spacing between the three calls, never a retry backoff
        self.assertEqual(sleeps, [0.25, 0.25])

    def test_fetch_defensive_shapes(self):
        client, _ = make_client()
        shapes = [
            {"title": "A", "content": "# Hello"},
            {"data": {"title": "A", "markdown": "# Hello"}},
            {"result": {"title": "A", "text": "# Hello", "og_image": "https://img/x.jpg"}},
        ]
        for shape in shapes:
            with mock.patch.object(dsr.urllib.request, "urlopen", return_value=FakeResponse(shape)):
                page = client.fetch("https://example.com/a")
            self.assertEqual(page["title"], "A")
            self.assertEqual(page["content"], "# Hello")
        self.assertEqual(page["image"], "https://img/x.jpg")

    def test_fetch_rejects_non_http(self):
        client, _ = make_client()
        with self.assertRaises(dsr.DsrError):
            client.fetch("file:///etc/passwd")


class OutputTests(unittest.TestCase):
    def test_compose_query(self):
        q = dsr.compose_query("ev sales", lang="zh, en", after="2025", sites=["iea.org"], not_sites=["x.com"])
        self.assertEqual(q, "ev sales site:iea.org -site:x.com lang:zh,en after:2025")

    def test_format_results(self):
        rs = [dsr.normalize_result(result("https://a.com", title="Good", authority=4, rerank=2.0)),
              dsr.normalize_result(result("https://b.com", title="Meh", authority=-1, rerank=0.4))]
        text = dsr.format_results(rs, "q")
        self.assertIn("[1]* Good", text)
        self.assertIn("[2] Meh", text)
        self.assertIn("authority ?", text)
        self.assertIn("2025-05-01", text)
        self.assertIn("rerank 2.00", text)

    def test_search_json_cli(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {dsr.ENV_KEY: FAKE_KEY}), \
                mock.patch.object(dsr.urllib.request, "urlopen",
                                  return_value=FakeResponse({"results": [result("https://a.com")]})), \
                redirect_stdout(out):
            code = dsr.main(["search", "ev", "--site", "iea.org", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["query"], "ev site:iea.org")
        self.assertEqual(data["results"][0]["url"], "https://a.com")


if __name__ == "__main__":
    unittest.main()
