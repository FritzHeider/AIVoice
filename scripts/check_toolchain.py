"""Toolchain preflight for `make setup` (specs/02 §2).

Checks that the local Python, uv, Node.js, pnpm, and Docker versions satisfy the pinned ranges. Required tools
fail the check (exit 1); optional tools (Docker, only needed for `make up`) print a warning.

`--search-path` takes the caller's shell PATH because `uv run --python X` prepends the interpreter's directory
(e.g. /opt/homebrew/bin), which can shadow the node, pnpm, uv, and docker the shell would actually use.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Runner = Callable[[Sequence[str]], str | None]

COMMAND_TIMEOUT_SECONDS = 15
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class Requirement:
    name: str
    command: tuple[str, ...]
    minimum: tuple[int, ...]
    below: tuple[int, ...] | None
    required: bool = True
    fallback_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    found: str | None
    message: str
    required: bool = True


PYTHON = Requirement(name="python", command=(), minimum=(3, 12), below=(3, 13))

REQUIREMENTS: tuple[Requirement, ...] = (
    PYTHON,
    Requirement(name="uv", command=("uv", "--version"), minimum=(0, 5), below=None),
    Requirement(name="node", command=("node", "--version"), minimum=(22,), below=(23,)),
    Requirement(
        name="pnpm",
        command=("pnpm", "--version"),
        minimum=(9,),
        below=(10,),
        fallback_command=("corepack", "pnpm", "--version"),
    ),
    Requirement(name="docker", command=("docker", "--version"), minimum=(27,), below=None, required=False),
    Requirement(
        name="docker compose",
        command=("docker", "compose", "version"),
        minimum=(2,),
        below=None,
        required=False,
    ),
)


def parse_version(text: str) -> tuple[int, ...] | None:
    """Return the first dotted version found in ``text`` (``v`` prefix allowed)."""
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _range_text(req: Requirement) -> str:
    text = f">={_fmt(req.minimum)}"
    if req.below is not None:
        text += f",<{_fmt(req.below)}"
    return text


def evaluate(req: Requirement, output: str | None) -> CheckResult:
    """Compare a tool's version output against the requirement's range."""
    if output is None:
        return CheckResult(req.name, ok=False, found=None, message="not found on PATH", required=req.required)
    version = parse_version(output)
    if version is None:
        return CheckResult(
            req.name,
            ok=False,
            found=None,
            message=f"could not parse version from {output.strip()!r}",
            required=req.required,
        )
    found = _fmt(version)
    in_range = version >= req.minimum and (req.below is None or version < req.below)
    if in_range:
        return CheckResult(req.name, ok=True, found=found, message=found, required=req.required)
    return CheckResult(
        req.name,
        ok=False,
        found=found,
        message=f"found {found}, need {_range_text(req)}",
        required=req.required,
    )


def subprocess_runner(cmd: Sequence[str], search_path: str | None = None) -> str | None:
    """Run ``cmd`` and return combined stdout/stderr, or ``None`` when it cannot run successfully.

    When ``search_path`` is given, the command (and any ``#!/usr/bin/env`` shims it starts) resolves against
    that PATH instead of this process's PATH.
    """
    env = None if search_path is None else {**os.environ, "PATH": search_path}
    try:
        completed = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False, env=env
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout + completed.stderr


def make_runner(search_path: str | None) -> Runner:
    """Return a runner that resolves commands against ``search_path`` (``None`` keeps this process's PATH)."""
    return functools.partial(subprocess_runner, search_path=search_path)


def run_checks(
    runner: Runner = subprocess_runner,
    python_version: tuple[int, ...] = tuple(sys.version_info[:3]),
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for req in REQUIREMENTS:
        if req is PYTHON:
            results.append(evaluate(req, _fmt(python_version)))
            continue
        output = runner(req.command)
        if output is None and req.fallback_command is not None:
            output = runner(req.fallback_command)
        results.append(evaluate(req, output))
    return results


def format_result(result: CheckResult) -> str:
    if result.ok:
        return f"ok   {result.name} {result.found}"
    label = "FAIL" if result.required else "warn"
    return f"{label} {result.name}: {result.message}"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the local toolchain against the pinned ranges.")
    parser.add_argument(
        "--search-path",
        default=None,
        help=(
            "PATH used to find node, pnpm, uv, and docker; pass the shell's $PATH because `uv run` prepends "
            "the interpreter directory"
        ),
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    runner: Runner | None = None,
    python_version: tuple[int, ...] = tuple(sys.version_info[:3]),
) -> int:
    args = parse_args(argv)
    if runner is None:
        runner = make_runner(args.search_path)
    results = run_checks(runner, python_version)
    for result in results:
        print(format_result(result))
    failed = [r for r in results if r.required and not r.ok]
    if failed:
        print(f"toolchain check failed: {', '.join(r.name for r in failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
