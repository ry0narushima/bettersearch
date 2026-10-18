# Build spec: deepseek-research skill

A portable Agent Skill (SKILL.md standard) that makes any coding agent (Claude Code, Codex, Cursor, Gemini CLI, OpenCode, Hermes...) do much better web research using DeepSeek's new Search & Fetch APIs.

## DeepSeek API facts (from https://api-docs.deepseek.com/guides/search, 2026-10-17)

- Search: POST https://api.deepseek.com/v1/tools/search
  body: {"query": str 1-128 chars, "count": 1-50 (default 10), "ori_question": optional str 1-128, "localization": optional IP str, "need_content": bool (default false)}
  Query operators supported inside query: site:, -site:, -term, -"phrase", "exact", lang:zh,en, after:2025 or after:2025-01-01, before:...
  Response: {"results": [{"rerank_score": 0-3 (>=1.5 high quality), "title", "url", "snippet" (100-200 chars), "summary" (600-800 chars, for models), "content" (empty unless need_content), "site_name"|null, "site_icon"|null, "date" RFC3339|null, "authority" int 0-4 (-1 unknown, >=3 highly authoritative)}]}
- Fetch: POST https://api.deepseek.com/v1/tools/fetch body {"url": str}. Returns page as Markdown (fields include title and content; inspect response defensively).
- Auth headers: Authorization: Bearer <key>, Content-Type: application/json.
- Billing: $0.75 per 1,000 successful requests each. Empty results / failures not billed.
- Rate limit: 5 QPS per account for Search, 5 for Fetch, separately. 429 when exceeded, Fetch also has per-domain QPS. Must throttle + retry with backoff on 429.
- Keys: https://platform.deepseek.com/api_keys (account needs a topped-up balance).

## Repo layout

deepseek-research/
  SKILL.md                 the skill (frontmatter: name, description). Under ~300 lines. Progressive disclosure: details in references/.
  README.md                human-facing: what it does, 1-line install, setup, examples, screenshot placeholder, license MIT
  LICENSE                  MIT
  install.sh               installs the skill folder into every agent skills dir it finds: ~/.claude/skills, ~/.codex/skills, ~/.agents/skills, ~/.cursor/skills, ~/.gemini/skills, ~/.config/opencode/skills, ~/.hermes/skills. Also supports `--project` to install into ./.claude/skills etc. Works via `curl -fsSL <raw url>/install.sh | bash` (clones or downloads tarball to a temp dir).
  scripts/
    dsr.py                 single stdlib-only Python 3.9+ CLI (no pip installs):
                             dsr.py setup        guided key setup (see below)
                             dsr.py check        verifies key works, prints balance-free status
                             dsr.py search "q" [--count N] [--content] [--lang] [--after] [--site ...] [--json]
                             dsr.py fetch URL [--json]
                             dsr.py batch plan.json --out run_dir   runs many searches + fetches with throttling (<=5 QPS), dedupes by URL, writes sources.json + raw/ markdown files
                             dsr.py report spec.json --out report.html   builds the HTML report
                           Output for agents: compact, numbered, with url, date, authority, rerank_score, snippet. --json for machines.
    report_template.html   template used by `dsr.py report`
  references/
    quick-mode.md
    advanced-mode.md       the full multi-agent workflow
    roles.md               prompts for each subagent role
    query-craft.md         how to write good queries with operators
    report-spec.md         JSON schema of the report spec
  examples/
    example-spec.json      a realistic spec
    example-report.html    generated from example-spec.json
  tests/
    test_dsr.py            unittest, no network: mocks urllib, covers key loading, throttling, retry on 429, dedupe, report rendering, HTML escaping

## Key setup (must be friendly to non-developers)

`dsr.py setup`:
1. Explains in 3 short lines what a key is and that it costs $0.75 per 1,000 searches.
2. Prints the URL https://platform.deepseek.com/api_keys and tries to open it in the browser (webbrowser module), tells them to sign up, top up a small balance, click "Create new API key", copy it.
3. Reads the key with getpass (never echoed). Validates format loosely (starts with "sk-").
4. Makes one real test search. On 401 says the key is wrong, on 402/insufficient balance says top up.
5. Saves to ~/.config/deepseek-research/config.json with chmod 600. Never prints the key back; shows only last 4 chars.
6. Prints how to also use env var DEEPSEEK_API_KEY instead.
Key lookup order: env DEEPSEEK_API_KEY, then config file. Never write key into the skill folder, never log it, redact it from any error output.

In SKILL.md: when no key is found, the agent must walk the user through setup: tell them to run `python3 <skill_dir>/scripts/dsr.py setup` in their own terminal (because the agent should never see or handle the key), and wait. The agent must never ask the user to paste the key into chat.

## Two modes

The user picks, or the agent infers: "quick" is default for simple factual questions; "advanced" when the user says deep/advanced/thorough/report, or the question is broad, contested, or needs numbers.

### Quick mode
- Agent writes 2 to 4 targeted queries (use operators: after: for recent topics, site: for official sources, lang:).
- count 10 each, ori_question set to the user's question.
- Prefer results with authority >= 3 and rerank_score >= 1.5. Fetch the 1 to 3 best pages when snippets aren't enough.
- Answer in chat: direct answer first, then key facts with inline numbered citations [1], then source list with dates. Flag anything only one source says. No HTML.

### Advanced mode (multi-agent)
1. Planner (main agent): break the question into 3 to 6 sub-questions. Write plan.json with 8 to 20 queries (count 20 to 50), with operators.
2. Gather: `dsr.py batch plan.json` collects sources, fetches top pages (authority/rerank ranked, cap ~25 fetches).
3. Researchers: one subagent per sub-question, in parallel, each reads its sources (raw/ files) and returns findings as structured claims: {claim, value, unit, date, source_ids, quote (verbatim)}.
4. Fact-checker subagent: verifies every claim against the fetched text (quote must appear in source), checks numbers across sources, marks each claim verified / single-source / conflicting / unsupported. Drops unsupported.
5. Reviewer subagent: checks gaps, bias, stale data, missing counter-views, sends back max one round of follow-up queries to gather.
6. Writer: builds report spec JSON (see report-spec.md) only from verified claims, picks charts where numbers support them, picks images from source pages (og:image or images found in fetched markdown), then `dsr.py report spec.json --out report.html` and opens it.
7. Chat output: 5-line summary + path to report.html.

Portability rule in SKILL.md: if the agent has a subagent/Task tool (Claude Code Task, Codex subagents, etc.) use it with the role prompts from references/roles.md. If not, run the roles one after another yourself, keeping each role's output in a separate file in the run dir. The workflow must still work.

Run directory: ./research/<slug>-<YYYYMMDD-HHMM>/ with plan.json, sources.json, raw/, claims.json, review.md, spec.json, report.html.

## The HTML report (the showpiece, must look beautiful)

Single self-contained file, opens offline (no CDN, no external JS). Charts drawn as inline SVG by dsr.py (bar, horizontal bar, line, donut, stat cards) from the spec. Images referenced by URL with graceful fallback (hide on error).
Design: clean editorial style, like a premium newsletter. System font stack, generous whitespace, max width ~760px, light theme with automatic dark mode (prefers-color-scheme), one accent color. Mobile friendly.
Sections:
- Hero: title, one-line answer (TL;DR), date, mode, counts (sources read, claims verified).
- Key takeaways: 3 to 5 cards, very simple language, each with citation chips.
- Numbers: stat cards + charts.
- Sections per sub-question: short paragraphs, images with captions and credit.
- Confidence: each claim tagged verified / single source / conflicting, with a small legend.
- What we couldn't confirm.
- Sources: cards with site icon, title, site, date, authority badge.
Citations [n] are links that jump to the source card. Escape all text from the web (XSS-safe). Keep it simple and easy to read: grade-8 language.

## Quality bars
- stdlib only, Python 3.9+. Works on macOS, Linux, Windows.
- Throttle to <= 4 QPS per endpoint, retry 429/5xx with exponential backoff + jitter, max 4 retries, timeouts 30s.
- All tests pass: `python3 -m unittest discover tests`.
- Generate examples/example-report.html from example-spec.json and make sure it renders (valid HTML, no broken template vars).
- SKILL.md description must clearly trigger on "research", "look up", "search the web", "deep research", "fact check".
- No secrets in repo. Add .gitignore (research/, *.env, config.json).
- git init, commit in logical commits on branch main. Do NOT push and do NOT create a GitHub repo.
