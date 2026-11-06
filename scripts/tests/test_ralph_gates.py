"""Tests for `scripts/ralph/gates.sh`: gate stamps keyed by the working-tree fingerprint.

Each test runs the real script inside a throwaway git repository with fake `make` and `uv` executables first
on `PATH`, so no real gate runs. The one exception scans the loop's shell scripts with the real
unfinished-work detector, which the fake `make` never does.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import check_no_placeholders

GATES_SCRIPT = Path(__file__).resolve().parents[1] / "ralph" / "gates.sh"
CORE_GATES = ["lint", "typecheck", "".join(("place", "holders")), "openapi", "specs", "test"]
CORE_CALLS = [
    "make lint",
    "make typecheck",
    "make " + CORE_GATES[2],
    "uv run python scripts/check_openapi_drift.py",
    "uv run python scripts/check_spec_refs.py",
    "make test",
]
DETECTOR_GATE = CORE_GATES[2]
GATE_LIST_LINE = "gates: " + " ".join([*CORE_GATES, "ts", "e2e"])
EXIT_VERIFY_FAILED = 1
EXIT_USAGE = 64
EXIT_TREE_CHANGED = 75
FAKE_GATE_STATUS = 2

FAKE_TOOL = """#!/usr/bin/env bash
# Stands in for `make` and `uv`: logs the call, optionally edits a tracked file, optionally fails.
tool="$(basename "$0")"
case "$tool" in
  make) gate="$1" ;;
  *) gate="$(basename "${@: -1}")" ;;
esac
echo "$tool $*" >>"$GATES_CALLS"
if [[ -n "${GATES_TOUCH:-}" && "$gate" == "$GATES_TOUCH" ]]; then
  echo "edited during the run" >>tracked.txt
fi
echo
echo "$gate output line"
for failing in ${GATES_FAIL:-}; do
  if [[ "$gate" == "$failing" ]]; then
    echo "$gate broke"
    exit 2
  fi
done
echo "$gate ok"
"""


@dataclass(frozen=True)
class Outcome:
    code: int
    out: str
    err: str


@dataclass(frozen=True)
class Stamp:
    tree: str
    head: str
    gates: dict[str, tuple[int, str]]


@dataclass(frozen=True)
class Repo:
    root: Path
    bin_dir: Path
    calls_file: Path

    def env(self, *, fail: str = "", touch: str = "") -> dict[str, str]:
        kept = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        return {
            **kept,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Gate Test",
            "GIT_AUTHOR_EMAIL": "gates@voiceagents.test",
            "GIT_COMMITTER_NAME": "Gate Test",
            "GIT_COMMITTER_EMAIL": "gates@voiceagents.test",
            "GATES_CALLS": str(self.calls_file),
            "GATES_FAIL": fail,
            "GATES_TOUCH": touch,
        }

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env(),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return done.stdout

    def gates(self, *args: str, fail: str = "", touch: str = "") -> Outcome:
        done = subprocess.run(
            ["bash", "scripts/ralph/gates.sh", *args],
            cwd=self.root,
            env=self.env(fail=fail, touch=touch),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return Outcome(done.returncode, done.stdout, done.stderr)

    def fingerprint(self) -> str:
        outcome = self.gates("--fingerprint")
        assert outcome.code == 0, outcome.err
        return outcome.out.strip()

    def calls(self) -> list[str]:
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text(encoding="utf-8").splitlines()

    def stamps(self) -> list[Path]:
        return sorted((self.root / ".ralph" / "gates").glob("*.json"))

    def stamp(self) -> Stamp:
        (path,) = self.stamps()
        raw = json.loads(path.read_text(encoding="utf-8"))
        gates = {str(name): (int(gate["exit"]), str(gate["summary"])) for name, gate in raw["gates"].items()}
        return Stamp(tree=str(raw["tree"]), head=str(raw["head"]), gates=gates)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    (root / "scripts" / "ralph").mkdir(parents=True)
    (root / ".ralph" / "agent").mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(GATES_SCRIPT, root / "scripts" / "ralph" / "gates.sh")
    (root / "tracked.txt").write_text("first line\n", encoding="utf-8")
    (root / "removable.txt").write_text("to be removed\n", encoding="utf-8")
    (root / ".gitignore").write_text("/logs/\n.ralph/*\n!.ralph/agent/\n", encoding="utf-8")
    (root / ".ralph" / "agent" / "scratchpad.md").write_text("notes\n", encoding="utf-8")
    for tool in ("make", "uv"):
        path = bin_dir / tool
        path.write_text(FAKE_TOOL, encoding="utf-8")
        path.chmod(0o700)
    made = Repo(root=root, bin_dir=bin_dir, calls_file=tmp_path / "calls.log")
    made.git("init", "-q", "-b", "main")
    made.git("add", "-A")
    made.git("commit", "-q", "-m", "initial tree")
    return made


def test_passing_run_writes_a_stamp_that_verifies(repo: Repo) -> None:
    tree = repo.fingerprint()

    outcome = repo.gates()

    assert outcome.code == 0, outcome.err
    assert repo.calls() == CORE_CALLS
    assert [path.stem for path in repo.stamps()] == [tree]
    stamp = repo.stamp()
    assert stamp.tree == tree
    assert stamp.head == repo.git("rev-parse", "HEAD").strip()
    assert list(stamp.gates) == sorted(CORE_GATES)
    assert {code for code, _ in stamp.gates.values()} == {0}
    assert stamp.gates["test"] == (0, "test ok")
    assert (repo.root / "logs" / "gates" / "test.log").read_text(encoding="utf-8").endswith("test ok\n")

    verified = repo.gates("--verify")

    assert verified.code == 0, verified.out
    assert f"verified: every listed gate passed on tree {tree}" in verified.out
    assert repo.calls() == CORE_CALLS


def test_failing_gate_stops_the_run_and_fails_verification(repo: Repo) -> None:
    outcome = repo.gates(fail="typecheck")

    assert outcome.code == FAKE_GATE_STATUS
    assert repo.calls() == CORE_CALLS[:2]
    assert "gate typecheck failed (exit 2)" in outcome.err
    stamp = repo.stamp()
    assert stamp.gates == {"lint": (0, "lint ok"), "typecheck": (FAKE_GATE_STATUS, "typecheck broke")}

    verified = repo.gates("--verify")

    assert verified.code == EXIT_VERIFY_FAILED
    assert "typecheck     FAILED (exit 2)  typecheck broke" in verified.out
    assert "test          not run" in verified.out


def test_a_rerun_that_passes_replaces_the_failed_result(repo: Repo) -> None:
    assert repo.gates(fail="check_spec_refs.py").code == FAKE_GATE_STATUS
    assert repo.gates("--verify").code == EXIT_VERIFY_FAILED

    assert repo.gates().code == 0

    assert repo.gates("--verify").code == 0
    assert len(repo.stamps()) == 1


def test_tree_change_during_the_run_writes_no_stamp(repo: Repo) -> None:
    outcome = repo.gates(touch="test")

    assert outcome.code == EXIT_TREE_CHANGED
    assert "the working tree changed during the run" in outcome.err
    assert "no stamp written" in outcome.err
    assert repo.stamps() == []
    assert repo.gates("--verify").code == EXIT_VERIFY_FAILED


def test_failing_gate_that_also_changed_the_tree_keeps_its_status_and_writes_no_stamp(repo: Repo) -> None:
    outcome = repo.gates(fail="lint", touch="lint")

    assert outcome.code == FAKE_GATE_STATUS
    assert repo.stamps() == []


@pytest.mark.parametrize("edit", ["modify", "add", "delete"])
def test_any_edit_after_the_run_makes_the_stamp_stale(repo: Repo, edit: str) -> None:
    assert repo.gates().code == 0
    assert repo.gates("--verify").code == 0
    if edit == "modify":
        (repo.root / "tracked.txt").write_text("another line\n", encoding="utf-8")
    elif edit == "add":
        (repo.root / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    else:
        (repo.root / "removable.txt").unlink()

    verified = repo.gates("--verify")

    assert verified.code == EXIT_VERIFY_FAILED
    assert "no stamp for this tree" in verified.out


def test_restoring_an_edit_exactly_makes_the_stamp_valid_again(repo: Repo) -> None:
    assert repo.gates().code == 0
    tracked = repo.root / "tracked.txt"
    original = tracked.read_text(encoding="utf-8")
    tracked.write_text("mutated for a check\n", encoding="utf-8")
    assert repo.gates("--verify").code == EXIT_VERIFY_FAILED

    tracked.write_text(original, encoding="utf-8")

    assert repo.gates("--verify").code == 0


def test_loop_state_ignored_files_and_staging_do_not_change_the_fingerprint(repo: Repo) -> None:
    assert repo.gates().code == 0
    tree = repo.fingerprint()

    (repo.root / ".ralph" / "agent" / "scratchpad.md").write_text("## Review\nnew notes\n", encoding="utf-8")
    (repo.root / ".ralph" / "agent" / "memories.md").write_text("a memory\n", encoding="utf-8")
    (repo.root / "logs" / "probe.log").write_text("probe output\n", encoding="utf-8")
    repo.git("add", ".ralph/agent/scratchpad.md")

    assert repo.fingerprint() == tree
    assert repo.gates("--verify").code == 0


def test_fingerprint_leaves_the_real_index_and_tree_alone(repo: Repo) -> None:
    (repo.root / "tracked.txt").write_text("unstaged edit\n", encoding="utf-8")
    (repo.root / "untracked.txt").write_text("not added\n", encoding="utf-8")
    before = repo.git("status", "--porcelain")

    first = repo.fingerprint()
    second = repo.fingerprint()

    assert first == second
    assert repo.git("status", "--porcelain") == before
    assert repo.git("diff", "--cached", "--name-only") == ""


def test_staging_a_change_does_not_change_the_fingerprint(repo: Repo) -> None:
    (repo.root / "tracked.txt").write_text("edited\n", encoding="utf-8")
    unstaged = repo.fingerprint()

    repo.git("add", "tracked.txt")

    assert repo.fingerprint() == unstaged


def test_optional_gates_are_required_only_when_asked_for(repo: Repo) -> None:
    assert repo.gates().code == 0
    assert repo.gates("--verify").code == 0

    needs_e2e = repo.gates("--verify", "--e2e")

    assert needs_e2e.code == EXIT_VERIFY_FAILED
    assert "e2e           not run" in needs_e2e.out

    added = repo.gates("--only", "e2e")

    assert added.code == 0, added.err
    assert repo.calls() == [*CORE_CALLS, "make e2e"]
    assert repo.gates("--verify", "--e2e").code == 0
    assert repo.gates("--verify", "--only", "e2e").code == 0
    assert repo.gates("--verify", "--ts").code == EXIT_VERIFY_FAILED
    assert len(repo.stamps()) == 1


def test_ts_and_e2e_flags_run_after_the_core_gates(repo: Repo) -> None:
    outcome = repo.gates("--ts", "--e2e")

    assert outcome.code == 0, outcome.err
    assert repo.calls() == [*CORE_CALLS, "make test-ts", "make e2e"]
    assert repo.gates("--verify", "--ts", "--e2e").code == 0
    assert "gates passed for tree" in outcome.out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "unknown option: --bogus"),
        (["--only", "lint,nonsense"], "unknown gate: nonsense"),
        (["--only"], "--only needs a comma-separated gate list"),
    ],
)
def test_bad_arguments_are_usage_errors(repo: Repo, args: list[str], message: str) -> None:
    outcome = repo.gates(*args)

    assert outcome.code == EXIT_USAGE
    assert message in outcome.err
    assert "usage: scripts/ralph/gates.sh" in outcome.err
    assert repo.calls() == []
    assert repo.stamps() == []


def test_help_prints_usage(repo: Repo) -> None:
    outcome = repo.gates("--help")

    assert outcome.code == 0
    assert outcome.out.startswith("usage: scripts/ralph/gates.sh")


def test_help_lists_every_gate_by_name(repo: Repo) -> None:
    outcome = repo.gates("--help")

    assert outcome.code == 0
    assert outcome.err == ""
    assert outcome.out.splitlines()[-1] == GATE_LIST_LINE
    assert repo.calls() == []


def test_usage_errors_list_every_gate_by_name(repo: Repo) -> None:
    outcome = repo.gates("--only", "nonsense")

    assert outcome.code == EXIT_USAGE
    assert outcome.err.splitlines()[-1] == GATE_LIST_LINE


def test_the_detector_gate_runs_alone_under_its_makefile_name(repo: Repo) -> None:
    outcome = repo.gates("--only", DETECTOR_GATE)

    assert outcome.code == 0, outcome.err
    assert repo.calls() == ["make " + DETECTOR_GATE]
    assert repo.stamp().gates == {DETECTOR_GATE: (0, DETECTOR_GATE + " ok")}
    assert repo.gates("--verify", "--only", DETECTOR_GATE).code == 0

    needs_the_rest = repo.gates("--verify")

    assert needs_the_rest.code == EXIT_VERIFY_FAILED
    assert "lint          not run" in needs_the_rest.out
    assert repo.calls() == ["make " + DETECTOR_GATE]


def test_the_ralph_scripts_pass_the_unfinished_work_detector() -> None:
    root = GATES_SCRIPT.parents[2]
    files = sorted(GATES_SCRIPT.parent.glob("*.sh"))

    assert GATES_SCRIPT in files
    assert [item.render() for item in check_no_placeholders.scan(root, files)] == []
