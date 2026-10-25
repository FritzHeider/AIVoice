#!/usr/bin/env bash
# Assemble PROMPT.md for one phase and run the Ralph loop until the phase completes.
# Usage: scripts/ralph/run_phase.sh <NN> [extra ralph run args...]
#   NN = two-digit phase number 00..20
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <phase NN> [extra ralph args]" >&2
  exit 64
fi

PHASE="$1"; shift
if [[ ! "$PHASE" =~ ^[0-9]{2}$ ]]; then
  echo "phase must be two digits (e.g. 05), got: $PHASE" >&2
  exit 64
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PHASE_PROMPT="prompts/phase-${PHASE}.md"
PHASE_PLAN="plan/phase-${PHASE}.md"
for f in MASTER_PROMPT.md "$PHASE_PROMPT" "$PHASE_PLAN" ralph.yml; do
  [[ -f "$f" ]] || { echo "missing required file: $f" >&2; exit 66; }
done

command -v ralph >/dev/null 2>&1 || {
  echo "ralph CLI not found. Install: npm install -g @ralph-orchestrator/ralph-cli  (or: cargo install ralph-cli)" >&2
  exit 69
}

# Initialise git on first run so Ralph checkpoints and Reviewer commits work.
if [[ ! -d .git ]]; then
  git init -b main
  git add -A
  git commit -m "chore: import specs, prompts, plans, and Ralph configuration" >/dev/null
fi

{
  cat MASTER_PROMPT.md
  printf '\n\n'
  printf '# CURRENT PHASE: %s\n\n' "$PHASE"
  printf 'Phase checklist file: `%s` (tick tasks here). Phase prompt source: `%s`.\n\n' "$PHASE_PLAN" "$PHASE_PROMPT"
  cat "$PHASE_PROMPT"
} > PROMPT.md

mkdir -p .ralph/agent logs
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="logs/ralph-phase-${PHASE}-${STAMP}.log"

echo "▶ Running phase ${PHASE} (log: ${LOG})"
set +e
ralph run -c ralph.yml "$@" 2>&1 | tee "$LOG"
STATUS=${PIPESTATUS[0]}
set -e

REMAINING="$(grep -cE '^\s*- \[ \]' "$PHASE_PLAN" || true)"
if [[ "$STATUS" -ne 0 ]]; then
  echo "✖ ralph exited with status ${STATUS} during phase ${PHASE}" >&2
  exit "$STATUS"
fi
if [[ "$REMAINING" -ne 0 ]]; then
  echo "✖ phase ${PHASE} ended with ${REMAINING} unchecked task(s) in ${PHASE_PLAN}; re-run to continue" >&2
  exit 75
fi
echo "✔ phase ${PHASE} complete"
