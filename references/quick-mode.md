# Quick mode

For simple factual questions. The answer goes in chat. No HTML, no run directory.

## 1. Write 2-4 queries

Each query targets one angle. Don't send the user's sentence as is. Rewrite it into keywords plus operators.

| Need | Example |
|---|---|
| Recent facts | `deepseek api pricing after:2025-06` |
| Official source | `python 3.13 release date site:python.org` |
| Exact name | `"Global EV Outlook 2025" sales` |
| Local topic | `上海 地铁 票价 lang:zh` plus an English query |
| Remove noise | `best rice cooker review -site:amazon.com -site:pinterest.com` |

See [query-craft.md](query-craft.md) for more.

## 2. Search

```
python3 <skill_dir>/scripts/dsr.py search "<query>" --count 10 --question "<user's question>"
```

- Always pass `--question` with the user's original question (it becomes `ori_question` and improves ranking).
- The flags `--site`, `--not-site`, `--lang`, `--after` and `--before` add operators for you.
- Run independent queries in parallel if your tool allows. The CLI keeps itself under the rate limit.

## 3. Pick sources

From the numbered output:

1. Prefer `*` results (authority >= 3 and rerank >= 1.5).
2. Then other results with authority >= 3, then rerank >= 1.5.
3. Treat authority 0-1 or rerank < 1 with care. Use them only if nothing better exists, and say so.
4. Check the date. For anything that changes (prices, versions, rules, people in roles), use the newest reliable source.

## 4. Fetch when snippets are not enough

If the snippet doesn't hold the exact fact, number or quote you need, fetch the 1-3 best pages:

```
python3 <skill_dir>/scripts/dsr.py fetch "<url>"
```

Don't fetch more than 3 pages in quick mode. If you need more than that, switch to advanced mode, and tell the user.

## 5. Answer

Format:

```
<Direct answer in 1-2 sentences.> [1]

- <Key fact> [1]
- <Key fact with number, date> [2]
- <Fact only one source gives> [3] (only one source says this)

Sources
[1] <Title>, <site>, <date>, <url>
[2] ...
```

Rules:
- Answer first. No preamble.
- Every fact gets a citation number that matches the source list.
- Flag anything that only one source says.
- If sources disagree, give both and say which looks more reliable and why.
- If you couldn't find it, say so plainly and say what you tried.
- Plain words, short sentences.
