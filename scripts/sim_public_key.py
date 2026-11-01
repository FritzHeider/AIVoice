"""Copy the running FakeTelnyx's webhook public key into the host env (specs/29 §2 Decision P03-T15b).

``uv run python scripts/sim_public_key.py [--url http://localhost:9100] [--env-file .env]
[--key-file .sim/telnyx_public_key] [--attempts 30]`` reads ``GET /sim/telnyx/public-key`` (retrying while
the simulator is still starting), sets ``TELNYX_PUBLIC_KEY`` in the env file (keeping its comment), and writes
the key file the host services read in ``local``. ``scripts/dev_up.sh`` runs it after every ``make up`` that
starts the ``sim`` service, because the simulator generates a new Ed25519 key on each start unless
``SIM_ED25519_PRIVATE_KEY`` is set.
Exit codes: 0 written, 1 simulator unreachable or answered without a valid key.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

import httpx

from env_file import update_env_file
from va_common.http import http_client
from va_common.settings import BaseServiceSettings

EXIT_FAILED: Final = 1
ENV_KEY: Final = "TELNYX_PUBLIC_KEY"
PUBLIC_KEY_PATH: Final = "/sim/telnyx/public-key"
ED25519_PUBLIC_KEY_BYTES: Final = 32
DEFAULT_URL: Final = "http://localhost:9100"
DEFAULT_ENV_FILE: Final = Path(".env")
DEFAULT_KEY_FILE: Final = Path(".sim/telnyx_public_key")
HTTP_TIMEOUT_S: Final = 5.0
ATTEMPTS: Final = 30
RETRY_DELAY_S: Final = 1.0

Sleep = Callable[[float], Awaitable[None]]


class SimKeySettings(BaseServiceSettings):
    """Only the shared service settings (user agent, environment); the script has no variables of its own."""

    service_name: ClassVar[str] = "sim-public-key"


class SimKeyError(Exception):
    """The simulator could not be reached or did not answer with a valid Ed25519 public key."""


def parse_public_key(body: object) -> str:
    """The base64 key of a ``{"public_key": "<base64 of 32 bytes>"}`` body, else :class:`SimKeyError`."""
    key = body.get("public_key") if isinstance(body, dict) else None
    if not isinstance(key, str):
        msg = "the response has no public_key string"
        raise SimKeyError(msg)
    try:
        raw = base64.b64decode(key, validate=True)
    except binascii.Error as exc:
        msg = "public_key is not valid base64"
        raise SimKeyError(msg) from exc
    if len(raw) != ED25519_PUBLIC_KEY_BYTES:
        msg = f"public_key decodes to {len(raw)} bytes, expected {ED25519_PUBLIC_KEY_BYTES}"
        raise SimKeyError(msg)
    return key


async def fetch_public_key(
    http: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = ATTEMPTS,
    delay_s: float = RETRY_DELAY_S,
    sleep: Sleep = asyncio.sleep,
) -> str:
    """GET the key from the simulator at ``url``; transport errors are retried ``attempts`` times in total."""
    endpoint = f"{url.rstrip('/')}{PUBLIC_KEY_PATH}"
    for attempt in range(1, attempts + 1):
        try:
            response = await http.get(endpoint)
        except httpx.TransportError as exc:
            if attempt == attempts:
                msg = f"{endpoint} unreachable after {attempts} attempts ({type(exc).__name__})"
                raise SimKeyError(msg) from exc
            await sleep(delay_s)
            continue
        if response.status_code != httpx.codes.OK:
            msg = f"{endpoint} answered HTTP {response.status_code}"
            raise SimKeyError(msg)
        try:
            body: object = response.json()
        except ValueError as exc:
            msg = f"{endpoint} answered a non-JSON body"
            raise SimKeyError(msg) from exc
        return parse_public_key(body)
    msg = "attempts must be at least 1"
    raise SimKeyError(msg)


def write_key_file(path: Path, key: str) -> None:
    """Write ``key`` plus a newline to ``path`` (the format the simulator itself writes), creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{key}\n", encoding="utf-8")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy the FakeTelnyx public key into the env file.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"FakeTelnyx base URL (default {DEFAULT_URL})")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help=f"default {DEFAULT_ENV_FILE}")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE, help=f"default {DEFAULT_KEY_FILE}")
    parser.add_argument(
        "--attempts",
        type=int,
        default=ATTEMPTS,
        help=f"connection attempts, {RETRY_DELAY_S:g} s apart (default {ATTEMPTS})",
    )
    return parser.parse_args(argv)


async def _fetch(url: str, attempts: int) -> str:
    kwargs: dict[str, Any] = {"_env_file": None}
    async with http_client(SimKeySettings(**kwargs), timeout=HTTP_TIMEOUT_S) as http:
        return await fetch_public_key(http, url, attempts=attempts)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        key = asyncio.run(_fetch(args.url, args.attempts))
    except SimKeyError as exc:
        print(f"sim_public_key: {exc}", file=sys.stderr)
        return EXIT_FAILED
    written = update_env_file(args.env_file, {ENV_KEY: key})
    write_key_file(args.key_file, key)
    state = "updated" if written else "unchanged"
    print(f"sim_public_key: {ENV_KEY} {state} in {args.env_file}; key file {args.key_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
