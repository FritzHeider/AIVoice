#!/usr/bin/env bash
# Start the local docker-compose stack and wait until every service is healthy (specs/29 §2).
#
# Usage: scripts/dev_up.sh [PROFILES]
#   PROFILES is a comma-separated list of compose profiles (default: $PROFILE, else "infra"),
#   for example `scripts/dev_up.sh infra,observability`.
set -euo pipefail

readonly COLLECTOR_HEALTH_URL="http://localhost:13133/"
readonly WAIT_TIMEOUT_S=240
# Services that run once and exit (they must not be passed to `up --wait`).
readonly ONE_SHOT_SERVICES=(minio-init)

usage() {
  echo "usage: scripts/dev_up.sh [PROFILES]  (comma-separated, default infra)" >&2
}

if [[ $# -gt 1 ]]; then
  usage
  exit 2
fi
case "${1:-}" in
  -h | --help)
    usage
    exit 0
    ;;
esac

profiles_arg="${1:-${PROFILE:-}}"
profiles_arg="${profiles_arg:-infra}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! docker info >/dev/null 2>&1; then
  echo "dev_up: the Docker daemon is not reachable; start Docker (or OrbStack: 'orb start') and retry" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "dev_up: created .env from .env.example"
fi

profile_flags=()
IFS=',' read -r -a profiles <<<"$profiles_arg"
for profile in "${profiles[@]}"; do
  if [[ -n "$profile" ]]; then
    profile_flags+=(--profile "$profile")
  fi
done
# Bash 3.2 (stock macOS) treats expanding an empty array as an unbound variable under `set -u`, so every array
# expanded below is either checked for emptiness first or guaranteed non-empty.
if [[ ${#profile_flags[@]} -eq 0 ]]; then
  echo "dev_up: no profile given in '$profiles_arg'" >&2
  exit 2
fi

# One-shot jobs exit when done, and `up --wait` reports an exited container as a failure, so the long-running
# services are started and awaited first and the jobs then run in the foreground.
services=()
jobs=()
while IFS= read -r service; do
  if [[ " ${ONE_SHOT_SERVICES[*]} " == *" $service "* ]]; then
    jobs+=("$service")
  else
    services+=("$service")
  fi
done < <(docker compose "${profile_flags[@]}" config --services)

if [[ ${#services[@]} -eq 0 ]]; then
  echo "dev_up: profile(s) '$profiles_arg' select no services" >&2
  exit 2
fi

docker compose "${profile_flags[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT_S" "${services[@]}"
if [[ ${#jobs[@]} -gt 0 ]]; then
  for job in "${jobs[@]}"; do
    docker compose "${profile_flags[@]}" run --rm "$job"
  done
fi

if [[ " ${services[*]} " == *" otel-collector "* ]]; then
  curl -fsS --retry 30 --retry-delay 1 --retry-all-errors --max-time 5 "$COLLECTOR_HEALTH_URL" >/dev/null
  echo "dev_up: otel-collector ready on $COLLECTOR_HEALTH_URL"
fi

docker compose "${profile_flags[@]}" ps
