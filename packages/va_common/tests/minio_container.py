"""A throwaway MinIO container for ``@pytest.mark.integration`` tests (skips when Docker is unavailable).

The image is the one docker-compose uses (``bitnamilegacy/minio``: the ``minio/minio`` images are gone). Each
test module that needs object storage boots one container (xdist workers boot their own).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import aioboto3
import pytest
from docker_probe import docker_available
from pydantic import SecretStr
from service_helpers import settings_of

from va_common.storage import S3Settings

__all__ = ["MINIO_IMAGE", "Minio", "make_bucket", "running_minio"]

MINIO_IMAGE = "bitnamilegacy/minio:2024.10.13"
_PORT = 9000


@dataclass(frozen=True, slots=True)
class Minio:
    endpoint_url: str
    access_key: str
    secret_key: str

    def settings(self, **overrides: object) -> S3Settings:
        """``S3Settings`` pointing at this server (path-style, static credentials)."""
        values: dict[str, object] = {
            "s3_endpoint_url": self.endpoint_url,
            "s3_access_key_id": self.access_key,
            "s3_secret_access_key": SecretStr(self.secret_key),
            **overrides,
        }
        return settings_of(S3Settings, **values)


@contextmanager
def running_minio() -> Iterator[Minio]:
    """Yield a ready MinIO server with random root credentials (skips without Docker)."""
    if not docker_available():
        pytest.skip("docker unavailable")
    from testcontainers.core.container import DockerContainer  # noqa: PLC0415 - imported only when Docker exists
    from testcontainers.core.wait_strategies import HttpWaitStrategy  # noqa: PLC0415

    access_key = f"va{secrets.token_hex(6)}"
    secret_key = secrets.token_hex(16)
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", access_key)
        .with_env("MINIO_ROOT_PASSWORD", secret_key)
        .with_exposed_ports(_PORT)
        .waiting_for(HttpWaitStrategy(_PORT, "/minio/health/ready").for_status_code(200))
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(_PORT)
        yield Minio(f"http://{host}:{port}", access_key, secret_key)


async def make_bucket(minio: Minio, bucket: str) -> None:
    """Create ``bucket`` on ``minio``."""
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=minio.endpoint_url,
        region_name="us-east-1",
        aws_access_key_id=minio.access_key,
        aws_secret_access_key=minio.secret_key,
    ) as client:
        await client.create_bucket(Bucket=bucket)
