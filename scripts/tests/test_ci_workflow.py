"""The CI workflow and pre-commit hooks match specs/29 §6.1 and specs/28 T17.

CI service containers must also agree with docker-compose.yml and job env with `.env.example`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import env_spec as es

ROOT = Path(__file__).resolve().parents[2]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE_PATH = ROOT / "docker-compose.yml"
PRECOMMIT_PATH = ROOT / ".pre-commit-config.yaml"
GITLEAKS_PATH = ROOT / ".gitleaks.toml"
GITIGNORE_PATH = ROOT / ".gitignore"
AGENTS_MD = ROOT / "AGENTS.md"
SPEC_29 = ROOT / "specs" / "29-infrastructure.md"

SERVICES = ("postgres", "redis", "clickhouse", "minio")
# Job env keys that configure tooling rather than the platform, so they are not in `.env.example`.
TOOLING_ENV = {"PYTEST_ADDOPTS", "GITLEAKS_VERSION"}
REQUIRED_HOOKS = {"ruff", "ruff-format", "check_no_placeholders", "prettier", "actionlint", "gitleaks"}

Mapping = dict[Any, Any]


def _load_yaml(path: Path) -> Mapping:
    return cast("Mapping", yaml.safe_load(path.read_text(encoding="utf-8")))


CI = _load_yaml(CI_PATH)
JOBS = cast("dict[str, Mapping]", CI["jobs"])
COMPOSE_SERVICES = cast("dict[str, Mapping]", _load_yaml(COMPOSE_PATH)["services"])
PRECOMMIT = _load_yaml(PRECOMMIT_PATH)


def _runs(job: str) -> str:
    return "\n".join(str(step.get("run", "")) for step in JOBS[job]["steps"])


def _uses(job: str) -> list[str]:
    return [str(step["uses"]) for step in JOBS[job]["steps"] if "uses" in step]


def _actionlint() -> str:
    # The binary ships with the `actionlint-py` dev dependency next to the interpreter.
    found = shutil.which("actionlint", path=str(Path(sys.executable).parent)) or shutil.which("actionlint")
    assert found is not None, "actionlint missing: run `uv sync --all-packages`"
    return found


def _lint(*paths: Path) -> subprocess.CompletedProcess[str]:
    cmd = [_actionlint(), "-no-color", *map(str, paths)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=ROOT, timeout=60)


def test_actionlint_passes() -> None:
    result = _lint(CI_PATH)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_actionlint_detects_broken_workflow(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yml"
    broken.write_text(
        "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    needs: [missing]\n"
        "    steps:\n      - run: echo ${{ github.nope.value }}\n",
        encoding="utf-8",
    )
    result = _lint(broken)
    assert result.returncode != 0
    assert "missing" in result.stdout


def test_triggers_push_main_and_pull_request() -> None:
    # YAML 1.1 parses the bare key `on` as the boolean True.
    triggers = cast("Mapping", CI[True])
    assert triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in triggers


def test_permissions_read_only() -> None:
    assert CI["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in JOBS.values())


def test_concurrency_cancels_superseded_runs() -> None:
    assert CI["concurrency"]["cancel-in-progress"] is True
    assert "github.ref" in CI["concurrency"]["group"]


def test_expected_jobs() -> None:
    assert set(JOBS) == {"check", "ts-build", "security", "actionlint"}


def test_every_job_has_timeout() -> None:
    for name, job in JOBS.items():
        assert isinstance(job.get("timeout-minutes"), int), name


def test_actions_pinned_to_versions() -> None:
    for name in JOBS:
        for uses in _uses(name):
            action, _, ref = uses.partition("@")
            assert action and ref, uses
            assert ref not in {"main", "master", "latest"}, uses


def test_check_job_runs_make_check_with_frozen_installs() -> None:
    runs = _runs("check")
    assert "make check" in runs
    assert "uv sync --all-packages --frozen" in runs
    assert "pnpm install --frozen-lockfile" in runs


def test_check_job_applies_compose_init_sql() -> None:
    sql = "infra/docker/postgres-init/01-roles.sql"
    assert sql in _runs("check")
    assert (ROOT / sql).is_file()


def test_check_job_uploads_reports_always() -> None:
    upload = next(
        s for s in JOBS["check"]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact")
    )
    assert upload["if"] == "always()"
    assert "reports/" in JOBS["check"]["env"]["PYTEST_ADDOPTS"]
    assert "/reports/" in GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("service", SERVICES)
def test_service_image_and_healthcheck_match_compose(service: str) -> None:
    ci_service = JOBS["check"]["services"][service]
    compose = COMPOSE_SERVICES[service]
    assert ci_service["image"] == compose["image"]
    assert "--health-cmd" in ci_service["options"]
    assert set(ci_service["ports"]) <= set(compose["ports"])


@pytest.mark.parametrize("service", SERVICES)
def test_service_env_matches_compose(service: str) -> None:
    ci_env = JOBS["check"]["services"][service].get("env", {})
    assert ci_env == COMPOSE_SERVICES[service].get("environment", {})


def test_clickhouse_service_raises_nofile_limit_like_compose() -> None:
    limit = COMPOSE_SERVICES["clickhouse"]["ulimits"]["nofile"]
    assert (
        f"--ulimit nofile={limit['soft']}:{limit['hard']}"
        in JOBS["check"]["services"]["clickhouse"]["options"]
    )


def test_job_env_names_and_values_match_env_example() -> None:
    entries = {entry.name: entry.value for entry in es.parse_env_example(es.load_env_example())}
    platform_env = {k: v for k, v in JOBS["check"]["env"].items() if k not in TOOLING_ENV}
    assert platform_env
    for name, value in platform_env.items():
        assert name in entries, name
        assert value == entries[name], name


def test_ts_build_job_builds_and_checks_generated_types() -> None:
    runs = _runs("ts-build")
    assert "pnpm -r run build" in runs
    assert "gen:api" in runs
    assert "git diff --exit-code" in runs


def test_security_job_runs_audits() -> None:
    runs = _runs("security")
    for needle in ("gitleaks git", "sha256sum --check", "pip-audit", "uv export --frozen", "pnpm audit"):
        assert needle in runs, needle
    checkout = next(
        s for s in JOBS["security"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_actionlint_job_runs_actionlint() -> None:
    assert "uv run actionlint" in _runs("actionlint")


def test_gitleaks_versions_agree() -> None:
    hook_repo = next(r for r in PRECOMMIT["repos"] if r["repo"].endswith("gitleaks/gitleaks"))
    assert hook_repo["rev"] == f"v{JOBS['security']['env']['GITLEAKS_VERSION']}"


def test_gitleaks_config_extends_defaults_with_scoped_allowlists() -> None:
    config = tomllib.loads(GITLEAKS_PATH.read_text(encoding="utf-8"))
    assert config["extend"]["useDefault"] is True
    for allowlist in config["allowlists"]:
        assert allowlist["condition"] == "AND"
        assert allowlist["paths"] and allowlist["regexes"]


def test_precommit_config_valid() -> None:
    cmd = [sys.executable, "-m", "pre_commit", "validate-config", str(PRECOMMIT_PATH)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_precommit_has_required_hooks() -> None:
    ids = {hook["id"] for repo in PRECOMMIT["repos"] for hook in repo["hooks"]}
    assert ids >= REQUIRED_HOOKS


def test_precommit_local_hooks_use_locked_tools() -> None:
    local = next(r for r in PRECOMMIT["repos"] if r["repo"] == "local")
    for hook in local["hooks"]:
        assert hook["language"] == "system", hook["id"]
        assert hook["entry"].startswith(("uv run ", "corepack pnpm exec ")), hook["id"]


def test_docs_mention_ci_and_pre_commit() -> None:
    agents = AGENTS_MD.read_text(encoding="utf-8")
    assert "uv run pre-commit install" in agents
    assert "uv run actionlint" in agents
    assert "P00-T14" in SPEC_29.read_text(encoding="utf-8")
