"""Probe the Docker daemon for ``@pytest.mark.integration`` tests without the docker SDK.

``docker.from_env()`` leaks a socket when the daemon is down, and ``filterwarnings=error`` turns the resulting
``ResourceWarning`` into a session error, so integration fixtures call :func:`docker_available` first.
"""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlsplit

from testcontainers.core.docker_client import get_docker_host

__all__ = ["docker_available"]

_DEFAULT_SOCKETS = ("/var/run/docker.sock", str(Path.home() / ".docker/run/docker.sock"))


def _reachable(address: str) -> bool:
    """Connect to the Docker daemon endpoint ``address`` (``tcp://`` or ``unix://``)."""
    parts = urlsplit(address)
    try:
        if parts.scheme == "tcp":
            with socket.create_connection((parts.hostname or "localhost", parts.port or 2375), timeout=1):
                return True
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(parts.path or address)
            return True
    except OSError:
        return False


def docker_available() -> bool:
    """Whether the daemon testcontainers will use accepts connections.

    Resolution follows testcontainers: ``~/.testcontainers.properties``, ``DOCKER_HOST``, then the active
    docker context (OrbStack, Colima, Docker Desktop); without any of them the default sockets are tried.
    """
    host = get_docker_host()
    candidates = [host] if host else [f"unix://{path}" for path in _DEFAULT_SOCKETS]
    return any(_reachable(candidate) for candidate in candidates)
