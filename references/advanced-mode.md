# Advanced mode: the full multi-agent workflow

Use this for broad, contested or number-heavy questions, or when the user asks for deep research or a report.
The result is a self-contained HTML report plus a short chat summary.

```
Plan ─► Gather ─► Researchers (parallel) ─► Fact-checker ─► Reviewer ─┬─► Writer ─► report.html
                     ▲                                                │
                     └──────────── at most one follow-up round ◄──────┘
```

Role prompts are in [roles.md](roles.md). Run `dsr.py check` first (see SKILL.md, Step 0).

## 0. Make the run directory

```
research/<slug>-<YYYYMMDD-HHMM>/
  plan.json          planner output (batch input)
  sources.json       batch output: every source, numbered
  raw/NNN.md         fetched pages as Markdown (NNN = source id)
  findings-<id>.json one per sub-question (researchers)
  claims.json        fact-checker output
  review.md          reviewer output
  followup.json      optional, reviewer's follow-up queries
  spec.json          writer output (report input)
  report.html        the report
```

`<slug>` is 3-6 lowercase words joined by hyphens, e.g. `ev-market-growth-2025`. Use local time for the stamp.

## 1. Plan (you, the main agent)

Split the question into **3-6 sub-questions** that together answer it. Cover:
the core facts, the numbers or trends, the causes, the counter-view or risks, and what is next.

Write `plan.json` with **8-20 queries**, `count` 20-50 each. Every sub-question gets at least 2 queries.
Use operators (see [query-craft.md](query-craft.md)): `after:` for recent data, `site:` for official
sources, `lang:` for local topics, and at least one query that looks for the counter-view.

```json
{
  "question": "Are electric car sales still growing worldwide?",
  "sub_questions": [
    {"id": "q1", "text": "How fast did global EV sales grow in 2024 and 2025?"},
    {"id": "q2", "text": "Which regions drive the growth?"},
    {"id": "q3", "text": "Are EVs getting cheaper?"},
    {"id": "q4", "text": "What slows growth down?"}
  ],
  "queries": [
    {"query": "global electric car sales 2024 million share after:2025", "count": 30, "sub_question": "q1"},
    {"query": "\"Global EV Outlook 2025\" site:iea.org", "count": 20, "sub_question": "q1"},
    {"query": "China NEV sales 2024 share lang:zh,en", "count": 30, "sub_question": "q2"},
    {"query": "EU battery electric registrations 2024 site:acea.auto", "count": 20, "sub_question": "q2"},
    {"query": "lithium-ion battery pack price 2024 $/kWh", "count": 30, "sub_question": "q3"},
    {"query": "EV sales slowdown reasons 2025 -site:pinterest.com", "count": 30, "sub_question": "q4"}
  ],
  "max_fetches": 25,
  "fetch_urls": []
}
```

Fields:
- `question` (string): the user's question. Sent as `ori_question` with every search.
- `sub_questions` (list of `{id, text}`).
- `queries` (list): `query` (1-128 chars, required), `count` (1-50, default 20), `sub_question` (an id),
  `need_content` (bool, default false; returns the full text with the search, which saves a fetch).
  A plain string also works as a query.
- `max_fetches` (int, default 25): cap on pages to fetch.
- `fetch_urls` (list of URLs): pages to always fetch, e.g. a known official report.

## 2. Gather

```
python3 <skill_dir>/scripts/dsr.py batch research/<run>/plan.json --out research/<run>
```

What it does:
- Runs all searches (under 4 per second, retries on 429/5xx).
- Dedupes results by URL and numbers each source (`id` 1, 2, 3...). These ids are the citation numbers all the way to the report.
- Fetches the best pages: the top 2 per sub-question first, then the best overall by rerank score and authority, up to `max_fetches`.
- Writes `sources.json` and `raw/NNN.md`.

`sources.json` (shortened):
```json
{
  "question": "...",
  "sub_questions": [{"id": "q1", "text": "..."}],
  "billing": {"search_requests": 14, "fetch_requests": 23, "estimated_usd": 0.0278},
  "searches": [{"query": "...", "sub_question": "q1", "results": 30, "error": null, "round": 1}],
  "sources": [{
    "id": 1, "url": "...", "title": "...", "site_name": "IEA", "site_icon": "...",
    "date": "2025-05-14T00:00:00Z", "authority": 4, "rerank_score": 2.7,
    "snippet": "...", "summary": "...", "sub_questions": ["q1", "q2"], "queries": ["..."],
    "fetched": true, "raw_file": "raw/001.md", "content_chars": 48211,
    "images": [{"url": "https://...", "alt": "..."}], "fetch_error": null
  }]
}
```

If many fetches fail (paywalls, bot blocks), that is normal. Researchers can still use `summary`
for context, but a verified claim needs a quote from a `raw/` file.

## 3. Researchers (one per sub-question, in parallel)

Give each researcher: its sub-question, the run dir path, and the list of source ids tagged with
its sub-question (from `sources.json`). Prompt: **Researcher** in [roles.md](roles.md).
Each writes `findings-<id>.json`:

```json
{"sub_question": "q1", "claims": [
  {"claim": "Global electric car sales passed 17 million in 2024.",
   "value": 17, "unit": "million cars", "date": "2024",
   "source_ids": [1], "quote": "Electric car sales exceeded 17 million worldwide in 2024"}
], "gaps": ["No 2025 full-year data yet."]}
```

## 4. Fact-checker

One fact-checker gets every `findings-*.json` file. Prompt: **Fact-checker** in [roles.md](roles.md).
For each claim it:
1. Opens `raw/NNN.md` for each source id and checks that the quote appears there (allowing
   whitespace and punctuation differences). Use `grep -F` or read the file.
2. Checks that the claim says what the quote says (same number, unit, year, scope).
3. Compares numbers across sources.
4. Marks the claim `verified` (quote found, and either 2+ sources agree or 1 primary source with no contradiction),
   `single-source` (quote found, only one non-primary source), `conflicting` (sources disagree; add a `note`
   that explains how), or `unsupported` (quote not found, or the claim goes beyond it).
5. Drops `unsupported` claims from the main list and keeps them in `dropped` for the report's
   "What we couldn't confirm" section.

Output `claims.json`:
```json
{"claims": [
  {"id": "c1", "sub_question": "q1", "claim": "...", "value": 17, "unit": "million cars", "date": "2024",
   "source_ids": [1, 2], "quote": "...", "status": "verified", "note": ""}
], "dropped": [{"claim": "...", "reason": "quote not found in raw/007.md"}]}
```

## 5. Reviewer

Prompt: **Reviewer** in [roles.md](roles.md). It reads `plan.json`, `sources.json` (titles, dates, sites)
and `claims.json`, and writes `review.md`: gaps, bias (e.g. all sources from one country or one industry),
stale data, and missing counter-views.

If the gaps matter, it also writes `followup.json` (same format as `plan.json`, **at most 6 queries**).
Then you run **one** follow-up round:

```
python3 <skill_dir>/scripts/dsr.py batch research/<run>/followup.json --out research/<run>
```

This adds to the same `sources.json`. Old ids stay the same, and queries already run are skipped.
Send only the new sources to a researcher, and only the new claims to the fact-checker, then merge them into `claims.json`.
**Never do a second follow-up round.** What is still missing goes into "What we couldn't confirm".

## 6. Writer

Prompt: **Writer** in [roles.md](roles.md). It builds `spec.json` ([report-spec.md](report-spec.md))
**only from claims in `claims.json`** (verified, single-source or conflicting), plus:
- 3-5 key takeaways in very simple words, each with citations.
- Stat cards and charts where numbers support them (2+ comparable values for a chart).
- One section per sub-question, with short paragraphs and 0-2 images. Images come from
  `sources.json` → `images` (og:image or images found in the fetched Markdown), credited to the source site.
- Every claim in `claims`, the dropped ones and the open gaps in `unconfirmed`.
- `sources`: only the sources that are cited, with the same ids as `sources.json`.
- `counts`: `searches`, `sources_found` (all sources), `sources_read` (fetched sources).
- `cost_usd`: `billing.estimated_usd` from `sources.json`.

Then:
```
python3 <skill_dir>/scripts/dsr.py report research/<run>/spec.json --out research/<run>/report.html --open
```
Fix every warning it prints (usually a citation id with no matching source), then run it again.

## 7. Chat output

Exactly this shape:

```
<one-line answer>
- <takeaway 1>
- <takeaway 2>
- <takeaway 3>
- <confidence: N verified, N single-source, N conflicting; cost ~$0.03>

Report: research/<run>/report.html
```

## No subagent tool?

Do every role yourself, in order, and **save each role's output to its own file before starting the next**
(`findings-<id>.json`, then `claims.json`, then `review.md`, then `spec.json`). When you play the fact-checker,
re-open the raw files. Don't trust your memory of what the researcher read. The files keep the roles honest.
