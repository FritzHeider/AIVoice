#!/usr/bin/env bash
# Start the local docker-compose stack and wait until every service is healthy (specs/29 §2).
#
# Usage: scripts/dev_up.sh [PROFILES]
#   PROFILES is a comma-separated list of compose profiles (default: $PROFILE, else "infra"),
#   for example `scripts/dev_up.sh infra,observability`.
set -euo pipefail

readonly COLLECTOR_HEALTH_URL="http://localhost:13133/"
readonly SIM_URL="http://localhost:9100"
readonly SIM_KEY_FILE=".sim/telnyx_public_key"
readonly COMPOSE_ENV_FILE=".env.compose"
readonly WAIT_TIMEOUT_S=240
# Services that run once and exit (they must not be passed to `up --wait`).
readonly ONE_SHOT_SERVICES=(minio-init)
# Application services (profiles apps/e2e). They start after the dependencies are healthy, the sim key is copied,
# the platform Telnyx resources are bootstrapped, and `migrate` has finished (specs/29 §2 Decision P05-T17a).
readonly APP_SERVICES=(api edge media worker webhooks events-sink dashboard)
# The one-shot schema job that must succeed before any application service starts.
readonly MIGRATE_SERVICE=migrate
# The edge as the containerized sim reaches it (the app's webhook URL registered by the bootstrap).
readonly COMPOSE_EDGE_PUBLIC_HOST="edge:8100"

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
# The e2e profile adds docker-compose.e2e.yml (e2e-only sim settings); the explicit file list is prepended to the
# profile flags, so every compose call below sees the same project.
if [[ ${#profile_flags[@]} -gt 0 && " ${profile_flags[*]} " == *" --profile e2e "* ]]; then
  profile_flags=(-f docker-compose.yml -f docker-compose.e2e.yml "${profile_flags[@]}")
fi
# Bash 3.2 (stock macOS) treats expanding an empty array as an unbound variable under `set -u`, so every array
# expanded below is either checked for emptiness first or guaranteed non-empty.
if [[ ${#profile_flags[@]} -eq 0 ]]; then
  echo "dev_up: no profile given in '$profiles_arg'" >&2
  exit 2
fi

# One-shot jobs exit when done, and `up --wait` reports an exited container as a failure, so the long-running
# services are started and awaited first and the jobs then run in the foreground. Application services wait for
# the migration job, which needs Postgres, so they form a second stage.
services=()
apps=()
jobs=()
migrate=false
while IFS= read -r service; do
  if [[ "$service" == "$MIGRATE_SERVICE" ]]; then
    migrate=true
  elif [[ " ${ONE_SHOT_SERVICES[*]} " == *" $service "* ]]; then
    jobs+=("$service")
  elif [[ " ${APP_SERVICES[*]} " == *" $service "* ]]; then
    apps+=("$service")
  else
    services+=("$service")
  fi
done < <(docker compose "${profile_flags[@]}" config --services)

if [[ ${#services[@]} -eq 0 && ${#apps[@]} -eq 0 ]]; then
  echo "dev_up: profile(s) '$profiles_arg' select no services" >&2
  exit 2
fi

# --build rebuilds the images of `build:` services (the sim) from the working tree; unchanged layers are cached.
if [[ ${#services[@]} -gt 0 ]]; then
  docker compose "${profile_flags[@]}" up -d --wait --build --wait-timeout "$WAIT_TIMEOUT_S" "${services[@]}"
fi
if [[ ${#jobs[@]} -gt 0 ]]; then
  for job in "${jobs[@]}"; do
    docker compose "${profile_flags[@]}" run --rm "$job"
  done
fi

if [[ " ${services[*]:-} " == *" otel-collector "* ]]; then
  curl -fsS --retry 30 --retry-delay 1 --retry-all-errors --max-time 5 "$COLLECTOR_HEALTH_URL" >/dev/null
  echo "dev_up: otel-collector ready on $COLLECTOR_HEALTH_URL"
fi

# The simulator generates a new Telnyx Ed25519 key on every start; copy it into .env and .sim/ for host services.
recreate_flags=()
if [[ " ${services[*]:-} " == *" sim "* ]]; then
  previous_key="$(cat "$SIM_KEY_FILE" 2>/dev/null || true)"
  uv run python scripts/sim_public_key.py --url "$SIM_URL" --env-file .env --key-file "$SIM_KEY_FILE"
  # Edge and media read the key once at startup: a restarted sim means restarting them too.
  if [[ "$(cat "$SIM_KEY_FILE" 2>/dev/null || true)" != "$previous_key" ]]; then
    recreate_flags=(--force-recreate)
  fi
  # FakeTelnyx forgets its resources on restart: recreate the platform app/profiles (ids into .env) before the
  # application containers read .env, with the webhook URL the sim can reach inside the compose network.
  if [[ ${#apps[@]} -gt 0 ]]; then
    EDGE_PUBLIC_HOST="$COMPOSE_EDGE_PUBLIC_HOST" \
      uv run python scripts/telnyx_bootstrap.py --env-file .env --write-env >/dev/null
    echo "dev_up: platform Telnyx resources bootstrapped in the simulator"
  fi
fi

# Compose reads `KEY=  # comment` as the comment text; the apps read .env through this rendered copy.
if [[ "$migrate" == true || ${#apps[@]} -gt 0 ]]; then
  uv run python scripts/compose_env.py --env-file .env --output "$COMPOSE_ENV_FILE"
fi
if [[ "$migrate" == true ]]; then
  docker compose "${profile_flags[@]}" run --rm --build "$MIGRATE_SERVICE"
fi
# --no-deps: the dependencies are already up (or the job already ran), and `up` must not re-run `migrate`.
if [[ ${#apps[@]} -gt 0 ]]; then
  docker compose "${profile_flags[@]}" up -d --wait --build --no-deps ${recreate_flags[@]+"${recreate_flags[@]}"} \
    --wait-timeout "$WAIT_TIMEOUT_S" "${apps[@]}"
fi

docker compose "${profile_flags[@]}" ps
