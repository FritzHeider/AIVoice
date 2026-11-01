"""Create or repair the platform Telnyx resources and print their ids (specs/06-telephony-telnyx.md §12).

``uv run python scripts/telnyx_bootstrap.py --env-file .env [--write-env] [--json]`` finds or creates the
outbound voice profile, the Call Control Application, and the messaging profile named ``voiceagents-{VA_ENV}``
(against FakeTelnyx by default, real Telnyx when ``TELNYX_API_BASE`` points there) and patches drifted
webhooks. Every run is safe to repeat. stdout carries only the result (three ``KEY=value`` lines, or one JSON
object); logs go to stderr. ``--write-env`` updates the three ``TELNYX_*_ID`` keys of the env file in place.
The logic is :func:`va_telnyx.bootstrap.bootstrap_account`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx
from pydantic import ValidationError
from redis.asyncio import Redis

from va_common.http import http_client
from va_common.logging import configure_logging, get_logger
from va_common.redis import redis_client
from va_common.settings import RedisSettings
from va_telnyx import (
    BootstrapResult,
    TelnyxApiError,
    TelnyxClient,
    TelnyxError,
    TelnyxRateLimiter,
    bootstrap_account,
    platform_resource_name,
    webhook_urls,
)

EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
DEFAULT_ENV_FILE: Final = Path(".env")
HTTP_TIMEOUT_S: Final = 30.0

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*?)(?P<comment>\s+#.*)?$")

log = get_logger("telnyx_bootstrap")


class BootstrapSettings(RedisSettings):
    """The specs/02 §6 variables the bootstrap reads (defaults point at the local simulator)."""

    service_name: ClassVar[str] = "telnyx-bootstrap"
    telnyx_api_base: str = "http://localhost:9100/v2"
    telnyx_api_key: str = "sim-telnyx-key"
    edge_public_host: str = "localhost:8100"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or repair the platform Telnyx resources.")
    parser.add_argument(
        "--env-file", type=Path, default=None, help=f"settings file (default {DEFAULT_ENV_FILE}, if present)"
    )
    parser.add_argument(
        "--write-env", action="store_true", help="update the TELNYX_*_ID keys in the env file"
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of KEY=value lines"
    )
    return parser.parse_args(argv)


def load_settings(env_file: Path | None) -> BootstrapSettings:
    """Settings from the environment plus ``env_file`` (environment variables win); no file when ``None``."""
    kwargs: dict[str, Any] = {"_env_file": env_file}
    return BootstrapSettings(**kwargs)


async def run_bootstrap(
    settings: BootstrapSettings, *, http: httpx.AsyncClient, redis: Redis
) -> BootstrapResult:
    """Bootstrap the platform account through ``http``; the rate limiter fails open when Redis is down."""
    client = TelnyxClient(settings.telnyx_api_key, settings.telnyx_api_base, http, TelnyxRateLimiter(redis))
    return await bootstrap_account(
        client,
        name=platform_resource_name(settings.va_env.value),
        webhooks=webhook_urls(settings.edge_public_host),
    )


def render_text(result: BootstrapResult) -> str:
    return "\n".join(f"{key}={value}" for key, value in result.env_values().items())


def render_json(result: BootstrapResult) -> str:
    body = {**result.env_values(), "created": list(result.created), "updated": list(result.updated)}
    return json.dumps(body, indent=2)


def _replace_values(lines: list[str], values: Mapping[str, str]) -> set[str]:
    """Rewrite matching ``KEY=...`` lines in place (keeping comments and line endings); the keys seen."""
    seen: set[str] = set()
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = _ASSIGNMENT.match(body)
        if match is None or match["key"] not in values:
            continue
        key = match["key"]
        seen.add(key)
        lines[index] = f"{key}={values[key]}{match['comment'] or ''}{line[len(body) :]}"
    return seen


def update_env_file(path: Path, values: Mapping[str, str]) -> bool:
    """Set ``values`` in the env file at ``path`` (created when absent); ``True`` when the file was written.

    Existing assignments keep their trailing ``  # comment``; missing keys are appended; every other line is
    kept byte for byte. The new content replaces the file atomically; an unchanged file is not touched.
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines(keepends=True)
    seen = _replace_values(lines, values)
    missing = [f"{key}={value}\n" for key, value in values.items() if key not in seen]
    if missing and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    updated = "".join(lines + missing)
    if updated == original:
        return False
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        if path.exists():
            Path(temp_name).chmod(path.stat().st_mode & 0o777)
        Path(temp_name).replace(path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return True


async def _bootstrap(settings: BootstrapSettings) -> BootstrapResult:
    async with http_client(settings, timeout=HTTP_TIMEOUT_S) as http:
        redis = redis_client(settings)
        try:
            return await run_bootstrap(settings, http=http, redis=redis)
        finally:
            await redis.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    env_file: Path | None = args.env_file
    if env_file is not None and not env_file.is_file():
        print(f"env file not found: {env_file}", file=sys.stderr)
        return EXIT_USAGE
    if env_file is None and DEFAULT_ENV_FILE.is_file():
        env_file = DEFAULT_ENV_FILE
    try:
        settings = load_settings(env_file)
    except ValidationError as exc:
        print(f"invalid settings: {exc}", file=sys.stderr)
        return EXIT_USAGE
    configure_logging(settings, stream=sys.stderr)
    try:
        result = asyncio.run(_bootstrap(settings))
    except TelnyxError as exc:
        status = exc.status if isinstance(exc, TelnyxApiError) else None
        codes = [item.code for item in exc.errors] if isinstance(exc, TelnyxApiError) else []
        log.error(  # noqa: TRY400 - a traceback adds nothing to the status and Telnyx codes
            "telnyx_bootstrap.failed",
            error=type(exc).__name__,
            operation=exc.operation,
            status=status,
            codes=codes,
        )
        return EXIT_FAILED
    if args.write_env:
        target = env_file if env_file is not None else DEFAULT_ENV_FILE
        written = update_env_file(target, result.env_values())
        log.info("telnyx_bootstrap.env_file", path=str(target), written=written)
    print(render_json(result) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
