from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import check_toolchain as ct

GOOD_OUTPUTS: Mapping[tuple[str, ...], str] = {
    ("uv", "--version"): "uv 0.12.17 (Homebrew 2026-09-18 aarch64-apple-darwin)",
    ("node", "--version"): "v22.23.4",
    ("pnpm", "--version"): "9.15.9",
    ("docker", "--version"): "Docker version 27.3.1, build ce12230",
    ("docker", "compose", "version"): "Docker Compose version v2.29.7",
}


def make_runner(outputs: Mapping[tuple[str, ...], str | None]) -> ct.Runner:
    def runner(cmd: Sequence[str]) -> str | None:
        return outputs.get(tuple(cmd))

    return runner


def requirement(name: str) -> ct.Requirement:
    return next(r for r in ct.REQUIREMENTS if r.name == name)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("uv 0.12.17 (Homebrew 2026-09-18 aarch64-apple-darwin)", (0, 12, 17)),
        ("v22.23.4", (22, 23, 4)),
        ("9.15.9", (9, 15, 9)),
        ("Docker version 27.3.1, build ce12230", (27, 3, 1)),
        ("Docker Compose version v2.29.7", (2, 29, 7)),
        ("3.12", (3, 12)),
        ("garbage", None),
    ],
)
def test_parse_version_variants(text: str, expected: tuple[int, ...] | None) -> None:
    assert ct.parse_version(text) == expected


def test_evaluate_ok_and_bounds() -> None:
    node = requirement("node")
    assert ct.evaluate(node, "v22.23.4").ok

    upper = ct.evaluate(node, "v23.0.0")
    assert not upper.ok
    assert upper.message == "found 23.0.0, need >=22,<23"

    lower = ct.evaluate(node, "v20.1.0")
    assert not lower.ok
    assert lower.found == "20.1.0"
    assert ">=22" in lower.message


def test_evaluate_open_upper_bound() -> None:
    uv = requirement("uv")
    assert ct.evaluate(uv, "uv 3.0.0").ok
    too_old = ct.evaluate(uv, "uv 0.4.30")
    assert not too_old.ok
    assert too_old.message == "found 0.4.30, need >=0.5"


def test_evaluate_unparseable_output() -> None:
    result = ct.evaluate(requirement("uv"), "command produced nonsense\n")
    assert not result.ok
    assert result.found is None
    assert "could not parse version" in result.message


def test_missing_binary_required_fails() -> None:
    outputs = {k: v for k, v in GOOD_OUTPUTS.items() if k[0] != "node"}
    results = {r.name: r for r in ct.run_checks(make_runner(outputs), (3, 12, 7))}
    assert not results["node"].ok
    assert results["node"].required
    assert results["node"].message == "not found on PATH"


def test_missing_binary_optional_warns(capsys: pytest.CaptureFixture[str]) -> None:
    outputs = {k: v for k, v in GOOD_OUTPUTS.items() if k[0] != "docker"}
    exit_code = ct.main([], runner=make_runner(outputs), python_version=(3, 12, 7))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "warn docker: not found on PATH" in out
    assert "warn docker compose: not found on PATH" in out


def test_pnpm_falls_back_to_corepack() -> None:
    outputs: dict[tuple[str, ...], str | None] = dict(GOOD_OUTPUTS)
    del outputs[("pnpm", "--version")]
    outputs[("corepack", "pnpm", "--version")] = "9.15.9"
    results = {r.name: r for r in ct.run_checks(make_runner(outputs), (3, 12, 7))}
    assert results["pnpm"].ok
    assert results["pnpm"].found == "9.15.9"


def test_pnpm_wrong_major_via_corepack_fails() -> None:
    outputs: dict[tuple[str, ...], str | None] = dict(GOOD_OUTPUTS)
    del outputs[("pnpm", "--version")]
    outputs[("corepack", "pnpm", "--version")] = "12.0.1"
    results = {r.name: r for r in ct.run_checks(make_runner(outputs), (3, 12, 7))}
    assert not results["pnpm"].ok
    assert results["pnpm"].message == "found 12.0.1, need >=9,<10"


@pytest.mark.parametrize(("version", "ok"), [((3, 11, 9), False), ((3, 12, 7), True), ((3, 13, 0), False)])
def test_python_version_check(version: tuple[int, int, int], ok: bool) -> None:
    results = {r.name: r for r in ct.run_checks(make_runner(GOOD_OUTPUTS), version)}
    assert results["python"].ok is ok


def test_main_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert ct.main([], runner=make_runner(GOOD_OUTPUTS), python_version=(3, 12, 7)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "ok   python 3.12.7",
        "ok   uv 0.12.17",
        "ok   node 22.23.4",
        "ok   pnpm 9.15.9",
        "ok   docker 27.3.1",
        "ok   docker compose 2.29.7",
    ]

    bad: dict[tuple[str, ...], str | None] = dict(GOOD_OUTPUTS)
    bad[("node", "--version")] = "v20.1.0"
    assert ct.main([], runner=make_runner(bad), python_version=(3, 12, 7)) == 1
    out_text = capsys.readouterr().out
    assert "FAIL node: found 20.1.0, need >=22,<23" in out_text
    assert "toolchain check failed: node" in out_text


def test_subprocess_runner_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="v22.1.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ct.subprocess_runner(("node", "--version")) == "v22.1.0\n"
    assert captured["cmd"] == ["node", "--version"]
    assert captured["kwargs"] == {
        "capture_output": True,
        "text": True,
        "timeout": ct.COMMAND_TIMEOUT_SECONDS,
        "check": False,
        "env": None,
    }


def test_subprocess_runner_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ct.subprocess_runner(("docker", "compose", "version")) is None


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), subprocess.TimeoutExpired(cmd=["pnpm"], timeout=15)]
)
def test_subprocess_runner_unavailable(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ct.subprocess_runner(("pnpm", "--version")) is None


def test_parse_args_search_path() -> None:
    assert ct.parse_args(["--search-path", "/a:/b"]).search_path == "/a:/b"


def test_parse_args_default_none() -> None:
    assert ct.parse_args([]).search_path is None


def write_tool(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)


def test_subprocess_runner_uses_search_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good, shadow = tmp_path / "good", tmp_path / "shadow"
    write_tool(good, "node", "echo v22.1.0")
    write_tool(shadow, "node", "echo v26.9.0")
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{os.environ['PATH']}")

    chosen = ct.subprocess_runner(
        ("node", "--version"), search_path=f"{good}{os.pathsep}{os.environ['PATH']}"
    )
    assert chosen is not None
    assert "22.1.0" in chosen

    inherited = ct.subprocess_runner(("node", "--version"))
    assert inherited is not None
    assert "26.9.0" in inherited


def test_main_with_search_path_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good, shadow = tmp_path / "good", tmp_path / "shadow"
    write_tool(good, "uv", "echo uv 0.12.17")
    write_tool(good, "node", "echo v22.1.0")
    write_tool(good, "pnpm", "echo 9.15.9")
    write_tool(
        good,
        "docker",
        'if [ "$1" = compose ]; then echo "Docker Compose version v2.29.0"; '
        'else echo "Docker version 27.0.0"; fi',
    )
    write_tool(shadow, "node", "echo v26.9.0")
    monkeypatch.setenv("PATH", f"{shadow}{os.pathsep}{good}{os.pathsep}/usr/bin{os.pathsep}/bin")

    assert (
        ct.main(["--search-path", f"{good}{os.pathsep}/usr/bin{os.pathsep}/bin"], python_version=(3, 12, 7))
        == 0
    )
    out = capsys.readouterr().out
    assert "ok   node 22.1.0" in out
    assert "ok   docker compose 2.29.0" in out

    assert ct.main([], python_version=(3, 12, 7)) == 1
    assert "FAIL node: found 26.9.0, need >=22,<23" in capsys.readouterr().out


def test_makefile_setup_passes_shell_path() -> None:
    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    assert 'check_toolchain.py --search-path "$$PATH"' in makefile.read_text()
