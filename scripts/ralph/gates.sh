#!/usr/bin/env bash
# Quality gates with one stamp per working tree, so a tree that already passed is not run a second time.
#
#   scripts/ralph/gates.sh [--ts] [--e2e]            run the core gates (plus `make test-ts` / `make e2e`)
#   scripts/ralph/gates.sh --only test,e2e           run just these gates and add them to the tree's stamp
#   scripts/ralph/gates.sh --verify [--ts] [--e2e]   exit 0 only if every listed gate passed on exactly this tree
#   scripts/ralph/gates.sh --verify --only e2e       the same check for single gates
#   scripts/ralph/gates.sh --fingerprint             print the working-tree fingerprint
#
# Core gates, in order: lint typecheck placeholders openapi specs test. Optional gates: ts e2e.
# A run stops at the first failing gate and exits with that gate's status.
#
# The fingerprint is the git tree hash of every tracked and untracked (not ignored) file outside .ralph/, taken
# through a throwaway index, so staging state does not matter and any edit after a run makes its stamp stale.
# The stamp .ralph/gates/<fingerprint>.json (git-ignored) is written only when the fingerprint is the same before
# and after the run; a run that changed the tree exits 75 without a stamp. Gate output is kept in logs/gates/.
#
# Exit codes: 0 passed / verified, 1 verification failed, 64 usage, 75 tree changed during the run,
# otherwise the failing gate's own status.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

STAMP_DIR=".ralph/gates"
LOG_DIR="logs/gates"
CORE_GATES=(lint typecheck placeholders openapi specs test)

usage() {
  cat <<'USAGE'
usage: scripts/ralph/gates.sh [--ts] [--e2e] [--only gate[,gate...]]
       scripts/ralph/gates.sh --verify [--ts] [--e2e] [--only gate[,gate...]]
       scripts/ralph/gates.sh --fingerprint
gates: lint typecheck placeholders openapi specs test ts e2e
USAGE
}

gate_command() {
  case "$1" in
    lint | typecheck | placeholders | test | e2e) echo "make $1" ;;
    ts) echo "make test-ts" ;;
    openapi) echo "uv run python scripts/check_openapi_drift.py" ;;
    specs) echo "uv run python scripts/check_spec_refs.py" ;;
    *) return 1 ;;
  esac
}

mode="run"
want_ts=0
want_e2e=0
only=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) mode="verify" ;;
    --fingerprint) mode="fingerprint" ;;
    --ts) want_ts=1 ;;
    --e2e) want_e2e=1 ;;
    --only)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "--only needs a comma-separated gate list" >&2
        usage >&2
        exit 64
      fi
      only="$2"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
  shift
done

gates=()
if [[ -n "$only" ]]; then
  IFS=',' read -r -a gates <<<"$only"
  for name in "${gates[@]}"; do
    if ! gate_command "$name" >/dev/null; then
      echo "unknown gate: $name" >&2
      usage >&2
      exit 64
    fi
  done
else
  gates=("${CORE_GATES[@]}")
  if [[ "$want_ts" -eq 1 ]]; then gates+=(ts); fi
  if [[ "$want_e2e" -eq 1 ]]; then gates+=(e2e); fi
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
RESULTS="$WORK_DIR/results.tsv"

# Tree hash of the working tree as it is on disk. The index starts empty, so every file is hashed from its
# content and neither the real index nor its stat cache is read or changed.
fingerprint() {
  local index="$WORK_DIR/index"
  rm -f "$index"
  GIT_INDEX_FILE="$index" git add -A -- . ':(exclude).ralph'
  GIT_INDEX_FILE="$index" git write-tree
}

write_stamp() {
  local stamp="$1" tree="$2" head
  head="$(git rev-parse --verify -q HEAD || echo none)"
  python3 - "$stamp" "$tree" "$head" "$RESULTS" <<'PY'
import datetime
import json
import pathlib
import sys

stamp, tree, head, results = sys.argv[1:5]
path = pathlib.Path(stamp)
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"gates": {}}
data["tree"] = tree
data["head"] = head
data["written_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for line in pathlib.Path(results).read_text(encoding="utf-8", errors="replace").splitlines():
    name, code, seconds, summary = line.split("\t", 3)
    data["gates"][name] = {"exit": int(code), "seconds": int(seconds), "summary": summary}
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_gates() {
  local before after name status started seconds summary log failed=0
  local -a cmd
  mkdir -p "$STAMP_DIR" "$LOG_DIR"
  before="$(fingerprint)"
  : >"$RESULTS"
  for name in "${gates[@]}"; do
    read -r -a cmd <<<"$(gate_command "$name")"
    log="$LOG_DIR/$name.log"
    echo "▶ gate $name: ${cmd[*]}"
    started=$SECONDS
    set +e
    "${cmd[@]}" 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    seconds=$((SECONDS - started))
    summary="$(grep -v '^[[:space:]]*$' "$log" | tail -n 1 | tr -d '\t\r' | cut -c1-300 || true)"
    printf '%s\t%s\t%s\t%s\n' "$name" "$status" "$seconds" "$summary" >>"$RESULTS"
    if [[ "$status" -ne 0 ]]; then
      echo "✖ gate $name failed (exit $status)" >&2
      failed="$status"
      break
    fi
  done
  after="$(fingerprint)"
  if [[ "$before" != "$after" ]]; then
    echo "✖ the working tree changed during the run ($before -> $after); no stamp written" >&2
    if [[ "$failed" -ne 0 ]]; then exit "$failed"; fi
    exit 75
  fi
  write_stamp "$STAMP_DIR/$after.json" "$after"
  if [[ "$failed" -ne 0 ]]; then exit "$failed"; fi
  echo "✔ gates passed for tree $after: ${gates[*]}"
}

verify_gates() {
  local tree
  tree="$(fingerprint)"
  python3 - "$STAMP_DIR/$tree.json" "${gates[@]}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
required = sys.argv[2:]
if not path.exists():
    print(f"no stamp for this tree ({path.stem}): the gates have not run on exactly these files")
    sys.exit(1)
recorded = json.loads(path.read_text(encoding="utf-8")).get("gates", {})
missing = False
for name in required:
    gate = recorded.get(name)
    if gate is None:
        print(f"{name:<13} not run")
        missing = True
    elif gate["exit"] != 0:
        print(f"{name:<13} FAILED (exit {gate['exit']})  {gate['summary']}")
        missing = True
    else:
        print(f"{name:<13} pass {gate['seconds']:>6}s  {gate['summary']}")
if missing:
    sys.exit(1)
print(f"verified: every listed gate passed on tree {path.stem}")
PY
}

case "$mode" in
  fingerprint) fingerprint ;;
  verify) verify_gates ;;
  run) run_gates ;;
esac
