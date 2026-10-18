# Report spec (`spec.json`)

`dsr.py report spec.json --out report.html` turns this JSON into one self-contained HTML file:
inline CSS, inline SVG charts, no external scripts. It works offline and in light or dark mode.
A full example is [examples/example-spec.json](../examples/example-spec.json).

All text is HTML-escaped. Inside text fields you may use:
- `[n]` or `[n, m]`: citation chips that link to source card `n`
- `**bold**`

Only `title` is required. Empty sections are left out.

## Schema

```jsonc
{
  "title": "string (required)",
  "question": "string: the user's question",
  "date": "YYYY-MM-DD (default: today)",
  "mode": "quick | advanced (default advanced)",
  "lang": "en (html lang attribute)",
  "notice": "string: optional banner above the title",
  "tldr": "string with [n]" | {"text": "string", "cites": [1, 2]},
  "counts": {                       // hero numbers; all optional
    "searches": 14,
    "sources_found": 186,
    "sources_read": 22,             // default: number of sources
    "claims_verified": 9            // default: claims with status verified
  },
  "cost_usd": 0.027,                // shown in the footer

  "takeaways": [                    // 3-5
    "string with [n]" | {"text": "string", "cites": [1]}
  ],

  "numbers": {
    "heading": "string (default 'The key figures')",
    "intro": "string with [n]",
    "stats": [                      // 2-4 stat cards
      {"label": "string", "value": 17000000 | "string", "unit": "string",
       "note": "string with [n]", "cites": [1]}
    ],
    "charts": [ Chart ]
  },

  "sections": [                     // one per sub-question
    {"kicker": "small label above the heading",
     "heading": "string",
     "paragraphs": ["string with [n]" | {"text": "string", "cites": [1]}],
     "images": [
       {"url": "https://...", "alt": "string", "caption": "string with [n]",
        "credit": "site name", "credit_url": "https://...", "source": 3}
     ]}
  ],

  "claims": [
    {"text": "string",              // or "claim"
     "status": "verified | single-source | conflicting | unsupported",
     "source_ids": [1, 2],          // or "cites"
     "note": "string with [n]"}
  ],

  "unconfirmed": [ "string" | {"text": "string", "note": "string"} ],

  "sources": [
    {"id": 1, "title": "string", "url": "https://...", "site_name": "string",
     "site_icon": "https://...", "date": "YYYY-MM-DD or RFC3339", "authority": 4}
  ]
}
```

### Chart

```jsonc
{
  "type": "bar | hbar | line | donut",
  "title": "string",
  "subtitle": "string: what is measured, in which unit",
  "unit": "% | M | K | B | $ | $/kWh | million | ...",
  "caption": "string with [n]",
  "cites": [1],

  // bar, hbar, donut:
  "data": [{"label": "2024", "value": 17.3}],

  // line (one or more series, max 8; null = gap):
  "labels": ["2021", "2022", "2023"],
  "series": [{"name": "World", "values": [9, 14, null]}],

  // donut only:
  "center_value": "17.3M",          // default: compact total
  "center_label": "cars in 2024"    // default: "total"
}
```

Which chart:
| Data | Type |
|---|---|
| A few values or years | `bar` |
| Many items, or long labels | `hbar` |
| Change over time, 1-8 series | `line` |
| Parts of one whole (max 8, the rest fold into "Other") | `donut` |
| One headline number | a stat card, not a chart |

Never mix units in one chart. Two measures on different scales need two charts.
Units `%`, `K`, `M`, `B`, `T`, `x` attach to the number (`17.3M`). Currency units go in front (`$/kWh` → `$115/kWh`).
Other units follow after a space (`17.3 million`).

Every chart gets hover tooltips and a "Show data" table automatically.

## Status meanings

| status | Tag | Meaning |
|---|---|---|
| `verified` | ✓ Verified | Quote found in the source, and sources agree or a primary source states it. |
| `single-source` | 1 Single source | Quote found, but only one source says it. |
| `conflicting` | ! Conflicting | Sources disagree. Explain in `note`. |
| `unsupported` | (moved) | Shown under "What we couldn't confirm", not under Confidence. |

## Safety

- Every string is HTML-escaped. Braces are escaped too, so web text can't inject template variables.
- Only `http(s)` URLs become links or images. Anything else (`javascript:`, `data:`) is dropped, with a warning.
- Images and site icons hide themselves if they fail to load.
- Citations to unknown source ids render as plain grey numbers, and `report` prints a warning.
