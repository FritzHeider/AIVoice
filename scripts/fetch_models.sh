#!/usr/bin/env bash
# Download and checksum-verify the ML models (specs/07 §5.3). The logic lives in scripts/fetch_models.py;
# pass --check to verify without downloading or --update to re-pin. Requires `uv sync` first.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --no-sync python scripts/fetch_models.py "$@"
