# Query craft

DeepSeek Search takes a query of 1-128 characters. Operators go inside the query text.

## Operators

| Operator | Meaning | Example |
|---|---|---|
| `site:` | Only this site or domain | `site:iea.org`, `site:gov.uk` |
| `-site:` | Never this site | `-site:pinterest.com` |
| `"..."` | Exact phrase | `"battery pack prices"` |
| `-term` | Exclude a word | `jaguar -car` |
| `-"..."` | Exclude a phrase | `python -"monty python"` |
| `lang:` | Result language(s) | `lang:en`, `lang:zh,en` |
| `after:` | Published after | `after:2025`, `after:2025-01-01` |
| `before:` | Published before | `before:2024-12-31` |

The CLI can add these for you: `--site`, `--not-site`, `--lang`, `--after`, `--before`.
In `plan.json`, write them into the query string.

## Recipes

**Keywords, not sentences.** `ev sales 2024 china share` beats "how many electric cars did China sell last year?"
Put the full question in `ori_question` (`--question`) instead. Search uses it to rank results.

**Recent topics.** Add `after:` with a date a little before the period you care about.
For "latest" questions, `after:` the last 6-12 months.

**Official sources.** Pair a topical query with a `site:` query for the authority:
- statistics: `site:ec.europa.eu/eurostat`, `site:census.gov`, `site:stats.gov.cn`
- regulators and laws: `site:sec.gov`, `site:eur-lex.europa.eu`, `site:federalregister.gov`
- companies: `site:investor.<company>.com`, or the press release by exact title
- science: `site:nature.com`, `site:arxiv.org`, `site:pubmed.ncbi.nlm.nih.gov`

**Local topics.** Search in the local language too: `lang:zh` for China, `lang:ja` for Japan,
`lang:de` for Germany. Run the English query as well and compare.

**Counter-views.** For any contested claim, add a query that looks for the other side:
`"<claim keyword>" criticism`, `"<claim keyword>" debunked`, `<topic> risks`, `<topic> study limitations`.

**Numbers.** Add the unit and the year: `heat pump installations 2024 million units europe`.

**Exact documents.** Quote the title: `"Global EV Outlook 2025"`.

**Noise.** Remove content farms and shops: `-site:pinterest.com -site:amazon.com -site:quora.com`.

## Reading results

- `rerank_score` 0-3: how well the result matches. 1.5 or more is high quality.
- `authority` 0-4: how trustworthy the site is (-1 = unknown). 3 or more is highly authoritative.
- `date`: may be missing. Check the page if the date matters.
- `snippet` is short (100-200 chars). `summary` (600-800 chars, in `--json`) is written for models
  and is often enough to decide whether to fetch.

## Budget

Each successful search costs $0.00075. Results with no hits are free. `count` doesn't change
the price, so ask for 20-50 results in advanced mode and 10 in quick mode.
