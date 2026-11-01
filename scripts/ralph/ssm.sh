#!/usr/bin/env bash
# Run a bash script on the EC2 Ralph runner through AWS Systems Manager and print its output.
#
# Usage:
#   scripts/ralph/ssm.sh "<comment>" <<'EOF'
#   cd /home/ssm-user/AIVOICE && sudo -u ralph tmux ls
#   EOF
#   echo 'uptime' | scripts/ralph/ssm.sh "<comment>"
#
# The script on stdin travels base64-encoded inside an AWS-RunShellScript command, so quoting
# survives intact; it runs as root on the runner (use `sudo -u ralph` for repo and tmux work).
# The comment shows up in the SSM command history (truncated to 100 characters, the API limit).
#
# Environment (defaults in brackets):
#   AWS_PROFILE        CLI profile [aivoice]
#   AWS_REGION         runner region [us-east-2]
#   RALPH_INSTANCE_ID  runner instance id [the one running instance tagged Name=AivoiceRalphRunner/Runner]
#   SSM_TIMEOUT_S      seconds to wait for the command [600]
#
# Prints "[<status>]", then stdout, then stderr lines prefixed "ERR: ". SSM returns at most
# 24 000 characters of each stream, so trim large outputs on the runner side. Exits 0 only when
# the command status is Success.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 \"<comment>\" < script" >&2
  exit 64
fi
COMMENT="${1:0:100}"

export AWS_PROFILE="${AWS_PROFILE:-aivoice}"
export AWS_REGION="${AWS_REGION:-us-east-2}"
TIMEOUT_S="${SSM_TIMEOUT_S:-600}"

INSTANCE="${RALPH_INSTANCE_ID:-}"
if [[ -z "$INSTANCE" ]]; then
  INSTANCE="$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=AivoiceRalphRunner/Runner" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceId' --output text)"
  if [[ -z "$INSTANCE" || "$INSTANCE" == *[[:space:]]* ]]; then
    echo "expected exactly one running AivoiceRalphRunner/Runner instance, found: '${INSTANCE}'" >&2
    echo "set RALPH_INSTANCE_ID to choose one" >&2
    exit 69
  fi
fi

SCRIPT_B64="$(base64 | tr -d '\n')"
REMOTE="/tmp/ralph_ssm_\$\$.sh"
PARAMS="$(printf '{"commands":["echo %s | base64 -d > %s && bash %s; rc=$?; rm -f %s; exit $rc"],"executionTimeout":["%s"]}' \
  "$SCRIPT_B64" "$REMOTE" "$REMOTE" "$REMOTE" "$TIMEOUT_S")"

COMMAND_ID="$(aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
  --comment "$COMMENT" --parameters "$PARAMS" --query Command.CommandId --output text)"

invocation() {
  aws ssm get-command-invocation --command-id "$COMMAND_ID" --instance-id "$INSTANCE" --query "$1" --output text
}

STATUS="Pending"
DEADLINE=$((SECONDS + TIMEOUT_S + 60))
while ((SECONDS < DEADLINE)); do
  STATUS="$(invocation Status 2>/dev/null || echo Pending)"
  case "$STATUS" in
    Success | Failed | TimedOut | Cancelled) break ;;
  esac
  sleep 2
done

echo "[$STATUS]"
invocation StandardOutputContent
invocation StandardErrorContent | sed 's/^/ERR: /'
[[ "$STATUS" == "Success" ]]
