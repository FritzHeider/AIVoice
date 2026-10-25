#!/usr/bin/env bash
# Print per-phase progress from the plan checklists.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
printf '%-8s %-6s %-6s %s\n' PHASE DONE TOTAL TITLE
for f in plan/phase-*.md; do
  nn="$(basename "$f" .md | sed 's/phase-//')"
  done_count="$(grep -cE '^\s*- \[x\]' "$f" || true)"
  total="$(grep -cE '^\s*- \[( |x)\]' "$f" || true)"
  title="$(head -n1 "$f" | sed 's/^# //')"
  printf '%-8s %-6s %-6s %s\n' "$nn" "$done_count" "$total" "$title"
done
