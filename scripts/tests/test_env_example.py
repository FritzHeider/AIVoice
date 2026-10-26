"""`.env.example` mirrors the environment-variable table in specs/02 §6 (specs/02 §1, §6, §7).

These tests are the gate that keeps the two in lockstep: they fail when a variable is added to the spec table
but not to `.env.example`, and when one is added to `.env.example` but not to the spec table.
"""

from __future__ import annotations

import re

import pytest

import env_spec as es

SPEC_TEXT = es.SPEC_PATH.read_text(encoding="utf-8")
SPEC_VARS = es.parse_spec_table(SPEC_TEXT)
ENV_TEXT = es.load_env_example()
ASSIGNMENTS = es.parse_env_example(ENV_TEXT)

EXPECTED_SPEC_NAMES = 106
EXPECTED_LITERAL_KEYS = 105
EXPECTED_KIND_COUNTS = {
    es.DevValueKind.LITERAL: 78,
    es.DevValueKind.EMPTY: 16,
    es.DevValueKind.FREE: 12,
}
SECRET_SHAPED = ("SECRET_KEY", "STREAM_TOKEN_SECRET", "LOCAL_MASTER_KEY", "API_KEY_PEPPER")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

TABLE_HEADER = ("| Variable | Used by | Dev value | Meaning |", "|---|---|---|---|")


def _mini_spec(*rows: str) -> str:
    """Build a minimal markdown document shaped like specs/02 §6 around the given body rows."""
    return "\n".join(["# Heading", "", es.SECTION_HEADING, "", *TABLE_HEADER, *rows, "", "## 7. Next"])


def _by_name(name: str) -> es.SpecVar:
    return next(var for var in SPEC_VARS if var.name == name)


# --- The spec table itself ---------------------------------------------------------------------------------


def test_spec_table_shape() -> None:
    assert len(SPEC_VARS) == EXPECTED_SPEC_NAMES
    names = [var.name for var in SPEC_VARS]
    assert len(set(names)) == len(names)
    assert [var.name for var in SPEC_VARS if var.templated] == ["LOG_LEVEL_<COMPONENT>"]
    assert len([var for var in SPEC_VARS if not var.templated]) == EXPECTED_LITERAL_KEYS
    counts = {kind: len([var for var in SPEC_VARS if var.kind is kind]) for kind in es.DevValueKind}
    assert counts == EXPECTED_KIND_COUNTS


def test_s3_credential_row_splits_into_two_vars() -> None:
    assert _by_name("S3_ACCESS_KEY_ID").literal == "minio"
    assert _by_name("S3_SECRET_ACCESS_KEY").literal == "minio12345"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("EDGE_PUBLIC_HOST", "localhost:8100"),
        ("LOG_FORMAT", "console"),
        ("WEBHOOK_ALLOW_PRIVATE", "true"),
        ("LLM_MODEL_COSTS_JSON", "{}"),
    ],
)
def test_literal_extraction_ignores_trailing_prose(name: str, expected: str) -> None:
    var = _by_name(name)
    assert var.kind is es.DevValueKind.LITERAL
    assert var.literal == expected


@pytest.mark.parametrize("name", ["TELNYX_PUBLIC_KEY", "TELNYX_CALL_CONTROL_APP_ID"])
def test_descriptive_dev_cells_are_free(name: str) -> None:
    """Dev cells that only mention a command in backticks describe the value; they do not name it."""
    var = _by_name(name)
    assert var.kind is es.DevValueKind.FREE
    assert var.literal is None


def test_empty_dev_cells_with_trailing_prose_are_empty() -> None:
    var = _by_name("SESSION_COOKIE_DOMAIN")
    assert var.dev_cell == "empty (host-only cookie)"
    assert var.kind is es.DevValueKind.EMPTY


# --- Parity between the spec table and .env.example --------------------------------------------------------


def test_env_example_has_no_duplicate_keys() -> None:
    names = [assignment.name for assignment in ASSIGNMENTS]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == []


def test_env_example_covers_every_spec_variable() -> None:
    expected = {var.name for var in SPEC_VARS if not var.templated}
    actual = {assignment.name for assignment in ASSIGNMENTS}
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    assert (missing, extra) == ([], []), (
        f"missing from .env.example: {missing}; absent from specs/02 §6: {extra}"
    )


def test_env_example_order_matches_spec_order() -> None:
    expected = [var.name for var in SPEC_VARS if not var.templated]
    assert [assignment.name for assignment in ASSIGNMENTS] == expected


def test_env_example_literal_values_match_spec() -> None:
    values = {assignment.name: assignment.value for assignment in ASSIGNMENTS}
    mismatched = {
        var.name: (values[var.name], var.literal)
        for var in SPEC_VARS
        if var.kind is es.DevValueKind.LITERAL and values[var.name] != var.literal
    }
    assert mismatched == {}


def test_env_example_empty_valued_variables_are_empty() -> None:
    values = {assignment.name: assignment.value for assignment in ASSIGNMENTS}
    non_empty = {
        var.name: values[var.name]
        for var in SPEC_VARS
        if var.kind is es.DevValueKind.EMPTY and values[var.name] != ""
    }
    assert non_empty == {}


def test_free_valued_variables_are_all_defined() -> None:
    defined = {assignment.name for assignment in ASSIGNMENTS}
    free = {var.name for var in SPEC_VARS if var.kind is es.DevValueKind.FREE and not var.templated}
    assert free <= defined


def test_every_assignment_has_a_trailing_comment() -> None:
    uncommented = [assignment.name for assignment in ASSIGNMENTS if not assignment.comment]
    assert uncommented == []


def test_templated_variables_appear_only_as_commented_examples() -> None:
    templated = [var.name for var in SPEC_VARS if var.templated]
    assigned = {assignment.name for assignment in ASSIGNMENTS}
    comments = es.commented_lines(ENV_TEXT)
    for name in templated:
        assert name not in assigned
        assert any(name in comment for comment in comments), f"{name} is not shown as a commented example"


def test_secret_shaped_values_are_64_hex_and_distinct() -> None:
    by_name = {assignment.name: assignment for assignment in ASSIGNMENTS}
    values = [by_name[name].value for name in SECRET_SHAPED]
    assert all(HEX64.match(value) for value in values), values
    assert len(set(values)) == len(SECRET_SHAPED)
    for name in SECRET_SHAPED:
        assert "openssl rand -hex 32" in by_name[name].comment


def test_email_from_is_quoted_in_the_file() -> None:
    assert 'EMAIL_FROM="VoiceAgents <no-reply@voiceagents.local>"' in ENV_TEXT


def test_preamble_documents_how_to_create_a_local_env() -> None:
    comments = es.commented_lines(ENV_TEXT)
    assert "cp .env.example .env" in comments
    assert any("gitignored" in comment for comment in comments)


# --- The parity check fails in both directions -------------------------------------------------------------


def test_parity_detects_variable_added_to_spec_only() -> None:
    spec = _mini_spec("| `A_VAR` | api | `1` | first |", "| `B_VAR` | api | `2` | second |")
    env = "A_VAR=1  # first\n"
    spec_names = {var.name for var in es.parse_spec_table(spec)}
    env_names = {assignment.name for assignment in es.parse_env_example(env)}
    assert spec_names - env_names == {"B_VAR"}


def test_parity_detects_variable_added_to_env_only() -> None:
    spec = _mini_spec("| `A_VAR` | api | `1` | first |")
    env = "A_VAR=1  # first\nB_VAR=2  # second\n"
    spec_names = {var.name for var in es.parse_spec_table(spec)}
    env_names = {assignment.name for assignment in es.parse_env_example(env)}
    assert env_names - spec_names == {"B_VAR"}


def test_parity_detects_changed_literal_value() -> None:
    spec = _mini_spec("| `A_VAR` | api | `1` | first |")
    (var,) = es.parse_spec_table(spec)
    (assignment,) = es.parse_env_example("A_VAR=2  # first\n")
    assert assignment.value != var.literal


# --- Parser units ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rhs", "expected"),
    [
        ('"a b"  # c', ("a b", "c")),
        ("'a b'# c", ("a b", "c")),
        ('"a#b"', ("a#b", "")),
        ("plain  # c", ("plain", "c")),
        ("plain", ("plain", "")),
        ("  # only a comment", ("", "only a comment")),
        ("", ("", "")),
    ],
)
def test_split_trailing_comment(rhs: str, expected: tuple[str, str]) -> None:
    assert es.split_trailing_comment(rhs) == expected


def test_split_trailing_comment_rejects_unterminated_quote() -> None:
    with pytest.raises(es.EnvSpecError, match="unterminated quote"):
        es.split_trailing_comment('"a b')


def test_split_trailing_comment_rejects_text_after_the_quoted_value() -> None:
    with pytest.raises(es.EnvSpecError, match="unexpected text"):
        es.split_trailing_comment('"a" b')


def test_parse_env_example_rejects_a_line_that_is_neither_comment_nor_assignment() -> None:
    with pytest.raises(es.EnvSpecError, match="line 2"):
        es.parse_env_example("A_VAR=1  # ok\nnot an assignment\n")


def test_parse_env_example_ignores_blank_and_comment_lines() -> None:
    assignments = es.parse_env_example("# header\n\n   \nA_VAR=1  # ok\n")
    assert [(a.name, a.value, a.comment, a.line_no) for a in assignments] == [("A_VAR", "1", "ok", 4)]


def test_commented_lines_strips_markers() -> None:
    assert es.commented_lines("# a\nB=1  # inline\n#  b\n") == ["a", "b"]


def test_parse_spec_table_requires_the_section_heading() -> None:
    with pytest.raises(es.EnvSpecError, match="not found"):
        es.parse_spec_table("# Some other document\n")


def test_parse_spec_table_requires_a_table_under_the_heading() -> None:
    markdown = "\n".join([es.SECTION_HEADING, "", "Prose, not a table.", ""])
    with pytest.raises(es.EnvSpecError, match="no table directly under"):
        es.parse_spec_table(markdown)


def test_parse_spec_table_requires_data_rows() -> None:
    with pytest.raises(es.EnvSpecError, match="no data rows"):
        es.parse_spec_table(_mini_spec())


def test_parse_spec_table_requires_four_cells() -> None:
    with pytest.raises(es.EnvSpecError, match="expected 4 cells"):
        es.parse_spec_table(_mini_spec("| `A_VAR` | api | `1` |"))


def test_parse_spec_table_requires_one_dev_value_per_name() -> None:
    with pytest.raises(es.EnvSpecError, match="declares 2 names"):
        es.parse_spec_table(_mini_spec("| `A_VAR` / `B_VAR` | api | `1` | both |"))


def test_parse_spec_table_rejects_an_empty_name() -> None:
    with pytest.raises(es.EnvSpecError, match="empty variable name"):
        es.parse_spec_table(_mini_spec("| `A_VAR` / | api | `1` / `2` | both |"))


def test_classify_dev_value_rejects_an_unterminated_backtick() -> None:
    with pytest.raises(es.EnvSpecError, match="unterminated backtick"):
        es.classify_dev_value("`oops")


def test_load_helpers_read_the_committed_files() -> None:
    assert es.repo_root().joinpath("pyproject.toml").is_file()
    assert es.SPEC_PATH.is_file()
    assert es.ENV_EXAMPLE_PATH.name == ".env.example"
    assert len(es.load_spec_variables()) == EXPECTED_SPEC_NAMES
    assert es.load_env_example().startswith("# VoiceAgents environment contract.")
