"""Seed the local database with a dev org, its owner, starting credit, and a live and a test API key.

``make seed`` runs this script (``make seed ARGS=--json`` for machine-readable output). It refuses to run
unless ``VA_ENV`` is ``local`` or ``test`` because it prints secrets. Every run is safe to repeat: the org,
owner, and credit are created once, and the two ``dev-seed`` keys are rolled so the printed keys always work.
The logic is ``va_api.dev_seed``; this module only parses arguments, opens the database, KMS provider, and
Redis, and prints. Logs go to stderr; stdout carries only the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from contextlib import AsyncExitStack

from pydantic import ValidationError

from va_api.auth.api_keys import drop_cached_keys
from va_api.dev_seed import (
    DEV_EMAIL,
    DevSeeder,
    SeedRefusedError,
    SeedResult,
    check_seed_environment,
    render_text,
)
from va_api.settings import ApiSettings
from va_common.auth import PasswordHasher
from va_common.crypto import Envelope, open_kms_provider
from va_common.errors import AppError
from va_common.logging import configure_logging
from va_common.redis import redis_client
from va_common.settings import BaseServiceSettings
from va_common.time import SystemClock
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database

EXIT_FAILED = 1
EXIT_REFUSED = 2


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a dev org, owner, credit, and API keys.")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    parser.add_argument("--email", default=DEV_EMAIL, help=f"owner email (default {DEV_EMAIL})")
    parser.add_argument(
        "--reset-password", action="store_true", help="set and print a new password for an existing owner"
    )
    return parser.parse_args(argv)


async def run_seed(
    settings: ApiSettings, *, email: str, reset_password: bool
) -> tuple[SeedResult, list[str]]:
    """Seed through ``settings``' database and KMS, then drop the revoked keys' cached lookups.

    Returns the result and the prefixes whose cache entry Redis failed to drop (they expire within 60 s).
    """
    configure_logging(settings, stream=sys.stderr)
    api_key_hasher = settings.api_key_hasher()
    # Building the hasher computes its dummy argon2 hash; keep that CPU work off the event loop.
    password_hasher = await asyncio.to_thread(PasswordHasher)
    async with open_kms_provider(settings) as kms_provider, AsyncExitStack() as stack:
        database = Database.from_settings(settings)
        stack.push_async_callback(database.dispose)
        envelope = Envelope(kms_provider, OrgDataKeyStore(database))
        seeder = DevSeeder(
            database=database,
            provisioner=OrgProvisioner(database, envelope),
            api_key_hasher=api_key_hasher,
            password_hasher=password_hasher,
            clock=SystemClock(),
        )
        result = await seeder.seed(email=email, reset_password=reset_password)
        redis = redis_client(settings)
        stack.push_async_callback(redis.aclose)
        failed = await drop_cached_keys(redis, result.revoked_prefixes)
    return result, failed


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # Only VA_ENV first: the full api settings refuse some local-only values outside local/test.
        check_seed_environment(BaseServiceSettings().va_env)
        settings = ApiSettings()
        result, failed = asyncio.run(run_seed(settings, email=args.email, reset_password=args.reset_password))
    except SeedRefusedError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except (AppError, RuntimeError, ValidationError) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if failed:
        print(
            f"warning: could not drop cached keys {', '.join(failed)}; they stop working within 60 s",
            file=sys.stderr,
        )
    print(json.dumps(result.to_json(), indent=2) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
