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


class DedupeTests(unittest.TestCase):
    def test_normalize_url(self):
        same = ["https://www.Example.com/a/?utm_source=x&id=2#top", "http://example.com/a?id=2",
                "https://example.com:443/a//?id=2&fbclid=abc"]
        self.assertEqual(len({dsr.normalize_url(u) for u in same}), 1)
        self.assertNotEqual(dsr.normalize_url("https://example.com/a?id=2"),
                            dsr.normalize_url("https://example.com/a?id=3"))

    def test_add_hits_merges_duplicates(self):
        sources = []
        q1 = {"query": "q1", "sub_question": "s1"}
        q2 = {"query": "q2", "sub_question": "s2"}
        hits = [
            (dsr.normalize_result(result("https://a.com/x", title="A", authority=2, rerank=1.0)), q1),
            (dsr.normalize_result(result("https://b.com/", title="B")), q1),
            (dsr.normalize_result(result("https://www.a.com/x/?utm_medium=m", title="A longer title",
                                         authority=4, rerank=2.5)), q2),
        ]
        new = dsr.add_hits(sources, hits)
        self.assertEqual(new, 2)
        self.assertEqual([s["id"] for s in sources], [1, 2])
        a = sources[0]
        self.assertEqual(a["authority"], 4)
        self.assertEqual(a["rerank_score"], 2.5)
        self.assertEqual(a["title"], "A longer title")
        self.assertEqual(a["sub_questions"], ["s1", "s2"])
        self.assertEqual(a["queries"], ["q1", "q2"])
        # ids stay stable when more hits arrive
        dsr.add_hits(sources, [(dsr.normalize_result(result("https://c.com")), q1)])
        self.assertEqual([s["id"] for s in sources], [1, 2, 3])

    def test_pick_fetches_covers_each_sub_question(self):
        sources = []
        hits = [(dsr.normalize_result(result("https://s1-%d.com" % i, rerank=2.9 - i * 0.1)),
                 {"query": "a", "sub_question": "s1"}) for i in range(5)]
        hits.append((dsr.normalize_result(result("https://s2.com", authority=0, rerank=0.2)),
                     {"query": "b", "sub_question": "s2"}))
        dsr.add_hits(sources, hits)
        picked = dsr.pick_fetches(sources, limit=3)
        urls = [s["url"] for s in picked]
        self.assertEqual(len(urls), 3)
        self.assertIn("https://s2.com", urls)  # weak but the only source for s2
        self.assertEqual(urls[0], "https://s1-0.com")

    def test_extract_images(self):
        md = ("![Chart of sales](https://cdn.x.com/chart.png) ![](https://x.com/logo.png) "
              "![pixel](https://t.x.com/pixel.gif) ![Photo](<https://cdn.x.com/p.jpg> \"cap\")")
        self.assertEqual(dsr.extract_images(md), [
            {"url": "https://cdn.x.com/chart.png", "alt": "Chart of sales"},
            {"url": "https://cdn.x.com/p.jpg", "alt": "Photo"},
        ])


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "research" / "ev-20250615-1200"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_urlopen(self, req, timeout):
        body = json.loads(req.data)
        self.calls.append((req.full_url, body))
        if req.full_url == dsr.SEARCH_URL:
            if body["query"] == "broken":
                raise http_error(400)
            return FakeResponse({"results": [
                result("https://iea.org/report", title="IEA", authority=4, rerank=2.8),
                result("https://news.com/%s" % body["query"].replace(" ", "-"), title=body["query"],
                       authority=2, rerank=1.2),
            ]})
        if body["url"].startswith("https://news.com/q2"):
            return FakeResponse({"title": "", "content": ""})
        return FakeResponse({"title": "Page " + body["url"],
                             "content": "Some text.\n![Sales chart](https://cdn.iea.org/sales.png)"})

    def run_plan(self, plan, **kw):
        client, _ = make_client()
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=self.fake_urlopen):
            return dsr.run_batch(plan, self.run_dir, client, log=lambda m: None, **kw)

    def test_batch_end_to_end(self):
        plan_path = Path(self.tmp.name) / "plan.json"
        plan_path.write_text(json.dumps({
            "question": "Is the EV market growing?",
            "sub_questions": [{"id": "s1", "text": "Sales"}, {"id": "s2", "text": "Prices"}],
            "queries": [{"query": "q1", "count": 30, "sub_question": "s1"},
                        {"query": "q2", "count": 99, "sub_question": "s2"},
                        "broken"],
        }), encoding="utf-8")
        summary = self.run_plan(dsr.load_plan(plan_path))
        data = json.loads((self.run_dir / "sources.json").read_text(encoding="utf-8"))

        search_bodies = [b for u, b in self.calls if u == dsr.SEARCH_URL]
        self.assertEqual({b["query"] for b in search_bodies}, {"q1", "q2", "broken"})
        self.assertTrue(all(b["ori_question"] == "Is the EV market growing?" for b in search_bodies))
        self.assertEqual(max(b["count"] for b in search_bodies), 50)

        urls = [s["url"] for s in data["sources"]]
        self.assertEqual(len(urls), 3)  # iea.org deduped across both queries
        self.assertEqual(len(set(urls)), 3)
        iea = data["sources"][0]
        self.assertEqual(iea["sub_questions"], ["s1", "s2"])
        self.assertTrue(iea["fetched"])
        raw = (self.run_dir / iea["raw_file"]).read_text(encoding="utf-8")
        self.assertIn("Some text.", raw)
        self.assertIn("source 1", raw)
        self.assertEqual(iea["images"][0]["url"], "https://cdn.iea.org/sales.png")

        empty = [s for s in data["sources"] if s["url"].startswith("https://news.com/q2")][0]
        self.assertFalse(empty["fetched"])
        self.assertEqual(empty["fetch_error"], "empty page")

        self.assertEqual(summary["searches_ok"], 2)
        self.assertEqual(summary["searches_failed"], 1)
        self.assertEqual(summary["fetched"], 2)
        self.assertEqual([s["error"] is None for s in data["searches"]], [True, True, False])
        self.assertEqual(data["billing"]["search_requests"], 2)
        self.assertEqual(data["billing"]["fetch_requests"], 2)
        self.assertAlmostEqual(data["billing"]["estimated_usd"], 0.003)
        self.assertNotIn("_content", json.dumps(data))
        self.assertNotIn(FAKE_KEY, json.dumps(data))

    def test_fetch_cap(self):
        plan = {"queries": [{"query": "q%d" % i, "count": 10, "sub_question": None, "need_content": False}
                            for i in range(6)], "fetch_urls": []}
        self.run_plan(plan, max_fetches=3)
        fetches = [b for u, b in self.calls if u == dsr.FETCH_URL]
        self.assertEqual(len(fetches), 3)

    def test_follow_up_round_appends(self):
        plan = {"question": "Q", "queries": [{"query": "q1", "count": 10, "sub_question": "s1",
                                               "need_content": False}], "fetch_urls": []}
        self.run_plan(plan)
        before = json.loads((self.run_dir / "sources.json").read_text(encoding="utf-8"))
        self.calls.clear()
        follow = {"queries": [{"query": "q1", "count": 10, "sub_question": "s1", "need_content": False},
                              {"query": "q3", "count": 10, "sub_question": "s3", "need_content": False}],
                  "fetch_urls": []}
        summary = self.run_plan(follow)
        after = json.loads((self.run_dir / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual([b["query"] for u, b in self.calls if u == dsr.SEARCH_URL], ["q3"])
        self.assertEqual(summary["round"], 2)
        self.assertEqual(after["question"], "Q")
        stable = lambda ss: [(s["id"], s["url"], s["raw_file"]) for s in ss[:2]]  # noqa: E731
        self.assertEqual(stable(after["sources"]), stable(before["sources"]))
        self.assertEqual(after["sources"][0]["sub_questions"], ["s1", "s3"])  # merged, not duplicated
        self.assertEqual(len(after["sources"]), 3)
        fetched_urls = [b["url"] for u, b in self.calls if u == dsr.FETCH_URL]
        self.assertNotIn("https://iea.org/report", fetched_urls)  # not fetched twice

    def test_search_content_skips_fetch(self):
        def urlopen(req, timeout):
            body = json.loads(req.data)
            self.calls.append((req.full_url, body))
            return FakeResponse({"results": [result("https://a.com", content="Full text here")]})

        client, _ = make_client()
        plan = {"queries": [{"query": "q", "count": 5, "sub_question": None, "need_content": True}],
                "fetch_urls": []}
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=urlopen):
            dsr.run_batch(plan, self.run_dir, client, log=lambda m: None)
        self.assertEqual([u for u, b in self.calls], [dsr.SEARCH_URL])
        self.assertIn("Full text here", (self.run_dir / "raw" / "001.md").read_text(encoding="utf-8"))

    def test_auth_error_aborts(self):
        client, _ = make_client()
        plan = {"queries": [{"query": "q", "count": 5, "sub_question": None, "need_content": False}],
                "fetch_urls": []}
        with mock.patch.object(dsr.urllib.request, "urlopen", side_effect=http_error(401)):
            with self.assertRaises(dsr.AuthError):
                dsr.run_batch(plan, self.run_dir, client, log=lambda m: None)

    def test_plan_validation(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"queries": [{"query": "x" * 200}]}), encoding="utf-8")
        with self.assertRaises(dsr.DsrError):
            dsr.load_plan(bad)
        bad.write_text(json.dumps({"queries": []}), encoding="utf-8")
        with self.assertRaises(dsr.DsrError):
            dsr.load_plan(bad)


if __name__ == "__main__":
    unittest.main()
