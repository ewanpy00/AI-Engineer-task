#!/bin/bash
INPUT=$(cat)
FP=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FP" ] && exit 0
case "$FP" in
  */docs/*|docs/*) exit 0 ;;
  *) echo "architect пишет только в docs/. Отклонено: $FP" >&2; exit 2 ;;
esac
