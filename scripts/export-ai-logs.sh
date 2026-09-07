#!/bin/bash
set -euo pipefail

SLUG=$(pwd | sed 's/[/._]/-/g')
SRC="$HOME/.claude/projects/$SLUG"
DST="ai-logs/sessions"

[ -d "$SRC" ] || { echo "Не найдено: $SRC"; exit 1; }
mkdir -p "$DST"
rsync -a --include='*/' --include='*.jsonl' --exclude='*' "$SRC/" "$DST/"

{
  echo "# Сессии Claude Code"
  echo
  echo "Транскрипты разработки по стадиям. Ниже первый промпт каждой сессии."
  echo
  for f in $(find "$DST" -maxdepth 1 -name '*.jsonl' | sort); do
    FIRST=$(jq -rs 'map(select(.type=="user")) | .[0].message.content // "" | if type=="array" then (.[0].text // "") else . end' "$f" 2>/dev/null | head -c 400)
    echo "## $(basename "$f")"
    echo '```'
    echo "$FIRST"
    echo '```'
    echo
  done
} > ai-logs/README.md

echo "Файлов: $(find "$DST" -name '*.jsonl' | wc -l | tr -d ' ')"
