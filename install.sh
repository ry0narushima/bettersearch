#!/usr/bin/env bash
# Install the bettersearch skill into every agent skills folder found.
#
#   curl -fsSL https://raw.githubusercontent.com/ry0narushima/bettersearch/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --project      # into ./.claude/skills etc.
#   ./install.sh [--project] [--dir PATH] [--dry-run]
#
# Env: DSR_REPO (owner/name), DSR_BRANCH (default main).
set -euo pipefail

NAME="bettersearch"
REPO="${DSR_REPO:-ry0narushima/bettersearch}"
BRANCH="${DSR_BRANCH:-main}"
MODE="user"
DRY_RUN=0
EXTRA_DIRS=()

usage() {
  sed -n '2,9p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true
  echo "Options:"
  echo "  --project     install into this project (./.claude/skills, ./.agents/skills, ...)"
  echo "  --dir PATH    also install into PATH (a skills folder)"
  echo "  --dry-run     show what would happen"
  echo "  -h, --help    this help"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --project) MODE="project" ;;
    --dir) shift; [ $# -gt 0 ] || { echo "--dir needs a path" >&2; exit 1; }; EXTRA_DIRS+=("$1") ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then say "  would run: $*"; else "$@"; fi; }

# 1. Find the skill files: this checkout, or download to a temp dir.
TMP=""
cleanup() { if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT

SRC=""
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  HERE="$(cd "$(dirname "$SELF")" && pwd)"
  if [ -f "$HERE/SKILL.md" ] && [ -f "$HERE/scripts/dsr.py" ]; then
    SRC="$HERE"
  fi
fi
if [ -z "$SRC" ]; then
  TMP="$(mktemp -d 2>/dev/null || mktemp -d -t dsr)"
  say "Downloading $REPO ($BRANCH)..."
  if command -v git >/dev/null 2>&1 && git clone -q --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$TMP/src" 2>/dev/null; then
    SRC="$TMP/src"
  elif command -v curl >/dev/null 2>&1; then
    mkdir -p "$TMP/src"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$TMP/src" --strip-components=1
    SRC="$TMP/src"
  else
    echo "Need git or curl to download the skill." >&2
    exit 1
  fi
  [ -f "$SRC/SKILL.md" ] || { echo "Download failed: SKILL.md not found." >&2; exit 1; }
fi

# 2. Pick target skills folders.
TARGETS=()
add() { local d; for d in "${TARGETS[@]+"${TARGETS[@]}"}"; do [ "$d" = "$1" ] && return; done; TARGETS+=("$1"); }

if [ "$MODE" = "project" ]; then
  BASE="$(pwd)"
  add "$BASE/.claude/skills"      # Claude Code
  add "$BASE/.agents/skills"      # shared standard (Codex, Cursor, Gemini CLI, OpenCode, ...)
  for agent in .codex .cursor .gemini .opencode .hermes; do
    [ -d "$BASE/$agent" ] && add "$BASE/$agent/skills"
  done
else
  H="$HOME"
  [ -d "$H/.claude" ]          && add "$H/.claude/skills"
  [ -d "$H/.codex" ]           && add "$H/.codex/skills"
  [ -d "$H/.agents" ]          && add "$H/.agents/skills"
  [ -d "$H/.cursor" ]          && add "$H/.cursor/skills"
  [ -d "$H/.gemini" ]          && add "$H/.gemini/skills"
  [ -d "$H/.config/opencode" ] && add "$H/.config/opencode/skills"
  [ -d "$H/.hermes" ]          && add "$H/.hermes/skills"
  if [ "${#TARGETS[@]}" -eq 0 ]; then
    say "No agent folders found. Installing to ~/.claude/skills and ~/.agents/skills."
    add "$H/.claude/skills"
    add "$H/.agents/skills"
  fi
fi
for d in "${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}"; do add "$d"; done

# 3. Copy (replace an older install of this skill only).
FILES=(SKILL.md README.md LICENSE scripts references examples)
for dir in "${TARGETS[@]}"; do
  dest="$dir/$NAME"
  say "Installing to $dest"
  run mkdir -p "$dir"
  stage="$dir/.$NAME.tmp.$$"
  run rm -rf "$stage"
  run mkdir -p "$stage"
  for f in "${FILES[@]}"; do
    [ -e "$SRC/$f" ] && run cp -R "$SRC/$f" "$stage/"
  done
  run find "$stage" -name '__pycache__' -prune -exec rm -rf {} +
  run chmod +x "$stage/scripts/dsr.py"
  case "$dest" in
    */"$NAME") run rm -rf "$dest" ;;
  esac
  run mv "$stage" "$dest"
done

# 4. Next steps.
PY="python3"
command -v python3 >/dev/null 2>&1 || PY="python"
if ! command -v "$PY" >/dev/null 2>&1; then
  say ""
  say "Note: Python 3.9+ was not found. Install it from https://www.python.org/downloads/"
fi
FIRST="${TARGETS[0]}/$NAME"
say ""
say "Done. Installed into ${#TARGETS[@]} folder(s)."
say ""
say "Next: add your DeepSeek API key (one time, about 2 minutes):"
say "  $PY \"$FIRST/scripts/dsr.py\" setup"
say ""
say "Then ask your agent something like: \"research how heat pumps compare to gas boilers\""
