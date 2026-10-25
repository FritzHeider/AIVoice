#!/usr/bin/env bash
# Run phases sequentially from FROM to TO (inclusive). Stops at the first phase that does not complete.
# Usage: scripts/ralph/run_all.sh [FROM=00] [TO=20]
set -euo pipefail
FROM="${1:-00}"
TO="${2:-20}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
for ((n=10#$FROM; n<=10#$TO; n++)); do
  NN="$(printf '%02d' "$n")"
  REMAINING="$(grep -cE '^\s*- \[ \]' "plan/phase-${NN}.md" || true)"
  if [[ "$REMAINING" -eq 0 ]]; then
    echo "• phase ${NN} already complete, skipping"
    continue
  fi
  attempts=0
  until scripts/ralph/run_phase.sh "$NN"; do
    attempts=$((attempts + 1))
    if [[ "$attempts" -ge 3 ]]; then
      echo "✖ phase ${NN} did not complete after ${attempts} runs; inspect logs/ and the scratchpad" >&2
      exit 1
    fi
    echo "↻ re-running phase ${NN} (attempt $((attempts + 1)))"
  done
done
echo "✔ phases ${FROM}..${TO} complete"
