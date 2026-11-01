"""`infra/docker/python.Dockerfile` and `.dockerignore` match specs/29 §1.1 and its Decision (P03-T15b).

The Dockerfile must equal the spec block except the three documented lines (uv image, distribution name from
the app's pyproject, the runtime `.sim` directory). The package-name pipeline is run for every Python app.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra" / "docker" / "python.Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
SPEC_29 = ROOT / "specs" / "29-infrastructure.md"

SPEC_UV = "COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /usr/local/bin/uv"
REPO_UV = "COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv"
SPEC_SYNC = '    uv sync --frozen --no-dev --package "va-${SERVICE//_/-}" --no-editable'
REPO_SYNC = [
    '    package="$(sed -n \'s/^name = "\\(.*\\)"$/\\1/p\' "apps/${SERVICE}/pyproject.toml" | head -n 1)" \\',
    '    && test -n "$package" \\',
    '    && uv sync --frozen --no-dev --package "$package" --no-editable',
]
ASSETS_COPY = "COPY assets/ /app/assets/"
SIM_DIR = "RUN install -d -o 10001 -g 10001 /app/.sim"
CONTEXT_PATHS = ("pyproject.toml", "uv.lock", "packages", "apps", "models", "assets")
EXCLUDED = (".git", ".venv", ".env", ".sim", "**/node_modules", "**/__pycache__")


def _spec_dockerfile() -> list[str]:
    text = SPEC_29.read_text(encoding="utf-8")
    start = text.index("### 1.1 `infra/docker/python.Dockerfile`")
    match = re.search(r"```dockerfile\n(.*?)```", text[start:], re.DOTALL)
    assert match is not None
    return match.group(1).splitlines()


def _expected_dockerfile() -> list[str]:
    expected: list[str] = []
    for line in _spec_dockerfile():
        if line == SPEC_UV:
            expected.append(REPO_UV)
        elif line == SPEC_SYNC:
            expected.extend(REPO_SYNC)
        else:
            expected.append(line)
            if line == ASSETS_COPY:
                expected.append(SIM_DIR)
    return expected


def _python_apps() -> list[Path]:
    return sorted(path.parent for path in (ROOT / "apps").glob("*/pyproject.toml"))


def _package_pipeline() -> str:
    """The shell pipeline inside ``package="$(...)"`` of the Dockerfile's sync step."""
    match = re.search(r'package="\$\((.*)\)" \\$', DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_spec_block_contains_every_replaced_line() -> None:
    spec = _spec_dockerfile()
    assert SPEC_UV in spec
    assert SPEC_SYNC in spec
    assert ASSETS_COPY in spec


def test_dockerfile_equals_spec_with_documented_deviations() -> None:
    assert DOCKERFILE.read_text(encoding="utf-8").splitlines() == _expected_dockerfile()


def test_lock_revision_is_the_one_the_pinned_uv_reads() -> None:
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert re.search(r"^revision = 5$", lock, re.MULTILINE), "lock revision changed; re-check the uv pin"


BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash not installed")
@pytest.mark.parametrize("app", _python_apps(), ids=lambda path: path.name)
def test_package_pipeline_yields_the_app_distribution(app: Path) -> None:
    assert BASH is not None
    project = tomllib.loads((app / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    result = subprocess.run(
        [BASH, "-o", "pipefail", "-c", _package_pipeline()],
        cwd=ROOT,
        env={**os.environ, "SERVICE": app.name},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == project["name"]


def test_every_app_distribution_is_a_locked_workspace_member() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    editable = {pkg["name"] for pkg in lock["package"] if "editable" in pkg.get("source", {})}
    for app in _python_apps():
        name = tomllib.loads((app / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
        assert name in editable, name


def _dockerignore_patterns() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_dockerignore_excludes_local_state_and_secrets() -> None:
    patterns = _dockerignore_patterns()
    for pattern in EXCLUDED:
        assert pattern in patterns, pattern


def test_dockerignore_keeps_the_build_inputs() -> None:
    patterns = [p for p in _dockerignore_patterns() if not p.startswith("!")]
    for path in CONTEXT_PATHS:
        for pattern in patterns:
            assert not (path == pattern or path.startswith(f"{pattern}/")), f"{pattern} drops {path}"
    for app in _python_apps():
        relative = app.relative_to(ROOT).as_posix()
        assert not [p for p in patterns if relative.startswith(p)], relative
