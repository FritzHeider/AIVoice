"""The local docker-compose stack matches specs/29 §2, specs/02 §2, AGENTS.md, and `.env.example`.

These tests read the committed files only; the `docker compose config` checks need the Docker CLI but no
daemon.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import yaml

import env_spec as es

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
COLLECTOR_PATH = ROOT / "infra" / "otel" / "collector.yaml"
PROMETHEUS_PATH = ROOT / "infra" / "prometheus" / "prometheus.yml"
INIT_SQL_PATH = ROOT / "infra" / "docker" / "postgres-init" / "01-roles.sql"
SPEC_29 = ROOT / "specs" / "29-infrastructure.md"
SPEC_02 = ROOT / "specs" / "02-repo-layout-and-conventions.md"
AGENTS_MD = ROOT / "AGENTS.md"
DEV_UP = ROOT / "scripts" / "dev_up.sh"
DEV_DOWN = ROOT / "scripts" / "dev_down.sh"

INFRA_SERVICES = {
    "postgres",
    "redis",
    "clickhouse",
    "temporal",
    "temporal-ui",
    "minio",
    "minio-init",
    "mailpit",
    "otel-collector",
}
OBSERVABILITY_ONLY = {"jaeger", "prometheus", "grafana"}
NO_HEALTHCHECK = {"minio-init", "otel-collector"}
COMPARED_KEYS = ("image", "ports", "environment", "command", "entrypoint", "ulimits")
COLLECTOR_EXTRA_PORTS = ["8889:8889", "13133:13133"]
# Keys the specs/29 §2 Decision adds to services whose spec entry does not set them.
DOCUMENTED_ADDITIONS = {("grafana", "environment")}

Mapping = dict[str, Any]


def _load_yaml(path: Path) -> Mapping:
    return cast("Mapping", yaml.safe_load(path.read_text(encoding="utf-8")))


COMPOSE = _load_yaml(COMPOSE_PATH)
SERVICES = cast("dict[str, Mapping]", COMPOSE["services"])


def _section(text: str, heading_prefix: str) -> str:
    """Return the markdown between the heading starting with `heading_prefix` and the next `## ` heading."""
    start = text.index(heading_prefix)
    end = text.find("\n## ", start + len(heading_prefix))
    return text[start : end if end >= 0 else len(text)]


def _spec_compose() -> Mapping:
    section = _section(SPEC_29.read_text(encoding="utf-8"), "## 2. docker-compose")
    match = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert match is not None
    return cast("Mapping", yaml.safe_load(match.group(1)))


SPEC_SERVICES = cast("dict[str, Mapping]", _spec_compose()["services"])


def _host_ports(service: Mapping) -> list[int]:
    return [int(str(port).split(":")[0]) for port in service.get("ports", [])]


def _publisher(host_port: int) -> str:
    owners = [name for name, svc in SERVICES.items() if host_port in _host_ports(svc)]
    assert len(owners) == 1, f"host port {host_port} published by {owners}"
    return owners[0]


def _env_example() -> dict[str, str]:
    return {a.name: a.value for a in es.parse_env_example(es.load_env_example())}


def _entrypoint_text(service: Mapping) -> str:
    entrypoint = service["entrypoint"]
    return entrypoint if isinstance(entrypoint, str) else " ".join(entrypoint)


def test_image_pins_match_toolchain_table() -> None:
    section = _section(SPEC_02.read_text(encoding="utf-8"), "## 2. Toolchain versions")
    table = "\n".join(line for line in section.splitlines() if line.startswith("|"))
    pinned = re.findall(r"`([a-z0-9./-]+:[A-Za-z0-9._-]+)`", table)
    assert len(pinned) == 7
    images = {svc["image"] for svc in SERVICES.values()}
    for image in pinned:
        assert image in images, image


def test_images_match_spec_compose_block() -> None:
    for name, spec in SPEC_SERVICES.items():
        if "build" in spec:
            continue
        assert name in SERVICES, f"{name} from specs/29 §2 is missing"
        for key in COMPARED_KEYS:
            expected = spec.get(key)
            if name == "otel-collector" and key == "ports":
                expected = [*spec["ports"], *COLLECTOR_EXTRA_PORTS]
            if (name, key) in DOCUMENTED_ADDITIONS:
                assert expected is None, f"{name}.{key} is now in the spec; compare it"
                continue
            assert SERVICES[name].get(key) == expected, f"{name}.{key}"


def test_bucket_job_matches_spec() -> None:
    assert _entrypoint_text(SERVICES["minio-init"]) == _entrypoint_text(SPEC_SERVICES["minio-init"])
    assert "mc mb -p " in _entrypoint_text(SERVICES["minio-init"])


def test_profiles_match_spec_with_collector_in_every_stack() -> None:
    for name, spec in SPEC_SERVICES.items():
        if "build" in spec or name == "otel-collector":
            continue
        assert SERVICES[name]["profiles"] == spec["profiles"], name
    collector = set(SERVICES["otel-collector"]["profiles"])
    assert set(SPEC_SERVICES["otel-collector"]["profiles"]) <= collector
    assert {"infra", "apps", "e2e"} <= collector


def test_no_application_services_yet() -> None:
    assert not [name for name, svc in SERVICES.items() if "build" in svc]


def test_infra_profile_services() -> None:
    infra = {name for name, svc in SERVICES.items() if "infra" in svc["profiles"]}
    assert infra == INFRA_SERVICES
    observability_only = {name for name, svc in SERVICES.items() if svc["profiles"] == ["observability"]}
    assert observability_only == OBSERVABILITY_ONLY


def test_ports_match_agents_md() -> None:
    ports_text = _section(AGENTS_MD.read_text(encoding="utf-8"), "## Ports (local)")
    expected = {
        "Postgres 5432": ("postgres", 5432),
        "Redis 6379": ("redis", 6379),
        "ClickHouse HTTP 8123": ("clickhouse", 8123),
        "published as 9009": ("clickhouse", 9009),
        "Temporal 7233": ("temporal", 7233),
        "Temporal UI 8233": ("temporal-ui", 8233),
        "MinIO API 9000": ("minio", 9000),
        "console 9001": ("minio", 9001),
        "Mailpit SMTP 1025": ("mailpit", 1025),
        "UI 8025": ("mailpit", 8025),
        "OTel collector 4317": ("otel-collector", 4317),
        "OTLP HTTP 4318": ("otel-collector", 4318),
        "collector health 13133": ("otel-collector", 13133),
        "collector metrics 8889": ("otel-collector", 8889),
        "Jaeger 16686": ("jaeger", 16686),
        "Prometheus 9090": ("prometheus", 9090),
        "Grafana 3001": ("grafana", 3001),
    }
    flat = " ".join(ports_text.split())
    for phrase, (service, port) in expected.items():
        assert phrase in flat, phrase
        assert _publisher(port) == service, phrase
    published = [port for svc in SERVICES.values() for port in _host_ports(svc)]
    assert len(published) == len(set(published))


def test_long_running_services_have_healthchecks() -> None:
    for name, svc in SERVICES.items():
        if name in NO_HEALTHCHECK:
            assert "healthcheck" not in svc, name
            continue
        assert svc["healthcheck"]["test"], name
        assert svc["restart"] == "unless-stopped", name
    assert SERVICES["minio-init"]["restart"] == "no"


def test_depends_on_reference_healthy_services() -> None:
    for name, svc in SERVICES.items():
        for dep, cond in svc.get("depends_on", {}).items():
            assert dep in SERVICES, f"{name} -> {dep}"
            assert cond["condition"] == "service_healthy", f"{name} -> {dep}"
            assert "healthcheck" in SERVICES[dep], dep
            assert set(SERVICES[dep]["profiles"]) >= set(svc["profiles"]), f"{name} -> {dep}"


def test_named_volumes_declared() -> None:
    declared = set(COMPOSE["volumes"])
    used: set[str] = set()
    for svc in SERVICES.values():
        for volume in svc.get("volumes", []):
            source = str(volume).split(":")[0]
            if not source.startswith("./"):
                used.add(source)
    assert used == declared


def test_compose_mounts_exist() -> None:
    for name, svc in SERVICES.items():
        for volume in svc.get("volumes", []):
            source = str(volume).split(":")[0]
            if source.startswith("./"):
                assert (ROOT / source).exists(), f"{name}: {source}"


def test_credentials_match_env_example() -> None:
    env = _env_example()
    db = urlsplit(env["DATABASE_URL"])
    pg_env = SERVICES["postgres"]["environment"]
    assert (db.username, db.password, db.path.lstrip("/")) == (
        pg_env["POSTGRES_USER"],
        pg_env["POSTGRES_PASSWORD"],
        pg_env["POSTGRES_DB"],
    )
    minio_env = SERVICES["minio"]["environment"]
    assert env["S3_ACCESS_KEY_ID"] == minio_env["MINIO_ROOT_USER"]
    assert env["S3_SECRET_ACCESS_KEY"] == minio_env["MINIO_ROOT_PASSWORD"]
    init = _entrypoint_text(SERVICES["minio-init"])
    assert f"http://minio:9000 {env['S3_ACCESS_KEY_ID']} {env['S3_SECRET_ACCESS_KEY']}" in init
    buckets = set(re.findall(r"local/(va-[a-z-]+)", init))
    assert buckets == {env["S3_BUCKET_RECORDINGS"], env["S3_BUCKET_KB"], env["S3_BUCKET_VOICES"]}
    ch = urlsplit(env["CLICKHOUSE_URL"])
    assert ch.path.lstrip("/") == SERVICES["clickhouse"]["environment"]["CLICKHOUSE_DB"]


@pytest.mark.parametrize(
    ("variable", "service"),
    [
        ("DATABASE_URL", "postgres"),
        ("REDIS_URL", "redis"),
        ("CLICKHOUSE_URL", "clickhouse"),
        ("TEMPORAL_HOST", "temporal"),
        ("S3_ENDPOINT_URL", "minio"),
        ("SMTP_URL", "mailpit"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector"),
    ],
)
def test_env_example_endpoints_are_published(variable: str, service: str) -> None:
    value = _env_example()[variable]
    port = urlsplit(value if "://" in value else f"tcp://{value}").port
    assert port is not None
    assert _publisher(port) == service


def _assert_pipeline_components_defined(config: Mapping, pipeline: Mapping) -> None:
    for kind in ("receivers", "processors", "exporters"):
        undefined = [component for component in pipeline[kind] if component not in config[kind]]
        assert not undefined, f"{kind}: {undefined}"


def test_collector_config() -> None:
    config = _load_yaml(COLLECTOR_PATH)
    protocols = config["receivers"]["otlp"]["protocols"]
    assert protocols["grpc"]["endpoint"] == "0.0.0.0:4317"
    assert protocols["http"]["endpoint"] == "0.0.0.0:4318"
    assert config["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
    assert config["exporters"]["prometheus"]["endpoint"] == "0.0.0.0:8889"
    service = config["service"]
    assert service["extensions"] == ["health_check"]
    assert set(service["pipelines"]) == {"traces", "metrics"}
    for pipeline in service["pipelines"].values():
        _assert_pipeline_components_defined(config, pipeline)
        assert pipeline["processors"][0] == "memory_limiter"
    assert service["pipelines"]["traces"]["exporters"] == ["otlp/jaeger"]


def test_collector_config_is_mounted() -> None:
    mounts = SERVICES["otel-collector"]["volumes"]
    assert "./infra/otel/collector.yaml:/etc/otelcol-contrib/config.yaml:ro" in mounts


def test_prometheus_config() -> None:
    config = _load_yaml(PROMETHEUS_PATH)
    targets = {
        job["job_name"]: [t for sc in job["static_configs"] for t in sc["targets"]]
        for job in config["scrape_configs"]
    }
    assert targets["otel-collector"] == ["otel-collector:8889"]
    assert targets["prometheus"] == ["localhost:9090"]
    assert all(targets.values())
    assert (
        "./infra/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro"
        in SERVICES["prometheus"]["volumes"]
    )


def test_grafana_datasource_points_at_prometheus() -> None:
    datasource = _load_yaml(ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "prometheus.yaml")
    (entry,) = datasource["datasources"]
    assert entry["url"] == "http://prometheus:9090"
    assert entry["isDefault"] is True


def test_postgres_init_sql() -> None:
    sql = INIT_SQL_PATH.read_text(encoding="utf-8")
    assert "CREATE ROLE va_app LOGIN PASSWORD 'va'" in sql
    assert "CREATE ROLE va_system LOGIN BYPASSRLS PASSWORD 'va'" in sql
    assert "CREATE DATABASE temporal" in sql


@pytest.mark.parametrize("script", [DEV_UP, DEV_DOWN], ids=lambda p: p.name)
def test_scripts_executable_and_strict(script: Path) -> None:
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in lines
    assert script.stat().st_mode & stat.S_IXUSR


def test_makefile_up_down_use_scripts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "PROFILE ?= infra\n" in makefile
    assert re.search(r"^up:.*\n\tscripts/dev_up\.sh \$\(PROFILE\)$", makefile, re.MULTILINE)
    assert re.search(r"^down:.*\n\tscripts/dev_down\.sh$", makefile, re.MULTILINE)


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, env=env, check=False, capture_output=True, text=True, timeout=60)


def test_dev_down_rejects_unknown_arg() -> None:
    result = _run(["bash", str(DEV_DOWN), "--bogus"])
    assert result.returncode == 2
    assert "unknown argument: --bogus" in result.stderr


def test_dev_up_rejects_extra_args() -> None:
    result = _run(["bash", str(DEV_UP), "infra", "apps"])
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_dev_up_fails_clearly_without_daemon(tmp_path: Path) -> None:
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    bash = shutil.which("bash")
    assert bash is not None
    env = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}
    result = _run([bash, str(DEV_UP)], env=env)
    assert result.returncode == 1
    assert "Docker daemon is not reachable" in result.stderr


_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  info) exit 0 ;;
  *"config --services"*) printf '%s\\n' {services} ;;
esac
exit 0
"""


def _bash_interpreters() -> list[str]:
    found = [shutil.which("bash"), "/bin/bash" if Path("/bin/bash").exists() else None]
    return sorted({path for path in found if path is not None})


def _fake_tools(tmp_path: Path, services: list[str]) -> tuple[dict[str, str], Path]:
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(_FAKE_DOCKER.format(log=log, services=" ".join(services)), encoding="utf-8")
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for tool in (docker, curl):
        tool.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}, log


@pytest.mark.parametrize("bash", _bash_interpreters())
def test_dev_up_without_one_shot_jobs(tmp_path: Path, bash: str) -> None:
    env, log = _fake_tools(tmp_path, ["jaeger", "prometheus", "grafana", "otel-collector"])
    result = _run([bash, str(DEV_UP), "observability"], env=env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "compose --profile observability up -d --wait" in calls
    assert "run --rm" not in calls


@pytest.mark.parametrize("bash", _bash_interpreters())
def test_dev_up_runs_one_shot_jobs_after_wait(tmp_path: Path, bash: str) -> None:
    env, log = _fake_tools(tmp_path, ["postgres", "minio", "minio-init"])
    result = _run([bash, str(DEV_UP), "infra,observability"], env=env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    up = next(i for i, line in enumerate(calls) if " up -d --wait " in line)
    assert calls[up].endswith(" postgres minio")
    assert calls[up + 1] == "compose --profile infra --profile observability run --rm minio-init"


@pytest.mark.parametrize("bash", _bash_interpreters())
def test_dev_up_rejects_empty_profile_list(tmp_path: Path, bash: str) -> None:
    env, log = _fake_tools(tmp_path, [])
    result = _run([bash, str(DEV_UP), ","], env=env)
    assert result.returncode == 2
    assert "no profile given" in result.stderr
    assert log.read_text(encoding="utf-8") == "info\n"


DOCKER = shutil.which("docker")
needs_docker_cli = pytest.mark.skipif(DOCKER is None, reason="docker CLI not installed")


@needs_docker_cli
def test_docker_compose_config_valid() -> None:
    result = _run(["docker", "compose", "--profile", "*", "config", "--quiet"])
    assert result.returncode == 0, result.stderr


@needs_docker_cli
def test_docker_compose_infra_profile_services() -> None:
    result = _run(["docker", "compose", "--profile", "infra", "config", "--services"])
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == INFRA_SERVICES
