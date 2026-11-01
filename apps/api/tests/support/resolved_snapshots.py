"""Resolved `SendCallConfig` snapshots of the Bland Send Call fixtures (specs/05 §4.3, P05-T03b).

Each fixture in `fixtures/bland_send_call/` is resolved with fixed runner inputs, references and clock, and
compared with `fixtures/bland_send_call_resolved/<name>`. After a deliberate resolution change regenerate
with `PYTHONPATH=packages/va_domain/tests/support uv run python apps/api/tests/support/resolved_snapshots.py
--write` (then prettier the JSON).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from resolution_helpers import AGENT_VERSION_ID, FROM_NUMBER, NOW, make_refs

from va_domain.call_resolution import (
    AgentRunner,
    PathwayRef,
    PathwayRunner,
    PersonaRunner,
    RunnerInputs,
    TaskRunner,
    resolve_call_config,
)
from va_domain.calls import SendCallRequest
from va_domain.org import load_settings
from va_domain.phone import normalize_e164

FIXTURES: Final = Path(__file__).resolve().parent.parent / "fixtures"
REQUESTS: Final = FIXTURES / "bland_send_call"
SNAPSHOTS: Final = FIXTURES / "bland_send_call_resolved"
ORG_SETTINGS: Final[Mapping[str, Any]] = {
    "timezone": "America/Chicago",
    "dispositions_default": ["CALLBACK_REQUESTED"],
    "call_defaults": {"max_tokens": 300, "voicemail_detect_timeout_ms": 10_000},
}


def _runner(name: str, request: SendCallRequest) -> RunnerInputs:
    if name == "02_pathway_runner.json" and request.pathway_id is not None:
        defaults = {"max_duration": 10, "voicemail": {"message": "Please call Acme back."}}
        ref = PathwayRef(request.pathway_id, 4, defaults, "One moment please.", "start")
        return PathwayRunner(ref)
    if name == "03_persona_settings.json" and request.persona_id is not None:
        config = {
            "name": "Avery",
            "runner": "prompt",
            "prompt": "You are Avery from Acme. Help the customer renew their plan.",
            "first_sentence": "Hi, this is Avery from Acme.",
            "language": "babel-en",
            "call_settings": {"record": True, "max_duration": 8},
            "metadata": {"persona": "avery"},
        }
        return PersonaRunner(request.persona_id, 7, False, config)
    if name == "04_agent_runner.json" and request.agent_id is not None:
        settings = {"task": "Greet {{first_name}} and book a demo.", "max_tokens": 200, "record": True}
        return AgentRunner(request.agent_id, AGENT_VERSION_ID, settings)
    return TaskRunner()


def resolved(path: Path) -> dict[str, Any]:
    """The JSON snapshot of the resolved config and warnings for one request fixture."""
    request = SendCallRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    refs = make_refs(
        phone_number=normalize_e164(request.phone_number, request.country),
        from_number=request.from_ or FROM_NUMBER,
    )
    resolution = resolve_call_config(
        load_settings(ORG_SETTINGS), request, _runner(path.name, request), refs, NOW
    )
    return {
        "config": resolution.config.model_dump(mode="json", by_alias=True),
        "warnings": [
            {"code": warning.code, "message": warning.message, "field": warning.field}
            for warning in resolution.warnings
        ],
    }


def request_paths() -> list[Path]:
    return sorted(REQUESTS.glob("*.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the snapshot files")
    args = parser.parse_args(argv)
    stale = []
    for path in request_paths():
        target = SNAPSHOTS / path.name
        snapshot = resolved(path)
        current = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
        if current == snapshot:
            continue
        stale.append(path.name)
        if args.write:
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for name in stale:
        sys.stdout.write(f"{'wrote' if args.write else 'stale'} {name}\n")
    return 0 if args.write or not stale else 1


if __name__ == "__main__":
    raise SystemExit(main())
