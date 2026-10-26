#!/usr/bin/env bash
# Stop every service of the local docker-compose stack in all profiles (specs/29 §2).
#
# Usage: scripts/dev_down.sh [-v|--volumes]
#   -v, --volumes  also remove the named volumes (wipes Postgres, Redis, ClickHouse, MinIO, Prometheus, Grafana data)
set -euo pipefail

usage() {
  echo "usage: scripts/dev_down.sh [-v|--volumes]" >&2
}

down_flags=(--remove-orphans)
for arg in "$@"; do
  case "$arg" in
    -v | --volumes) down_flags+=(--volumes) ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "dev_down: unknown argument: $arg" >&2
      usage
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

docker compose --profile '*' down "${down_flags[@]}"
