"""Scenario DSL: the specs/30 §3.5 example loads; invalid documents report exact paths, lines, columns."""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent
from typing import Final

import pytest
from pydantic import ValidationError

from va_sim.scenario import (
    DtmfStep,
    ExpectHangupStep,
    HangupStep,
    SayStep,
    Scenario,
    ScenarioError,
    ScenarioIssue,
    SilenceStep,
    WaitAgentSpeechStep,
    load_scenario,
    load_scenario_file,
    load_scenarios,
    scenario_json_schema,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"
BASE: Final = """\
id: base
match:
  to: "+14155550101"
"""


def spec_example() -> str:
    spec = (REPO_ROOT / "specs" / "30-testing-and-evals.md").read_text(encoding="utf-8")
    section = spec.split("### 3.5", 1)[1]
    found = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert found is not None
    return found.group(1)


def issues_of(text: str) -> tuple[ScenarioIssue, ...]:
    with pytest.raises(ScenarioError) as caught:
        load_scenario(dedent(text), source="case.yaml")
    return caught.value.issues


def only_issue(text: str) -> ScenarioIssue:
    [issue] = issues_of(text)
    return issue


def test_spec_example_loads() -> None:
    scenario = load_scenario(spec_example(), source="specs/30")
    assert scenario.id == "outbound_basic"
    assert scenario.match.to == "+14155550101"
    assert not scenario.inbound
    assert [type(step) for step in scenario.steps] == [
        WaitAgentSpeechStep,
        SayStep,
        WaitAgentSpeechStep,
        SayStep,
        DtmfStep,
        SilenceStep,
        ExpectHangupStep,
    ]
    assert scenario.answer is not None
    assert (scenario.answer.after_ms, scenario.answer.answered_by) == (1500, "human_residence")
    assert scenario.assertions.webhook_events == ("call.completed",)
    assert scenario.assertions.variables == {"name": "Ana"}
    assert scenario.transfer_target is not None
    assert isinstance(scenario.transfer_target.steps[0], SayStep)
    assert scenario.profile == "realistic"
    # Relative rule paths stay as written without a base directory.
    assert scenario.llm_rules == ("rules/outbound_basic.yaml",)


def test_step_defaults() -> None:
    scenario = load_scenario(spec_example())
    wait = scenario.steps[2]
    assert isinstance(wait, WaitAgentSpeechStep)
    assert (wait.wait_agent_speech.min_ms, wait.wait_agent_speech.then_silence_ms) == (0, 700)
    assert wait.wait_agent_speech.timeout_ms == 10_000
    say = scenario.steps[3]
    assert isinstance(say, SayStep)
    assert say.say.language == "en"
    dtmf = scenario.steps[4]
    assert isinstance(dtmf, DtmfStep)
    assert not dtmf.dtmf.inband


def test_fixture_files_load_with_rules_resolved_against_their_directory() -> None:
    outbound = load_scenario_file(FIXTURES / "outbound_voicemail.yaml")
    assert outbound.answer is not None
    assert (outbound.answer.answered_by, outbound.answer.beep_at_ms) == ("machine", 5200)
    assert outbound.llm_rules == ((FIXTURES / "rules" / "outbound_voicemail.yaml").resolve().as_posix(),)
    inbound = load_scenario_file(FIXTURES / "inbound_ivr.yaml")
    assert inbound.inbound
    assert inbound.match.inbound_from == "+14155550199"
    assert inbound.answer is None
    assert isinstance(inbound.steps[-1], HangupStep)
    assert inbound.llm_rules == ((FIXTURES / "rules" / "inbound_ivr.yaml").resolve().as_posix(),)
    assert all(Path(rule).is_file() for rule in (*outbound.llm_rules, *inbound.llm_rules))


def test_absolute_rule_paths_are_kept(tmp_path: Path) -> None:
    scenario = load_scenario(BASE + "llm_rules: [/srv/rules.yaml]\n", base_dir=tmp_path)
    assert scenario.llm_rules == ("/srv/rules.yaml",)


def test_unknown_top_level_key() -> None:
    issue = only_issue(BASE + "colour: red\n")
    assert issue == ScenarioIssue("colour", 4, 1, "Extra inputs are not permitted")


def test_bad_enum_value() -> None:
    issue = only_issue(BASE + "answer:\n  after_ms: 10\n  result: hung\n")
    assert (issue.path, issue.line, issue.column) == ("answer.result", 6, 3)
    assert "'answered', 'busy', 'no_answer', 'rejected' or 'failed'" in issue.message


def test_answered_without_ring_time_points_at_answer() -> None:
    issue = only_issue(BASE + "answer:\n  result: answered\n")
    assert issue == ScenarioIssue("answer", 4, 1, "`after_ms` is required when `result` is answered")


def test_ring_time_is_only_for_answered_calls() -> None:
    issue = only_issue(BASE + "answer: {result: busy, after_ms: 100}\n")
    assert (issue.path, issue.line) == ("answer", 4)
    assert "only allowed when `result` is answered" in issue.message


def test_amd_result_requires_an_answered_call() -> None:
    issue = only_issue(BASE + "answer: {result: no_answer, answered_by: machine}\n")
    assert issue.message == "`answered_by` is only allowed when `result` is answered"


def test_beep_requires_a_machine() -> None:
    issue = only_issue(BASE + "answer:\n  after_ms: 0\n  answered_by: human_business\n  beep_at_ms: 400\n")
    assert issue == ScenarioIssue(
        "answer", 4, 1, "`beep_at_ms` is only allowed when `answered_by` is machine"
    )


def test_machine_with_beep_is_valid() -> None:
    scenario = load_scenario(BASE + "answer: {after_ms: 0, answered_by: machine, beep_at_ms: 4200}\n")
    assert scenario.answer is not None
    assert scenario.answer.beep_at_ms == 4200


def test_match_needs_exactly_one_binding() -> None:
    both = only_issue('id: x\nmatch:\n  to: "+14155550101"\n  inbound_from: "+14155550102"\n')
    assert (both.path, both.line, both.column) == ("match", 2, 1)
    assert "exactly one of `to`" in both.message
    neither = only_issue("id: x\nmatch: {}\n")
    assert neither.path == "match"


def test_non_e164_destination() -> None:
    issue = only_issue("id: x\nmatch:\n  to: 4155550101\n")
    assert (issue.path, issue.line, issue.column) == ("match.to", 3, 3)
    issue = only_issue("id: x\nmatch:\n  to: '4155550101'\n")
    assert issue.message == "must be an E.164 number such as +14155550101, got '4155550101'"


def test_inbound_scenarios_must_not_set_answer() -> None:
    issue = only_issue('id: x\nmatch: {inbound_from: "+14155550102"}\nanswer: {after_ms: 10}\n')
    assert (issue.path, issue.line) == ("answer", 3)
    assert "platform answers inbound calls" in issue.message


def test_step_with_two_keys() -> None:
    text = BASE + "steps:\n  - say: {text: hi}\n  - say: {text: hello}\n    silence: {ms: 10}\n"
    issue = only_issue(text)
    assert issue.path == "steps[1]"
    assert (issue.line, issue.column) == (6, 5)
    assert issue.message.startswith("a step must have exactly one key, one of: say, wait_agent_speech")


def test_step_that_is_not_a_mapping() -> None:
    issue = only_issue(BASE + "steps:\n  - hangup\n")
    assert (issue.path, issue.line, issue.column) == ("steps[0]", 5, 5)
    assert "exactly one key" in issue.message


def test_unknown_step_key() -> None:
    issue = only_issue(BASE + "steps:\n  - sing: {song: anthem}\n")
    assert issue.path == "steps[0]"
    assert (issue.line, issue.column) == (5, 5)
    assert issue.message.startswith("unknown step 'sing', expected one of:")


def test_negative_silence_points_inside_the_step() -> None:
    issue = only_issue(BASE + "steps:\n  - silence:\n      ms: -5\n")
    assert issue == ScenarioIssue("steps[0].silence.ms", 6, 7, "Input should be greater than or equal to 0")


def test_missing_step_field_points_at_the_step_key() -> None:
    issue = only_issue(BASE + "steps:\n  - say: {language: en}\n")
    assert issue == ScenarioIssue("steps[0].say.text", 5, 5, "Field required")


def test_bad_dtmf_digits_and_unknown_step_field() -> None:
    issues = issues_of(BASE + "steps:\n  - dtmf: {digits: '12x'}\n  - say: {text: hi, volume: 3}\n")
    assert [(issue.path, issue.line) for issue in issues] == [
        ("steps[0].dtmf.digits", 5),
        ("steps[1].say.volume", 6),
    ]


def test_transfer_target_steps_are_validated() -> None:
    issue = only_issue(BASE + "transfer_target:\n  answer_after_ms: 10\n  steps:\n    - silence: {ms: x}\n")
    assert (issue.path, issue.line, issue.column) == ("transfer_target.steps[0].silence.ms", 7, 17)


def test_wrong_type_in_assertions() -> None:
    issue = only_issue(BASE + "assertions:\n  min_turns: many\n")
    assert (issue.path, issue.line, issue.column) == ("assertions.min_turns", 5, 3)
    assert "valid integer" in issue.message


def test_invalid_id_and_missing_match() -> None:
    issues = issues_of("id: Not Valid\n")
    assert [(issue.path, issue.line, issue.column) for issue in issues] == [("id", 1, 1), ("match", 1, 1)]
    assert issues[1].message == "Field required"


def test_yaml_syntax_error_reports_the_marker() -> None:
    issue = only_issue('id: "unterminated\nmatch: {}\n')
    assert (issue.path, issue.line, issue.column) == ("", 3, 1)
    assert issue.message == "invalid YAML: while scanning a quoted scalar: found unexpected end of stream"


def test_bad_indentation_is_a_syntax_error() -> None:
    issue = only_issue("id: x\n  match: {}\n")
    assert issue.line == 2
    assert issue.message.startswith("invalid YAML: ")


def test_duplicate_keys_are_rejected() -> None:
    issues = issues_of(BASE + "id: again\nanswer: {after_ms: 1, after_ms: 2}\n")
    assert [(issue.path, issue.line, issue.column, issue.message) for issue in issues] == [
        ("id", 4, 1, "duplicate key 'id'"),
        ("answer.after_ms", 5, 23, "duplicate key 'after_ms'"),
    ]


def test_root_must_be_a_mapping() -> None:
    assert only_issue("- id: x\n").message == "the scenario must be a mapping, got a sequence"
    assert only_issue("just text\n").message == "the scenario must be a mapping, got a scalar"
    assert only_issue("# nothing\n") == ScenarioIssue("", 1, 1, "the scenario document is empty")


def test_multiple_issues_are_reported_together() -> None:
    text = BASE + "profile: slow\nsteps:\n  - silence: {ms: -1}\n"
    with pytest.raises(ScenarioError) as caught:
        load_scenario(text, source="scenarios/multi.yaml")
    error = caught.value
    assert len(error.issues) == 2
    assert error.source == "scenarios/multi.yaml"
    assert str(error).splitlines() == [
        "scenarios/multi.yaml:4:1: profile: Input should be 'fast' or 'realistic'",
        "scenarios/multi.yaml:6:15: steps[0].silence.ms: Input should be greater than or equal to 0",
    ]


def test_issue_rendering_without_position_or_path() -> None:
    error = ScenarioError("body", [ScenarioIssue("id", None, None, "bad"), ScenarioIssue("", 2, 3, "worse")])
    assert str(error) == "body: id: bad\nbody:2:3: worse"


def test_models_reject_unknown_fields_in_json_input() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {"id": "x", "match": {"to": "+14155550101"}, "steps": [{"hangup": {"now": True}}]}
        )
    scenario = Scenario.model_validate(
        {"id": "x", "match": {"to": "+14155550101"}, "steps": [{"hangup": {}}]}
    )
    # Already-built steps validate again unchanged (model_copy / round trips).
    assert Scenario.model_validate(scenario) == scenario
    assert Scenario.model_validate(scenario.model_dump()) == scenario


def test_load_scenarios_by_id(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(BASE.replace("base", "first"), encoding="utf-8")
    (tmp_path / "b.yml").write_text(BASE.replace("base", "second"), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert sorted(load_scenarios(tmp_path)) == ["first", "second"]


def test_load_scenarios_rejects_duplicate_ids(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(BASE, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(BASE, encoding="utf-8")
    with pytest.raises(ScenarioError) as caught:
        load_scenarios(tmp_path)
    message = str(caught.value)
    assert str(tmp_path / "b.yaml") in message
    assert str(tmp_path / "a.yaml") in message
    assert "duplicate scenario id 'base'" in message


def test_load_scenario_file_reports_its_path(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text(BASE + "colour: red\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match=re.escape(f"{path}:4:1: colour: Extra inputs are not permitted")):
        load_scenario_file(path)


def test_json_schema_describes_the_dsl() -> None:
    schema = scenario_json_schema()
    assert {"id", "match", "steps", "assertions"} <= set(schema["properties"])
    assert schema["required"] == ["id", "match"]
    assert {"Say", "Dtmf", "Answer", "TransferTarget"} <= set(schema["$defs"])


def test_relative_wav_greeting_resolves_against_base_dir(tmp_path: Path) -> None:
    scenario = load_scenario(BASE + "greeting_audio: audio/Hello.WAV\n", base_dir=tmp_path)
    assert scenario.greeting_audio == (tmp_path / "audio" / "Hello.WAV").resolve().as_posix()


def test_text_greeting_and_absolute_wav_are_kept(tmp_path: Path) -> None:
    text = load_scenario(BASE + 'greeting_audio: "Hello? Who is this.wav-free"\n', base_dir=tmp_path)
    assert text.greeting_audio == "Hello? Who is this.wav-free"
    absolute = load_scenario(BASE + "greeting_audio: /srv/hello.wav\n", base_dir=tmp_path)
    assert absolute.greeting_audio == "/srv/hello.wav"
    unresolved = load_scenario(BASE + "greeting_audio: hello.wav\n")
    assert unresolved.greeting_audio == "hello.wav"
