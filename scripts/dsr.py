#!/usr/bin/env python3
"""dsr.py - web research with DeepSeek Search & Fetch. Stdlib only, Python 3.9+.

Commands:
  setup                         guided API key setup (run it in your own terminal)
  check                         verify the key works
  search "query" [options]      one web search, compact numbered output
  fetch URL                     one page as Markdown
  batch plan.json --out DIR     many searches + fetches, deduped, saved to DIR
  report spec.json --out FILE   build the self-contained HTML report

Exit codes: 0 ok, 1 error, 2 no key, 3 key rejected, 4 balance too low.
"""

import argparse
import getpass
import html
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "1.0.0"

API_BASE = "https://api.deepseek.com/v1/tools"
SEARCH_URL = API_BASE + "/search"
FETCH_URL = API_BASE + "/fetch"
KEYS_URL = "https://platform.deepseek.com/api_keys"
ENV_KEY = "DEEPSEEK_API_KEY"
CONFIG_PATH = Path.home() / ".config" / "deepseek-research" / "config.json"

PRICE_PER_REQUEST = 0.75 / 1000  # USD per successful Search or Fetch request
MAX_QPS = 4.0                    # per endpoint; the API allows 5, we stay under it
DOMAIN_QPS = 1.0                 # Fetch also limits per domain
MAX_RETRIES = 4
TIMEOUT = 30
MAX_QUERY_CHARS = 128
USER_AGENT = "deepseek-research/" + __version__

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPT_DIR / "report_template.html"


# ---------------------------------------------------------------------------
# Errors


class DsrError(Exception):
    exit_code = 1


class NoKeyError(DsrError):
    exit_code = 2


class AuthError(DsrError):
    exit_code = 3


class BalanceError(DsrError):
    exit_code = 4


class ApiError(DsrError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Keys

KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
BEARER_PATTERN = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def mask_key(key: str) -> str:
    """Only ever show the last 4 characters of a key."""
    key = key or ""
    if len(key) < 8:
        return "****"
    return "sk-..." + key[-4:]


def redact(text: Any, key: Optional[str] = None) -> str:
    """Remove anything that looks like an API key from text meant for output."""
    text = str(text)
    if key:
        text = text.replace(key, mask_key(key))
    text = BEARER_PATTERN.sub(r"\1[redacted]", text)
    return KEY_PATTERN.sub(lambda m: mask_key(m.group(0)), text)


def load_key() -> Tuple[Optional[str], Optional[str]]:
    """Return (key, source). Lookup order: env DEEPSEEK_API_KEY, then config file."""
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        return env, "env"
    path = Path(CONFIG_PATH)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DsrError("The config file %s is unreadable. Run setup again." % path)
        key = str(data.get("api_key") or "").strip() if isinstance(data, dict) else ""
        if key:
            return key, "config"
    return None, None


def require_key() -> str:
    key, _ = load_key()
    if not key:
        raise NoKeyError(
            "No DeepSeek API key found.\n"
            "Run this in your own terminal (not through an agent):\n"
            "  python3 %s setup\n"
            "Or set the environment variable %s." % (Path(__file__).resolve(), ENV_KEY)
        )
    return key


def save_key(key: str, path: Optional[Path] = None) -> Path:
    """Save the key to the user config file, readable only by the user (chmod 600)."""
    path = Path(path or CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    data = json.dumps({"api_key": key, "saved_at": now_iso()}, indent=2)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data + "\n")
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass  # Windows: ACLs, not mode bits
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Throttling


class Throttle:
    """Spaces calls at least 1/qps seconds apart. Thread-safe."""

    def __init__(self, qps: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / qps
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = None  # type: Optional[float]

    def wait(self) -> float:
        with self._lock:
            now = self._clock()
            slot = now if self._next is None else max(now, self._next)
            self._next = slot + self.interval
        delay = slot - now
        if delay > 0:
            self._sleep(delay)
        return delay


class KeyedThrottle:
    """One Throttle per key (e.g. per domain)."""

    def __init__(self, qps: float, clock=time.monotonic, sleep=time.sleep):
        self._args = (qps, clock, sleep)
        self._lock = threading.Lock()
        self._throttles = {}  # type: Dict[str, Throttle]

    def wait(self, key: str) -> float:
        with self._lock:
            t = self._throttles.get(key)
            if t is None:
                t = self._throttles[key] = Throttle(*self._args)
        return t.wait()


# ---------------------------------------------------------------------------
# API client


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = -1) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_result(r: Dict[str, Any]) -> Dict[str, Any]:
    """Give every search result the same shape, whatever the API sent."""
    return {
        "title": str(r.get("title") or "").strip(),
        "url": str(r.get("url") or "").strip(),
        "snippet": str(r.get("snippet") or "").strip(),
        "summary": str(r.get("summary") or "").strip(),
        "content": str(r.get("content") or ""),
        "site_name": r.get("site_name") or None,
        "site_icon": r.get("site_icon") or None,
        "date": r.get("date") or None,
        "authority": _to_int(r.get("authority"), -1),
        "rerank_score": _to_float(r.get("rerank_score"), 0.0),
    }


def _unwrap(data: Any) -> Dict[str, Any]:
    """Fetch responses are inspected defensively: accept {..}, {data: {..}}, {result: {..}}."""
    if not isinstance(data, dict):
        return {}
    for k in ("data", "result"):
        inner = data.get(k)
        if isinstance(inner, dict) and not any(x in data for x in ("content", "markdown", "text")):
            return inner
    return data


def normalize_page(data: Any, url: str) -> Dict[str, Any]:
    d = _unwrap(data)
    content = ""
    for k in ("content", "markdown", "text", "body"):
        if isinstance(d.get(k), str) and d[k].strip():
            content = d[k]
            break
    image = None
    for k in ("og_image", "image", "cover", "thumbnail"):
        if isinstance(d.get(k), str) and d[k].startswith(("http://", "https://")):
            image = d[k]
            break
    return {
        "url": str(d.get("url") or url),
        "title": str(d.get("title") or "").strip(),
        "content": content,
        "image": image,
        "site_name": d.get("site_name") or None,
        "date": d.get("date") or d.get("published") or None,
    }


class Client:
    """DeepSeek Search & Fetch with throttling and retries.

    Throttle: <= MAX_QPS per endpoint, plus DOMAIN_QPS per fetched domain.
    Retries: 429, 5xx and network errors, exponential backoff + jitter, MAX_RETRIES.
    """

    def __init__(self, key: str, qps: float = MAX_QPS, timeout: float = TIMEOUT,
                 max_retries: int = MAX_RETRIES, sleep=time.sleep, clock=time.monotonic,
                 rng=random.random):
        self.key = key
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._rng = rng
        self.throttles = {"search": Throttle(qps, clock, sleep), "fetch": Throttle(qps, clock, sleep)}
        self.domain_throttle = KeyedThrottle(DOMAIN_QPS, clock, sleep)
        self.billed = {"search": 0, "fetch": 0}
        self._count_lock = threading.Lock()

    # -- low level

    def backoff(self, attempt: int, retry_after: Optional[str] = None) -> float:
        """Exponential backoff with jitter: ~1s, 2s, 4s, 8s (x0.5-1.5). Honors Retry-After."""
        base = min(30.0, 2.0 ** attempt)
        delay = base * (0.5 + self._rng())
        if retry_after:
            try:
                delay = max(delay, min(60.0, float(retry_after)))
            except ValueError:
                pass
        return delay

    def _post(self, kind: str, url: str, payload: Dict[str, Any], domain: Optional[str] = None) -> Any:
        body = json.dumps(payload).encode("utf-8")
        attempt = 0
        while True:
            self.throttles[kind].wait()
            if domain:
                self.domain_throttle.wait(domain)
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    raise ApiError("DeepSeek sent a reply that is not JSON.")
            except urllib.error.HTTPError as e:
                status = e.code
                detail = self._detail(e)
                if status == 429 or 500 <= status < 600:
                    if attempt < self.max_retries:
                        retry_after = e.headers.get("Retry-After") if e.headers else None
                        self._sleep(self.backoff(attempt, retry_after))
                        attempt += 1
                        continue
                    raise ApiError(redact("HTTP %d after %d retries: %s" % (status, self.max_retries, detail), self.key), status)
                if status == 401:
                    raise AuthError("DeepSeek rejected the API key (401). Check it, or create a new one at " + KEYS_URL)
                low = detail.lower()
                if status == 402 or "insufficient" in low or "balance" in low:
                    raise BalanceError("Your DeepSeek balance is too low (HTTP %d). Top up at https://platform.deepseek.com/top_up" % status)
                raise ApiError(redact("HTTP %d: %s" % (status, detail), self.key), status)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    self._sleep(self.backoff(attempt))
                    attempt += 1
                    continue
                reason = getattr(e, "reason", e)
                raise ApiError(redact("Network error after %d retries: %s" % (self.max_retries, reason), self.key))

    @staticmethod
    def _detail(e: urllib.error.HTTPError) -> str:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            return str(e.reason or "")
        try:
            data = json.loads(raw)
            err = data.get("error", data) if isinstance(data, dict) else data
            if isinstance(err, dict):
                return str(err.get("message") or err)
            return str(err)
        except ValueError:
            return raw[:300]

    def _bill(self, kind: str) -> None:
        with self._count_lock:
            self.billed[kind] += 1

    @property
    def estimated_cost(self) -> float:
        return (self.billed["search"] + self.billed["fetch"]) * PRICE_PER_REQUEST

    # -- endpoints

    def search(self, query: str, count: int = 10, ori_question: Optional[str] = None,
               localization: Optional[str] = None, need_content: bool = False) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not 1 <= len(query) <= MAX_QUERY_CHARS:
            raise DsrError("A query must be 1-%d characters (this one is %d): %r"
                           % (MAX_QUERY_CHARS, len(query), query[:60]))
        payload = {"query": query, "count": max(1, min(50, int(count)))}  # type: Dict[str, Any]
        if ori_question and ori_question.strip():
            payload["ori_question"] = ori_question.strip()[:MAX_QUERY_CHARS]
        if localization:
            payload["localization"] = localization
        if need_content:
            payload["need_content"] = True
        data = self._post("search", SEARCH_URL, payload)
        results = data.get("results") if isinstance(data, dict) else None
        if results is None:
            results = _unwrap(data).get("results")
        out = [normalize_result(r) for r in (results or []) if isinstance(r, dict)]
        if out:
            self._bill("search")
        return out

    def fetch(self, url: str) -> Dict[str, Any]:
        url = (url or "").strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise DsrError("Only http(s) URLs can be fetched: %r" % url[:100])
        data = self._post("fetch", FETCH_URL, {"url": url}, domain=parts.netloc.lower())
        page = normalize_page(data, url)
        if page["content"]:
            self._bill("fetch")
        return page


# ---------------------------------------------------------------------------
# Query helpers and output


def compose_query(query: str, lang: Optional[str] = None, after: Optional[str] = None,
                  before: Optional[str] = None, sites: Optional[List[str]] = None,
                  not_sites: Optional[List[str]] = None) -> str:
    parts = [query.strip()]
    for s in sites or []:
        parts.append("site:" + s.strip())
    for s in not_sites or []:
        parts.append("-site:" + s.strip())
    if lang:
        parts.append("lang:" + lang.replace(" ", ""))
    if after:
        parts.append("after:" + after.strip())
    if before:
        parts.append("before:" + before.strip())
    return " ".join(p for p in parts if p)


def is_high_quality(r: Dict[str, Any]) -> bool:
    return r.get("authority", -1) >= 3 and r.get("rerank_score", 0) >= 1.5


def _oneline(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_results(results: List[Dict[str, Any]], query: Optional[str] = None) -> str:
    lines = []
    if query is not None:
        lines.append("Search: %s  (%d results; * = authority>=3 and rerank>=1.5)" % (query, len(results)))
    if not results:
        lines.append("No results. Try fewer words or drop an operator.")
    for i, r in enumerate(results, 1):
        star = "*" if is_high_quality(r) else ""
        lines.append("[%d]%s %s" % (i, star, r["title"] or "(no title)"))
        lines.append("    " + r["url"])
        auth = r["authority"]
        meta = [
            (r["date"] or "no date")[:10],
            "authority %s" % ("?" if auth < 0 else auth),
            "rerank %.2f" % r["rerank_score"],
        ]
        if r["site_name"]:
            meta.append(str(r["site_name"]))
        lines.append("    " + " | ".join(meta))
        snippet = _oneline(r["snippet"] or r["summary"])
        if snippet:
            lines.append("    " + snippet)
        if r["content"]:
            lines.append("    (full content: %d chars, use --json)" % len(r["content"]))
    return "\n".join(lines)


def format_page(page: Dict[str, Any]) -> str:
    head = ["# " + (page["title"] or "(no title)"), "URL: " + page["url"]]
    if page.get("date"):
        head.append("Date: " + str(page["date"]))
    if not page["content"]:
        head.append("\n(The page came back empty. It may block crawlers or need a login.)")
    return "\n".join(head) + "\n\n" + page["content"]


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + rename)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise DsrError("Can't read %s %s: %s" % (what, path, e.strerror or e))
    except ValueError as e:
        raise DsrError("%s %s is not valid JSON: %s" % (what.capitalize(), path, e))


# ---------------------------------------------------------------------------
# Batch: many searches + fetches into a run directory

TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|spm)$", re.IGNORECASE)
IMAGE_MD = re.compile(r"!\[([^\]]*)\]\(\s*<?(https?://[^)\s>]+)>?(?:\s+[\"'][^)]*[\"'])?\s*\)")
IMAGE_SKIP = re.compile(r"(logo|icon|sprite|avatar|pixel|badge|spacer|emoji|\.svg|\.gif)(\?|$|[/_.-])", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """Key used to dedupe sources: same page => same key."""
    p = urllib.parse.urlsplit((url or "").strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = p.port if p.port and p.port not in (80, 443) else None
    netloc = host + (":%d" % port if port else "")
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    if scheme in ("http", "https"):
        scheme = "https"
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def extract_images(markdown: str, limit: int = 6) -> List[Dict[str, str]]:
    """Content images from fetched Markdown (skips logos, icons, tracking pixels)."""
    out, seen = [], set()
    for alt, url in IMAGE_MD.findall(markdown or ""):
        if url in seen or IMAGE_SKIP.search(url):
            continue
        seen.add(url)
        out.append({"url": url, "alt": alt.strip()})
        if len(out) >= limit:
            break
    return out


def load_plan(path: Path) -> Dict[str, Any]:
    plan = read_json(path, "plan")
    if not isinstance(plan, dict):
        raise DsrError("The plan must be a JSON object. See references/advanced-mode.md.")
    queries = []
    for i, q in enumerate(plan.get("queries") or []):
        if isinstance(q, str):
            q = {"query": q}
        if not isinstance(q, dict) or not str(q.get("query") or "").strip():
            raise DsrError("Plan query #%d has no \"query\" text." % (i + 1))
        query = str(q["query"]).strip()
        if len(query) > MAX_QUERY_CHARS:
            raise DsrError("Plan query #%d is longer than %d characters: %r" % (i + 1, MAX_QUERY_CHARS, query[:60]))
        queries.append({
            "query": query,
            "count": max(1, min(50, _to_int(q.get("count"), 20))),
            "sub_question": q.get("sub_question"),
            "need_content": bool(q.get("need_content", False)),
        })
    fetch_urls = [u for u in (plan.get("fetch_urls") or []) if isinstance(u, str)]
    if not queries and not fetch_urls:
        raise DsrError("The plan has no queries.")
    plan["queries"] = queries
    plan["fetch_urls"] = fetch_urls
    return plan


def source_score(s: Dict[str, Any]) -> float:
    """Rank for fetching: rerank score, plus a bonus for authority."""
    return _to_float(s.get("rerank_score")) + 0.5 * max(_to_int(s.get("authority")), 0)


def add_hits(sources: List[Dict[str, Any]], hits: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> int:
    """Merge search hits into sources, deduped by URL. Returns how many sources are new.

    Each hit is (result, query_info). Duplicates keep the best scores, the longest
    text fields, and remember every query and sub-question that found them.
    """
    index = {normalize_url(s["url"]): s for s in sources}
    next_id = max([s["id"] for s in sources] or [0]) + 1
    new = 0
    for r, q in hits:
        if not r.get("url"):
            continue
        key = normalize_url(r["url"])
        s = index.get(key)
        if s is None:
            s = {"id": next_id, "url": r["url"], "title": r["title"], "site_name": r["site_name"],
                 "site_icon": r["site_icon"], "date": r["date"], "authority": r["authority"],
                 "rerank_score": r["rerank_score"], "snippet": r["snippet"], "summary": r["summary"],
                 "sub_questions": [], "queries": [], "fetched": False, "raw_file": None,
                 "content_chars": 0, "images": [], "fetch_error": None}
            if r.get("content"):
                s["_content"] = r["content"]
            index[key] = s
            sources.append(s)
            next_id += 1
            new += 1
        else:
            s["authority"] = max(s["authority"], r["authority"])
            s["rerank_score"] = max(s["rerank_score"], r["rerank_score"])
            for f in ("title", "snippet", "summary"):
                if len(r.get(f) or "") > len(s.get(f) or ""):
                    s[f] = r[f]
            for f in ("site_name", "site_icon", "date"):
                s[f] = s.get(f) or r.get(f)
            if r.get("content") and not s.get("fetched") and not s.get("_content"):
                s["_content"] = r["content"]
        if q.get("sub_question") and q["sub_question"] not in s["sub_questions"]:
            s["sub_questions"].append(q["sub_question"])
        if q["query"] not in s["queries"]:
            s["queries"].append(q["query"])
    return new


def pick_fetches(sources: List[Dict[str, Any]], limit: int, per_sub_question: int = 2) -> List[Dict[str, Any]]:
    """Choose pages to fetch: the best couple per sub-question first, then best overall."""
    ranked = sorted((s for s in sources if not s.get("fetched") and not s.get("_content")
                     and not s.get("fetch_error") and urllib.parse.urlsplit(s["url"]).scheme in ("http", "https")),
                    key=lambda s: (-source_score(s), s["id"]))
    picked, seen = [], set()
    subs = []
    for s in ranked:
        for sq in s["sub_questions"]:
            if sq not in subs:
                subs.append(sq)
    for sq in subs:
        n = 0
        for s in ranked:
            if n >= per_sub_question or len(picked) >= limit:
                break
            if sq in s["sub_questions"] and s["id"] not in seen:
                picked.append(s)
                seen.add(s["id"])
                n += 1
    for s in ranked:
        if len(picked) >= limit:
            break
        if s["id"] not in seen:
            picked.append(s)
            seen.add(s["id"])
    return picked


def _raw_name(source_id: int) -> str:
    return "raw/%03d.md" % source_id


def _write_raw(run_dir: Path, s: Dict[str, Any], title: str, content: str) -> None:
    header = "<!-- source %d | %s | fetched %s -->\n# %s\n\n" % (s["id"], s["url"], now_iso(), title or s["title"])
    (run_dir / _raw_name(s["id"])).write_text(header + content, encoding="utf-8")
    s["fetched"] = True
    s["raw_file"] = _raw_name(s["id"])
    s["content_chars"] = len(content)
    s["images"] = extract_images(content)
    s["fetch_error"] = None


def run_batch(plan: Dict[str, Any], run_dir: Path, client: "Client", max_fetches: Optional[int] = None,
              workers: int = 4, log=print) -> Dict[str, Any]:
    """Run every plan query, dedupe by URL, fetch the best pages, write sources.json + raw/.

    Re-running into the same run_dir adds to it (used for the reviewer's follow-up round):
    existing source ids stay stable and queries already run are skipped.
    """
    import concurrent.futures as cf

    run_dir = Path(run_dir)
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "sources.json"
    state = read_json(out_path, "sources file") if out_path.exists() else {}
    sources = state.get("sources") or []
    searches = state.get("searches") or []
    billing = state.get("billing") or {"search_requests": 0, "fetch_requests": 0}
    round_no = max([s.get("round", 1) for s in searches] or [0]) + 1
    question = plan.get("question") or state.get("question") or ""
    done = {s["query"] for s in searches if not s.get("error")}
    todo = [q for q in plan["queries"] if q["query"] not in done]
    limit = max_fetches if max_fetches is not None else _to_int(plan.get("max_fetches"), 25)

    log("Round %d: %d searches (%d skipped as already run), up to %d fetches."
        % (round_no, len(todo), len(plan["queries"]) - len(todo), limit))

    # 1. searches, in parallel under the throttle; results merged in plan order
    results = [None] * len(todo)  # type: List[Any]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(client.search, q["query"], q["count"], question or None, None,
                               q["need_content"]): i for i, q in enumerate(todo)}
        try:
            for fut in cf.as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except (AuthError, BalanceError):
                    raise
                except DsrError as e:
                    results[i] = e
        except (AuthError, BalanceError):
            for f in futures:
                f.cancel()
            raise
    hits = []
    for q, res in zip(todo, results):
        entry = {"query": q["query"], "count": q["count"], "sub_question": q["sub_question"], "round": round_no}
        if isinstance(res, Exception):
            entry["results"] = 0
            entry["error"] = redact(res, client.key)
            log("  ! search failed: %s (%s)" % (q["query"], entry["error"]))
        else:
            entry["results"] = len(res)
            entry["error"] = None
            hits.extend((r, q) for r in res)
        searches.append(entry)
    new_sources = add_hits(sources, hits)

    # 2. pages whose content came with the search need no fetch
    for s in sources:
        content = s.pop("_content", None)
        if content and not s.get("fetched"):
            _write_raw(run_dir, s, s["title"], content)

    # 3. fetches: explicit URLs first, then the best-ranked sources
    explicit = []
    if plan.get("fetch_urls"):
        add_hits(sources, [(normalize_result({"url": u, "title": u}), {"query": "(fetch_urls)", "sub_question": None})
                           for u in plan["fetch_urls"]])
        keys = {normalize_url(u) for u in plan["fetch_urls"]}
        explicit = [s for s in sources if normalize_url(s["url"]) in keys and not s["fetched"]]
    to_fetch = explicit + [s for s in pick_fetches(sources, limit) if s not in explicit]

    def do_fetch(s):
        return s, client.fetch(s["url"])

    fetched = failed = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in cf.as_completed([pool.submit(do_fetch, s) for s in to_fetch]):
            try:
                s, page = fut.result()
            except (AuthError, BalanceError):
                raise
            except DsrError as e:
                failed += 1
                log("  ! fetch failed: %s" % redact(e, client.key))
                continue
            if not page["content"]:
                s["fetch_error"] = "empty page"
                failed += 1
                continue
            if not s["title"] or s["title"] == s["url"]:
                s["title"] = page["title"] or s["title"]
            _write_raw(run_dir, s, page["title"], page["content"])
            if page.get("image"):
                s["images"].insert(0, {"url": page["image"], "alt": "og:image"})
            fetched += 1
    for s in to_fetch:  # record failures by id (futures don't carry s on error)
        if not s["fetched"] and not s["fetch_error"]:
            s["fetch_error"] = "fetch failed"

    billing["search_requests"] += client.billed["search"]
    billing["fetch_requests"] += client.billed["fetch"]
    client.billed = {"search": 0, "fetch": 0}
    billing["estimated_usd"] = round((billing["search_requests"] + billing["fetch_requests"]) * PRICE_PER_REQUEST, 5)

    sub_questions = state.get("sub_questions") or []
    known = {sq.get("id") for sq in sub_questions if isinstance(sq, dict)}
    for sq in plan.get("sub_questions") or []:
        if isinstance(sq, dict) and sq.get("id") not in known:
            sub_questions.append(sq)

    write_json(out_path, {
        "version": 1,
        "question": question,
        "sub_questions": sub_questions,
        "updated_at": now_iso(),
        "billing": billing,
        "searches": searches,
        "sources": sources,
    })
    summary = {
        "round": round_no,
        "searches_ok": sum(1 for s in searches if s["round"] == round_no and not s["error"]),
        "searches_failed": sum(1 for s in searches if s["round"] == round_no and s["error"]),
        "new_sources": new_sources,
        "total_sources": len(sources),
        "fetched": fetched,
        "fetch_failed": failed,
        "total_fetched": sum(1 for s in sources if s["fetched"]),
        "estimated_usd": billing["estimated_usd"],
        "sources_file": str(out_path),
    }
    log("Done: %(searches_ok)d searches ok, %(searches_failed)d failed; %(new_sources)d new sources "
        "(%(total_sources)d total); fetched %(fetched)d pages, %(fetch_failed)d failed. "
        "Estimated cost so far: $%(estimated_usd).4f" % summary)
    log("Wrote %s and %s" % (out_path, run_dir / "raw"))
    return summary


# ---------------------------------------------------------------------------
# Report: spec.json -> one self-contained HTML file (inline CSS + SVG, no JS libs)

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
STATUSES = {
    # key: (css class, icon, label, legend text)
    "verified": ("verified", "&#10003;", "Verified", "Checked against the source text, and sources agree."),
    "single": ("single", "1", "Single source", "Only one source says this."),
    "conflicting": ("conflicting", "!", "Conflicting", "Sources disagree. Read the note."),
}
CITE_MARK = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
BOLD_MARK = re.compile(r"\*\*(.+?)\*\*")
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
MAX_SERIES = 8


def esc(v: Any) -> str:
    """HTML-escape text from the web. Braces too, so web text can never look like a template var."""
    if v is None:
        return ""
    return html.escape(str(v), quote=True).replace("{", "&#123;").replace("}", "&#125;")


def safe_url(u: Any) -> str:
    """Only http(s) URLs may become links or image sources (no javascript:, data:, etc.)."""
    if not isinstance(u, str):
        return ""
    u = u.strip()
    p = urllib.parse.urlsplit(u)
    if p.scheme.lower() in ("http", "https") and p.netloc and not re.search(r"[\s\"'<>`]", u):
        return u
    return ""


def status_key(s: Any) -> str:
    s = re.sub(r"[\s_-]+", " ", str(s or "")).strip().lower()
    if s.startswith("verif"):
        return "verified"
    if s.startswith("single"):
        return "single"
    if s.startswith("conflict"):
        return "conflicting"
    return "unsupported"


def fmt_num(v: Any, compact: bool = False) -> str:
    if isinstance(v, str):
        return v
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if compact and abs(x) >= 1000:
        for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(x) >= div:
                return ("%.1f" % (x / div)).rstrip("0").rstrip(".") + suffix
    if x.is_integer():
        return "{:,}".format(int(x))
    digits = 1 if abs(x) >= 100 else 2
    return "{:,.{d}f}".format(x, d=digits).rstrip("0").rstrip(".")


def with_unit(s: str, unit: Optional[str]) -> str:
    """'17' + '%' -> '17%'; '115' + '$/kWh' -> '$115/kWh'; '17.3' + 'million' -> '17.3 million'."""
    if not unit:
        return s
    if unit in ("%", "pp", "x", "×", "K", "M", "B", "T", "bn"):
        return s + unit
    if unit[0] in "$€£¥₹":
        return unit[0] + s + unit[1:]
    return s + " " + unit


def fmt_date(d: Any) -> str:
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?", str(d or ""))
    if not m:
        return str(d or "")
    year, month, day = int(m.group(1)), int(m.group(2)), m.group(3)
    if not 1 <= month <= 12:
        return str(d)
    return "%s %d, %d" % (MONTHS[month - 1], int(day), year) if day else "%s %d" % (MONTHS[month - 1], year)


def nice_ticks(lo: float, hi: float, target: int = 4) -> List[float]:
    if hi == lo:
        hi = lo + (abs(lo) or 1)
    span = hi - lo
    mag = 10 ** math.floor(math.log10(span / target))
    step = mag
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= target:
            break
    start = math.floor(lo / step + 1e-9) * step
    end = math.ceil(hi / step - 1e-9) * step
    ticks, v = [], start
    while v <= end + step * 1e-6:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _n(x: float) -> str:
    """Compact SVG coordinate."""
    return ("%.1f" % x).rstrip("0").rstrip(".")


def wrap_label(text: str, max_chars: int, max_lines: int = 2) -> List[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(1, max_chars - 1)] + "…"
    return [ln if len(ln) <= max_chars else ln[: max(1, max_chars - 1)] + "…" for ln in lines]


def _col_path(x: float, w: float, y0: float, y1: float) -> str:
    """Column from baseline y0 to value y1: 4px rounded data end, square at the baseline."""
    d = 1 if y1 <= y0 else -1
    r = min(4.0, w / 2, abs(y0 - y1))
    return "M%s %sV%sQ%s %s %s %sH%sQ%s %s %s %sV%sZ" % (
        _n(x), _n(y0), _n(y1 + d * r), _n(x), _n(y1), _n(x + r), _n(y1),
        _n(x + w - r), _n(x + w), _n(y1), _n(x + w), _n(y1 + d * r), _n(y0))


def _row_path(y: float, h: float, x0: float, x1: float) -> str:
    """Horizontal bar from baseline x0 to value x1, rounded at the value end."""
    d = 1 if x1 >= x0 else -1
    r = min(4.0, h / 2, abs(x1 - x0))
    return "M%s %sH%sQ%s %s %s %sV%sQ%s %s %s %sH%sZ" % (
        _n(x0), _n(y), _n(x1 - d * r), _n(x1), _n(y), _n(x1), _n(y + r),
        _n(y + h - r), _n(x1), _n(y + h), _n(x1 - d * r), _n(y + h), _n(x0))


class ReportBuilder:
    """Turns a report spec (see references/report-spec.md) into HTML. All web text is escaped."""

    def __init__(self, spec: Dict[str, Any]):
        if not isinstance(spec, dict) or not str(spec.get("title") or "").strip():
            raise DsrError("The report spec needs at least a \"title\". See references/report-spec.md.")
        self.spec = spec
        self.warnings = []  # type: List[str]
        self.sources = {}  # type: Dict[int, Dict[str, Any]]
        for i, s in enumerate(spec.get("sources") or []):
            if isinstance(s, dict):
                sid = _to_int(re.sub(r"\D", "", str(s.get("id", i + 1))) or i + 1, i + 1)
                self.sources[sid] = s
        self.unit_sep = " "

    # -- inline text

    def chip(self, n: int) -> str:
        s = self.sources.get(n)
        if s is None:
            self.warnings.append("Citation [%d] has no matching source." % n)
            return '<span class="cite missing">%d</span>' % n
        return '<a class="cite" href="#src-%d" title="%s">%d</a>' % (n, esc(s.get("title") or s.get("url")), n)

    def chips(self, ids: Any) -> str:
        if not isinstance(ids, (list, tuple)):
            ids = [ids] if ids not in (None, "") else []
        out = []
        for i in ids:
            n = _to_int(re.sub(r"\D", "", str(i)), -1)
            if n >= 0:
                out.append(self.chip(n))
        return "".join(out)

    def rich(self, text: Any) -> str:
        """Escape, then turn [n] / [n, m] into citation links and **x** into bold."""
        out = esc(text)
        out = CITE_MARK.sub(lambda m: "".join(self.chip(int(x)) for x in re.findall(r"\d+", m.group(1))), out)
        return BOLD_MARK.sub(r"<strong>\1</strong>", out)

    def item(self, it: Any) -> str:
        if isinstance(it, dict):
            text = it.get("text") or it.get("claim") or ""
            return self.rich(text) + self.chips(it.get("cites") or it.get("source_ids"))
        return self.rich(it)

    # -- charts

    def chart(self, c: Dict[str, Any]) -> str:
        kind = str(c.get("type") or "bar").lower()
        render = {"bar": self.svg_bar, "column": self.svg_bar, "hbar": self.svg_hbar,
                  "horizontal_bar": self.svg_hbar, "line": self.svg_line, "donut": self.svg_donut,
                  "pie": self.svg_donut}.get(kind)
        if render is None:
            self.warnings.append("Unknown chart type %r skipped." % kind)
            return ""
        labels, series = self.chart_data(c, kind)
        if not labels or not series:
            self.warnings.append("Chart %r has no data and was skipped." % c.get("title"))
            return ""
        unit = c.get("unit")
        legend = ""
        if kind == "line" and len(series) > 1:
            legend = '<ul class="legend">%s</ul>' % "".join(
                '<li><span class="swatch sw%d"></span>%s</li>' % (i + 1, esc(name)) for i, (name, _) in enumerate(series))
        svg = render(labels, series, unit, c)
        head = "<h3>%s</h3>" % esc(c.get("title") or "")
        if c.get("subtitle"):
            head += '<p class="sub">%s</p>' % esc(c["subtitle"])
        foot = []
        if c.get("caption") or c.get("cites"):
            foot.append("<span>%s%s</span>" % (self.rich(c.get("caption") or "Source"), self.chips(c.get("cites"))))
        foot.append(self.data_table(labels, series, unit, kind))
        return ('<figure class="chart-card">%s%s<div class="chart-wrap">%s</div><div class="chart-foot">%s</div></figure>'
                % (head, legend, svg, "".join(foot)))

    def chart_data(self, c: Dict[str, Any], kind: str) -> Tuple[List[str], List[Tuple[str, List[Optional[float]]]]]:
        def num(v):
            try:
                x = float(v)
                return x if math.isfinite(x) else None
            except (TypeError, ValueError):
                return None

        if c.get("series") and c.get("labels"):
            labels = [str(x) for x in c["labels"]]
            series = []
            for s in c["series"][:MAX_SERIES]:
                vals = [num(v) for v in (s.get("values") or [])][: len(labels)]
                vals += [None] * (len(labels) - len(vals))
                series.append((str(s.get("name") or ""), vals))
            if len(c["series"]) > MAX_SERIES:
                self.warnings.append("Chart %r: only the first %d series are drawn." % (c.get("title"), MAX_SERIES))
            return labels, series
        rows = [d for d in (c.get("data") or []) if isinstance(d, dict) and num(d.get("value")) is not None]
        labels = [str(d.get("label", "")) for d in rows]
        values = [num(d["value"]) for d in rows]
        if kind in ("donut", "pie"):
            keep = [(l, v) for l, v in zip(labels, values) if v > 0]
            if len(keep) > MAX_SERIES:  # fold the tail into "Other", never invent a 9th color
                other = sum(v for _, v in keep[MAX_SERIES - 1:])
                keep = keep[: MAX_SERIES - 1] + [("Other", other)]
            labels, values = [l for l, _ in keep], [v for _, v in keep]
        return labels, [(str(c.get("title") or ""), values)]

    def data_table(self, labels, series, unit, kind) -> str:
        head = "<tr><th></th>%s</tr>" % "".join("<th>%s</th>" % esc(n or "Value") for n, _ in series)
        rows = "".join("<tr><td>%s</td>%s</tr>" % (esc(l), "".join(
            '<td class="num">%s</td>' % ("" if vals[i] is None else esc(with_unit(fmt_num(vals[i]), unit)))
            for _, vals in series)) for i, l in enumerate(labels))
        return '<details class="data"><summary>Show data</summary><table>%s%s</table></details>' % (head, rows)

    @staticmethod
    def _svg(w: int, h: int, label: str, body: str) -> str:
        return ('<svg class="chart" viewBox="0 0 %d %d" role="img" aria-label="%s" '
                'xmlns="http://www.w3.org/2000/svg">%s</svg>' % (w, h, esc(label), body))

    def _y_axis(self, ticks, y, left, right_x) -> str:
        out = []
        for t in ticks:
            cls = "axis" if t == 0 else "grid"
            out.append('<line class="%s" x1="%d" x2="%d" y1="%s" y2="%s"/>' % (cls, left, right_x, _n(y(t)), _n(y(t))))
            out.append('<text class="tick" x="%d" y="%s" text-anchor="end">%s</text>'
                       % (left - 8, _n(y(t) + 4), esc(fmt_num(t, compact=True))))
        return "".join(out)

    def svg_bar(self, labels, series, unit, c) -> str:
        values = series[0][1]
        W, H, left, right, top, bottom = 680, 300, 48, 8, 26, 46
        pw, ph = W - left - right, H - top - bottom
        ticks = nice_ticks(min(0.0, min(values)), max(0.0, max(values)))
        lo, hi = ticks[0], ticks[-1]
        y = lambda v: top + (hi - v) / (hi - lo) * ph  # noqa: E731
        band = pw / len(values)
        bw = min(24.0, band * 0.62)
        label_all = len(values) <= 12
        extremes = {values.index(max(values)), len(values) - 1}
        out = [self._y_axis(ticks, y, left, W - right)]
        max_chars = max(3, int(band / 7))
        for i, (lab, v) in enumerate(zip(labels, values)):
            bx = left + band * i
            x = bx + (band - bw) / 2
            text = with_unit(fmt_num(v), unit)
            g = ['<g class="pt"><title>%s: %s</title>' % (esc(lab), esc(text)),
                 '<rect class="hit" x="%s" y="%d" width="%s" height="%d"/>' % (_n(bx), top, _n(band), ph),
                 '<path class="mark f1" d="%s"/>' % _col_path(x, bw, y(0), y(v))]
            if label_all or i in extremes:
                ty = y(v) - 7 if v >= 0 else y(v) + 16
                g.append('<text class="val" x="%s" y="%s" text-anchor="middle">%s</text>' % (_n(x + bw / 2), _n(ty), esc(text)))
            for j, line in enumerate(wrap_label(lab, max_chars)):
                g.append('<text class="lbl" x="%s" y="%d" text-anchor="middle">%s</text>'
                         % (_n(bx + band / 2), H - bottom + 20 + 15 * j, esc(line)))
            g.append("</g>")
            out.append("".join(g))
        return self._svg(W, H, c.get("title") or "Bar chart", "".join(out))

    def svg_hbar(self, labels, series, unit, c) -> str:
        values = series[0][1]
        W, row, bar_h, pad_right = 680, 48, 16, 96
        H = row * len(values) + 4
        lo, hi = min(0.0, min(values)), max(0.0, max(values))
        if hi == lo:
            hi = lo + 1
        x = lambda v: (v - lo) / (hi - lo) * (W - pad_right)  # noqa: E731
        out = []
        for i, (lab, v) in enumerate(zip(labels, values)):
            y0 = i * row
            text = with_unit(fmt_num(v), unit)
            vx = x(v) + 8 if v >= 0 else x(v) - 8
            anchor = "start" if v >= 0 else "end"
            out.append(''.join([
                '<g class="pt"><title>%s: %s</title>' % (esc(lab), esc(text)),
                '<rect class="hit" x="0" y="%d" width="%d" height="%d"/>' % (y0, W, row),
                '<text class="lbl" x="%s" y="%d">%s</text>' % (_n(x(0)), y0 + 15, esc(wrap_label(lab, 80, 1)[0])),
                '<path class="mark f1" d="%s"/>' % _row_path(y0 + 23, bar_h, x(0), x(v)),
                '<text class="val" x="%s" y="%d" text-anchor="%s">%s</text>' % (_n(vx), y0 + 36, anchor, esc(text)),
                "</g>"]))
        if lo < 0:
            out.append('<line class="axis" x1="%s" x2="%s" y1="0" y2="%d"/>' % (_n(x(0)), _n(x(0)), H))
        return self._svg(W, H, c.get("title") or "Bar chart", "".join(out))

    def svg_line(self, labels, series, unit, c) -> str:
        W, H, left, right, top, bottom = 680, 300, 48, 64, 16, 34
        pw, ph = W - left - right, H - top - bottom
        allv = [v for _, vals in series for v in vals if v is not None]
        if not allv:
            return ""
        ticks = nice_ticks(min(0.0, min(allv)), max(0.0, max(allv)))
        lo, hi = ticks[0], ticks[-1]
        n = len(labels)
        x = lambda i: left + (pw / 2 if n == 1 else i * pw / (n - 1))  # noqa: E731
        y = lambda v: top + (hi - v) / (hi - lo) * ph  # noqa: E731
        out = [self._y_axis(ticks, y, left, W - right)]
        every = max(1, math.ceil(n / 8))
        for i, lab in enumerate(labels):
            if i % every == 0 or i == n - 1:
                out.append('<text class="tick" x="%s" y="%d" text-anchor="middle">%s</text>'
                           % (_n(x(i)), H - 10, esc(wrap_label(lab, 12, 1)[0])))
        end_labels = []
        for k, (name, vals) in enumerate(series):
            cls = k + 1
            segs, cur = [], []
            for i, v in enumerate(vals):
                if v is None:
                    if cur:
                        segs.append(cur)
                    cur = []
                else:
                    cur.append((x(i), y(v)))
            if cur:
                segs.append(cur)
            for seg in segs:
                if len(seg) > 1:
                    out.append('<path class="line k%d" d="M%s"/>' % (cls, "L".join("%s %s" % (_n(a), _n(b)) for a, b in seg)))
            show_dots = n <= 12
            last = max((i for i, v in enumerate(vals) if v is not None), default=None)
            for i, v in enumerate(vals):
                if v is None:
                    continue
                text = with_unit(fmt_num(v), unit)
                tip = "%s%s: %s" % (name + ", " if name else "", labels[i], text)
                dot = '<circle class="mark dot f%d" cx="%s" cy="%s" r="4"/>' % (cls, _n(x(i)), _n(y(v))) \
                    if show_dots or i == last else ""
                out.append('<g class="pt"><title>%s</title><circle class="hit" cx="%s" cy="%s" r="11"/>%s</g>'
                           % (esc(tip), _n(x(i)), _n(y(v)), dot))
            if last is not None:
                end_labels.append((y(vals[last]), with_unit(fmt_num(vals[last], compact=True), unit), x(last)))
        placed = []  # type: List[float]
        for ly, text, lx in sorted(end_labels):
            if all(abs(ly - p) >= 15 for p in placed):  # colliding labels: legend + tooltip carry it
                placed.append(ly)
                out.append('<text class="val" x="%s" y="%s">%s</text>' % (_n(lx + 9), _n(ly + 4), esc(text)))
        return self._svg(W, H, c.get("title") or "Line chart", "".join(out))

    def svg_donut(self, labels, series, unit, c) -> str:
        values = series[0][1]
        total = sum(values)
        W, H, cx, cy, r, sw = 680, max(240, 32 * len(values) + 24), 120, 0, 84, 30
        cy = H / 2
        circ = 2 * math.pi * r
        gap = 2.0 if len(values) > 1 else 0.0
        out, start = [], 0.0
        for i, (lab, v) in enumerate(zip(labels, values)):
            seg = v / total * circ
            text = with_unit(fmt_num(v), unit)
            out.append('<g class="pt"><title>%s: %s (%s%%)</title><circle class="mark k%d" cx="%d" cy="%s" r="%d" '
                       'fill="none" stroke-width="%d" stroke-dasharray="%s %s" stroke-dashoffset="%s" '
                       'transform="rotate(-90 %d %s)"/></g>'
                       % (esc(lab), esc(text), fmt_num(v / total * 100), i + 1, cx, _n(cy), r, sw,
                          _n(max(seg - gap, 0.5)), _n(circ), _n(-start), cx, _n(cy)))
            start += seg
        center = c.get("center_label") or "total"
        big = c.get("center_value") or fmt_num(total, compact=True)
        out.append('<text class="big" x="%d" y="%s" text-anchor="middle">%s</text>' % (cx, _n(cy + 6), esc(big)))
        out.append('<text class="tick" x="%d" y="%s" text-anchor="middle">%s</text>' % (cx, _n(cy + 26), esc(center)))
        ly = cy - (len(values) * 32) / 2 + 8
        for i, (lab, v) in enumerate(zip(labels, values)):
            yy = ly + i * 32
            out.append('<rect class="f%d" x="260" y="%s" width="12" height="12" rx="3"/>' % (i + 1, _n(yy)))
            out.append('<text class="lbl" x="282" y="%s">%s</text>' % (_n(yy + 11), esc(wrap_label(lab, 34, 1)[0])))
            out.append('<text class="val" x="672" y="%s" text-anchor="end">%s · %s%%</text>'
                       % (_n(yy + 11), esc(with_unit(fmt_num(v), unit)), fmt_num(round(v / total * 100, 1))))
        return self._svg(W, int(H), c.get("title") or "Donut chart", "".join(out))

    # -- page sections

    def hero(self) -> str:
        s = self.spec
        claims = [c for c in (s.get("claims") or []) if isinstance(c, dict)]
        counts = s.get("counts") or {}
        verified = counts.get("claims_verified", sum(1 for c in claims if status_key(c.get("status")) == "verified"))
        stats = [(counts.get("sources_read", len(self.sources)), "sources read"),
                 (verified, "claims verified")]
        if counts.get("sources_found"):
            stats.insert(0, (counts["sources_found"], "sources found"))
        if counts.get("searches"):
            stats.insert(0, (counts["searches"], "searches"))
        mode = str(s.get("mode") or "advanced").strip().lower()
        date = s.get("date") or datetime.now().strftime("%Y-%m-%d")
        out = ["<header>"]
        if s.get("notice"):
            out.append('<p class="notice">%s</p>' % self.rich(s["notice"]))
        out.append('<div class="eyebrow"><span>Research report</span><span class="mode">%s mode</span>'
                   '<time datetime="%s">%s</time></div>' % (esc(mode.capitalize()), esc(date), esc(fmt_date(date))))
        out.append("<h1>%s</h1>" % esc(s["title"]))
        if s.get("question"):
            out.append('<p class="question">%s</p>' % esc(s["question"]))
        if s.get("tldr"):
            out.append('<p class="tldr"><span class="label">The short answer</span>%s</p>' % self.item(s["tldr"]))
        out.append('<div class="counts">%s</div>' % "".join(
            '<div class="count"><b>%s</b><span>%s</span></div>' % (esc(fmt_num(v)), esc(label)) for v, label in stats))
        out.append("</header>")
        return "".join(out)

    def takeaways(self) -> str:
        items = self.spec.get("takeaways") or []
        if not items:
            return ""
        return ('<section id="takeaways"><div class="kicker">Key takeaways</div><h2>What you need to know</h2>'
                '<ol class="takeaways">%s</ol></section>' % "".join("<li>%s</li>" % self.item(t) for t in items))

    def numbers(self) -> str:
        nums = self.spec.get("numbers") or {}
        stats = [x for x in (nums.get("stats") or []) if isinstance(x, dict)]
        charts = [self.chart(c) for c in (nums.get("charts") or []) if isinstance(c, dict)]
        charts = [c for c in charts if c]
        if not stats and not charts:
            return ""
        out = ['<section id="numbers"><div class="kicker">By the numbers</div><h2>%s</h2>'
               % esc(nums.get("heading") or "The key figures")]
        if nums.get("intro"):
            out.append('<p class="lede">%s</p>' % self.rich(nums["intro"]))
        if stats:
            cards = []
            for st in stats:
                unit = st.get("unit")
                value = fmt_num(st.get("value"), compact=not isinstance(st.get("value"), str))
                if unit and unit[0] in "$€£¥₹":
                    value, unit = unit[0] + value, unit[1:]
                cards.append('<div class="stat"><div class="label">%s</div><div class="value">%s%s</div>'
                             '<div class="note">%s%s</div></div>'
                             % (esc(st.get("label")), esc(value), "<small>%s</small>" % esc(unit) if unit else "",
                                self.rich(st.get("note") or ""), self.chips(st.get("cites"))))
            out.append('<div class="stats">%s</div>' % "".join(cards))
        out.extend(charts)
        out.append("</section>")
        return "".join(out)

    def image(self, im: Dict[str, Any]) -> str:
        src = safe_url(im.get("url"))
        if not src:
            if im.get("url"):
                self.warnings.append("Image skipped (not an http(s) URL): %r" % str(im.get("url"))[:80])
            return ""
        cap = self.rich(im.get("caption") or "")
        credit = im.get("credit")
        if credit:
            link = safe_url(im.get("credit_url"))
            credit = '<a href="%s" rel="noopener noreferrer" target="_blank">%s</a>' % (esc(link), esc(credit)) if link else esc(credit)
            cap += ' <span class="credit">Image: %s</span>' % credit
        cap += self.chips(im.get("source"))
        return ('<figure class="img"><img src="%s" alt="%s" loading="lazy" decoding="async" referrerpolicy="no-referrer" '
                'onerror="this.parentNode.style.display=\'none\'">%s</figure>'
                % (esc(src), esc(im.get("alt") or im.get("caption") or ""), "<figcaption>%s</figcaption>" % cap if cap else ""))

    def sections(self) -> str:
        out = []
        for i, sec in enumerate(self.spec.get("sections") or []):
            if not isinstance(sec, dict):
                continue
            body = ["<p>%s</p>" % self.item(p) for p in (sec.get("paragraphs") or [])]
            images = [self.image(im) for im in (sec.get("images") or []) if isinstance(im, dict)]
            # put the first image after the first paragraph, the rest at the end
            if images and body:
                body = body[:1] + images[:1] + body[1:] + images[1:]
            else:
                body += images
            kicker = '<div class="kicker">%s</div>' % esc(sec["kicker"]) if sec.get("kicker") else ""
            out.append('<section class="section" id="section-%d">%s<h2>%s</h2>%s</section>'
                       % (i + 1, kicker, esc(sec.get("heading") or ""), "".join(body)))
        return "".join(out)

    def tag(self, key: str) -> str:
        cls, icon, label, _ = STATUSES[key]
        return '<span class="tag %s"><i aria-hidden="true">%s</i>%s</span>' % (cls, icon, label)

    def confidence(self) -> str:
        claims = [c for c in (self.spec.get("claims") or []) if isinstance(c, dict)
                  and status_key(c.get("status")) != "unsupported"]
        if not claims:
            return ""
        order = {"verified": 0, "conflicting": 1, "single": 2}
        claims.sort(key=lambda c: order[status_key(c.get("status"))])
        legend = "".join("<span>%s %s</span>" % (self.tag(k), esc(STATUSES[k][3])) for k in ("verified", "single", "conflicting"))
        rows = []
        for c in claims:
            note = '<div class="note">%s</div>' % self.rich(c["note"]) if c.get("note") else ""
            rows.append("<li><div>%s</div>%s%s</li>" % (self.item(c), self.tag(status_key(c.get("status"))), note))
        return ('<section id="confidence"><div class="kicker">Confidence</div><h2>How sure are we?</h2>'
                '<div class="legend-status">%s</div><ul class="claims">%s</ul></section>' % (legend, "".join(rows)))

    def unconfirmed(self) -> str:
        items = list(self.spec.get("unconfirmed") or [])
        items += [c for c in (self.spec.get("claims") or []) if isinstance(c, dict)
                  and status_key(c.get("status")) == "unsupported"]
        if not items:
            return ""
        rows = []
        for it in items:
            note_text = (it.get("note") or it.get("reason")) if isinstance(it, dict) else None
            note = '<span class="note">%s</span>' % self.rich(note_text) if note_text else ""
            rows.append("<li>%s%s</li>" % (self.item(it), note))
        return ('<section id="unconfirmed"><div class="kicker">Open questions</div><h2>What we couldn\'t confirm</h2>'
                '<ul class="unconfirmed">%s</ul></section>' % "".join(rows))

    def source_cards(self) -> str:
        if not self.sources:
            return ""
        cards = []
        for sid in sorted(self.sources):
            s = self.sources[sid]
            url = safe_url(s.get("url"))
            host = urllib.parse.urlsplit(url).hostname or "" if url else ""
            site = s.get("site_name") or (host[4:] if host.startswith("www.") else host)
            letter = (str(site or s.get("title") or "?").strip()[:1] or "?").upper()
            icon = safe_url(s.get("site_icon"))
            icon_html = '<img src="%s" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">' % esc(icon) if icon else ""
            title = esc(s.get("title") or url or "Untitled")
            title_html = '<a href="%s" rel="noopener noreferrer" target="_blank">%s</a>' % (esc(url), title) if url else title
            meta = []
            if site:
                meta.append("<span>%s</span>" % esc(site))
            if s.get("date"):
                meta.append('<time datetime="%s">%s</time>' % (esc(str(s["date"])[:10]), esc(fmt_date(s["date"]))))
            auth = _to_int(s.get("authority"), -1)
            if auth >= 3:
                meta.append('<span class="badge high">High authority · %d/4</span>' % auth)
            elif auth >= 0:
                meta.append('<span class="badge">Authority %d/4</span>' % auth)
            else:
                meta.append('<span class="badge">Authority unknown</span>')
            cards.append('<li class="source" id="src-%d"><div class="icon" aria-hidden="true">%s%s</div><div>'
                         '<div class="title"><span class="num">[%d]</span>%s</div><div class="meta">%s</div></div></li>'
                         % (sid, esc(letter), icon_html, sid, title_html, "".join(meta)))
        return ('<section id="sources"><div class="kicker">Sources</div><h2>Where this comes from</h2>'
                '<ol class="sources">%s</ol></section>' % "".join(cards))

    def footer(self) -> str:
        bits = ["Made with deepseek-research. Web search by DeepSeek."]
        cost = self.spec.get("cost_usd")
        if isinstance(cost, (int, float)):
            bits.append("Research cost about $%s." % fmt_num(round(cost, 3)))
        bits.append("Generated %s." % fmt_date(datetime.now().strftime("%Y-%m-%d")))
        return "<footer>%s</footer>" % esc(" ".join(bits))

    def body(self) -> str:
        return "\n".join(x for x in (self.hero(), self.takeaways(), self.numbers(), self.sections(),
                                     self.confidence(), self.unconfirmed(), self.source_cards(), self.footer()) if x)


def render_template(template: str, values: Dict[str, str]) -> str:
    """Single-pass {{name}} substitution; inserted content is never re-scanned."""
    missing = [m for m in PLACEHOLDER.findall(template) if m not in values]
    if missing:
        raise DsrError("Template variables without a value: %s" % ", ".join(sorted(set(missing))))
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def build_report(spec: Dict[str, Any], template: Optional[str] = None) -> Tuple[str, List[str]]:
    """Return (html, warnings)."""
    if template is None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
    b = ReportBuilder(spec)
    content = b.body()
    tldr = spec.get("tldr")
    desc = tldr.get("text", "") if isinstance(tldr, dict) else (tldr or "")
    out = render_template(template, {
        "lang": esc(spec.get("lang") or "en"),
        "title": esc(spec["title"]),
        "description": esc(CITE_MARK.sub("", str(desc)).strip()),
        "generator": esc("deepseek-research dsr.py " + __version__),
        "content": content,
    })
    if PLACEHOLDER.search(out):
        raise DsrError("Report still contains template variables.")
    return out, sorted(set(b.warnings), key=b.warnings.index)


# ---------------------------------------------------------------------------
# Commands


def cmd_setup(args: argparse.Namespace) -> int:
    print("DeepSeek Research needs a DeepSeek API key.")
    print("A key is a private password that lets this tool use DeepSeek's web search.")
    print("Each search or page fetch costs $0.75 per 1,000 (less than a tenth of a cent).")
    print()
    print("1. Open %s" % KEYS_URL)
    print("2. Sign up or log in, and top up a small balance (a few dollars is plenty).")
    print('3. Click "Create new API key" and copy it.')
    if not args.no_browser:
        try:
            if webbrowser.open(KEYS_URL):
                print("   (We opened that page in your browser.)")
        except Exception:
            pass
    print()
    key = getpass.getpass("Paste your key here and press Enter (it stays hidden): ").strip()
    if not key:
        print("No key entered. Nothing was saved.")
        return 1
    if not key.startswith("sk-") or re.search(r"\s", key):
        print("That doesn't look like a DeepSeek key (they start with 'sk-').")
        if input("Use it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was saved.")
            return 1

    print("Testing the key with one search...")
    try:
        Client(key).search("DeepSeek", count=1)
        print("It works.")
    except AuthError:
        print("DeepSeek says this key is wrong (401). Copy it again, or create a new one at %s" % KEYS_URL)
        print("Nothing was saved.")
        return AuthError.exit_code
    except BalanceError:
        print("The key is fine, but your balance is too low. Top up at https://platform.deepseek.com/top_up")
        print("Then run setup again. Nothing was saved.")
        return BalanceError.exit_code
    except DsrError as e:
        print("Could not test the key: %s" % redact(e, key))
        if input("Save it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was saved.")
            return 1

    path = save_key(key)
    print("Saved the key ending in %s to %s (only you can read it)." % (key[-4:], path))
    print()
    print("Tip: you can use an environment variable instead of the file:")
    print("  export %s=sk-...      (macOS/Linux)" % ENV_KEY)
    print('  setx %s "sk-..."      (Windows)' % ENV_KEY)
    print("The environment variable wins if both are set.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    key, source = load_key()
    if not key:
        raise NoKeyError(
            "No DeepSeek API key found. Ask the user to run this in their own terminal:\n"
            "  python3 %s setup" % Path(__file__).resolve())
    where = "environment variable %s" % ENV_KEY if source == "env" else str(CONFIG_PATH)
    client = Client(key)
    results = client.search("DeepSeek API", count=1)
    print("OK: key %s from %s works." % (mask_key(key), where))
    print("Search API reachable (%d result). Cost so far: $%.5f" % (len(results), client.estimated_cost))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    client = Client(require_key())
    query = compose_query(args.query, args.lang, args.after, args.before, args.site, args.not_site)
    results = client.search(query, count=args.count, ori_question=args.question,
                            localization=args.localization, need_content=args.content)
    if args.json:
        print_json({"query": query, "results": results})
    else:
        print(format_results(results, query))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    client = Client(require_key())
    page = client.fetch(args.url)
    if args.json:
        print_json(page)
    else:
        print(format_page(page))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan))
    client = Client(require_key())
    run_batch(plan, Path(args.out), client, max_fetches=args.max_fetches,
              log=lambda m: print(m, file=sys.stderr))
    if args.json:
        print_json(read_json(Path(args.out) / "sources.json", "sources file"))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    spec = read_json(Path(args.spec), "report spec")
    page, warnings = build_report(spec)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    for w in warnings:
        print("Warning: " + w, file=sys.stderr)
    print("Wrote %s (%d KB)" % (out, max(1, len(page.encode("utf-8")) // 1024)))
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dsr.py", description="Web research with DeepSeek Search & Fetch.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    s = sub.add_parser("setup", help="guided API key setup (run in your own terminal)")
    s.add_argument("--no-browser", action="store_true", help="don't open the key page")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("check", help="verify the key works")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("search", help="one web search")
    s.add_argument("query")
    s.add_argument("--count", type=int, default=10, help="1-50 results (default 10)")
    s.add_argument("--content", action="store_true", help="include full page content")
    s.add_argument("--lang", help="e.g. en or zh,en (adds lang:)")
    s.add_argument("--after", help="e.g. 2025 or 2025-01-01 (adds after:)")
    s.add_argument("--before", help="e.g. 2024-12-31 (adds before:)")
    s.add_argument("--site", action="append", help="limit to a site (repeatable)")
    s.add_argument("--not-site", action="append", help="exclude a site (repeatable)")
    s.add_argument("--question", help="the user's original question (ori_question)")
    s.add_argument("--localization", help="IP address to localize results")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("fetch", help="one page as Markdown")
    s.add_argument("url")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("batch", help="run a research plan: searches + fetches into a run dir")
    s.add_argument("plan", help="plan.json (see references/advanced-mode.md)")
    s.add_argument("--out", required=True, help="run directory, e.g. research/<slug>-<YYYYMMDD-HHMM>")
    s.add_argument("--max-fetches", type=int, help="cap on pages to fetch (default: plan max_fetches or 25)")
    s.add_argument("--json", action="store_true", help="print sources.json when done")
    s.set_defaults(func=cmd_batch)

    s = sub.add_parser("report", help="build the HTML report from a report spec")
    s.add_argument("spec", help="spec.json (see references/report-spec.md)")
    s.add_argument("--out", required=True, help="output file, e.g. research/<run>/report.html")
    s.add_argument("--open", action="store_true", help="open the report in the browser")
    s.set_defaults(func=cmd_report)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    key = None
    try:
        key, _ = load_key()
    except DsrError:
        pass
    try:
        return args.func(args)
    except DsrError as e:
        print("Error: " + redact(e, key), file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
