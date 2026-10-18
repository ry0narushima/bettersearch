# Redesign brief: the HTML research report

Use the `frontend-design` skill (installed at ~/.claude/skills/frontend-design/SKILL.md). Read it first and follow its process: token plan, review against brief, build, critique with screenshots.

## Direction (pinned by the client, this wins over the skill's "avoid" list)

Anthropic's visual identity: colors, theme, fonts. The client explicitly wants the Anthropic look, so the cream + clay accent is the brief, not a tell.

Anthropic brand palette (official brand guidelines):
- Dark `#141413` (text, dark-mode background)
- Light `#faf9f5` (light background)
- Mid gray `#b0aea5` (secondary)
- Light gray `#e8e6dc` (subtle backgrounds, rules)
- Orange `#d97757` (primary accent)
- Blue `#6a9bcc` (secondary accent)
- Green `#788c5d` (tertiary accent)

Fonts (brand guidelines): Poppins for headings, Lora for body text. Woff2 files (latin subset, OFL) are in `scripts/fonts/`. Lora is a variable font, so `Lora-400.woff2` and `Lora-600.woff2` are the same file: embed it once with a weight range. The report must stay a single self-contained offline file, so `dsr.py report` must inline the fonts as base64 `@font-face` data URIs. Fallbacks: Georgia for Lora, system sans for Poppins.

Charts use the brand accents (orange, blue, green, then mid gray). Dark mode keeps the brand: `#141413` background, `#faf9f5` text, same accents.

## Keep
- Everything in `references/report-spec.md` keeps working. Same spec JSON in, no schema changes.
- Offline, no CDN, no external JS. XSS-safe escaping stays.
- Responsive to mobile, visible keyboard focus, prefers-reduced-motion, dark mode via prefers-color-scheme.
- Grade-8, sentence-case copy in the template chrome.

## Do
1. Plan tokens and layout per the skill, write the plan into the commit message body.
2. Rebuild `scripts/report_template.html` and whatever in `dsr.py` renders charts/fonts.
3. Regenerate `examples/example-report.html`.
4. Screenshot it for self-critique with headless Chrome. This exact command is allowed:
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --user-data-dir=/tmp/hl-chrome --window-size=820,2600 --screenshot=/tmp/report.png file://<abs path to examples/example-report.html>
   Also check a 390px wide mobile screenshot. Iterate until it looks premium.
5. Save the final screenshot to `docs/screenshot.png` and show it at the top of README.md.
6. Add `scripts/fonts/` licenses note in README credits. All tests must pass (add tests for font embedding). Commit. Do not push.
