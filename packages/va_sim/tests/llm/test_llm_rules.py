"""FakeLLM rules engine: YAML loading with positioned issues, matching, ordering, call_index counters."""

from __future__ import annotations

from pathlib import Path

import pytest

from va_sim.llm.rules import (
    RUNTIME_SOURCE,
    InjectedError,
    MatchContext,
    Rule,
    RuleBook,
    RulesError,
    RuleToolCall,
    load_rule_file,
    load_rules,
)

FULL = """\
rules:
  - id: greet-restaurant
    priority: 10
    match:
      system_contains: ["[[SCENARIO:restaurant]]"]
      last_user_regex: "(?i)reservation|book a table"
      tool_name: null
      model: null
      call_index: null
      call_id: call-1
    respond:
      content: "Sure! For how many people?"
      tool_calls:
        - name: book
          arguments: {party: 4}
        - name: broken
          arguments: '{"party": '
      ttft_ms: 120
      error: {status: 503, body: {detail: down}, headers: {retry-after: "2"}}
      delay_ms: 5
"""


def context(  # noqa: PLR0913 - keyword-only overrides of a default context
    *,
    model: str = "m",
    system: str = "",
    user: str | None = "hello",
    forced: str | None = None,
    tools: tuple[str, ...] = (),
    call_id: str | None = None,
) -> MatchContext:
    return MatchContext(
        model=model,
        system_text=system,
        last_user_text=user,
        forced_tool=forced,
        tool_names=tools,
        call_id=call_id,
    )


def book_of(text: str, source: str = RUNTIME_SOURCE) -> RuleBook:
    book = RuleBook()
    book.add(load_rules(text).rules, source=source)
    return book


def winner(book: RuleBook, ctx: MatchContext) -> str | None:
    rule = book.match(ctx)
    return rule.id if rule is not None else None


def test_every_key_loads() -> None:
    (rule,) = load_rules(FULL).rules
    assert rule.id == "greet-restaurant"
    assert rule.priority == 10
    assert rule.match.system_contains == ("[[SCENARIO:restaurant]]",)
    assert rule.match.call_id == "call-1"
    assert rule.respond.tool_calls == (
        RuleToolCall(name="book", arguments={"party": 4}),
        RuleToolCall(name="broken", arguments='{"party": '),
    )
    assert rule.respond.error == InjectedError(
        status=503, body={"detail": "down"}, headers={"retry-after": "2"}
    )
    assert (rule.respond.ttft_ms, rule.respond.delay_ms) == (120, 5)


def test_minimal_rule_defaults() -> None:
    (rule,) = load_rules("rules:\n  - id: r\n").rules
    assert rule == Rule(id="r")
    assert rule.respond.tool_calls == ()


@pytest.mark.parametrize(
    ("text", "path", "line", "fragment"),
    [
        ("rules:\n  - id: r\n    match: {colour: red}\n", "rules[0].match.colour", 3, "Extra inputs"),
        (
            "rules:\n  - id: r\n    match:\n      last_user_regex: '(unclosed'\n",
            "rules[0].match.last_user_regex",
            4,
            "invalid regular expression",
        ),
        ("rules:\n  - id: r\n    priority: high\n", "rules[0].priority", 3, "integer"),
        (
            "rules:\n  - id: r\n    respond: {error: {status: 200}}\n",
            "rules[0].respond.error.status",
            3,
            "400",
        ),
        ("rules:\n  - id: r\n    match: {call_index: -1}\n", "rules[0].match.call_index", 3, "0"),
        ("rules:\n  - id: a\n  - id: a\n", "rules", 1, "duplicate rule id 'a'"),
        ("rules:\n  - priority: 1\n", "rules[0].id", 2, "Field required"),
    ],
)
def test_invalid_documents_report_positioned_issues(text: str, path: str, line: int, fragment: str) -> None:
    with pytest.raises(RulesError) as caught:
        load_rules(text, source="rules.yaml")
    (issue,) = caught.value.issues
    assert (issue.path, issue.line) == (path, line)
    assert fragment in issue.message
    assert str(caught.value).startswith(f"rules.yaml:{line}:")


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "the rules document is empty"),
        ("- a\n", "the rules must be a mapping"),
        ("rules: [\n", "invalid YAML"),
    ],
)
def test_unparseable_documents(text: str, fragment: str) -> None:
    with pytest.raises(RulesError, match=fragment):
        load_rules(text)


def test_rule_file_loads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("rules:\n  - id: disk\n", encoding="utf-8")
    assert [rule.id for rule in load_rule_file(path).rules] == ["disk"]


def test_priority_then_registration_order() -> None:
    book = RuleBook()
    book.add(load_rules("rules:\n  - id: low\n  - id: high\n    priority: 5\n").rules, source="a.yaml")
    book.add(load_rules("rules:\n  - id: later-high\n    priority: 5\n").rules, source=RUNTIME_SOURCE)
    assert [entry.rule.id for entry in book.ordered()] == ["high", "later-high", "low"]
    assert winner(book, context()) == "high"


def test_conditions() -> None:
    book = book_of(
        """\
rules:
  - id: system
    match: {system_contains: ["[[A]]", "[[B]]"]}
  - id: regex
    match: {last_user_regex: "(?i)book a table"}
  - id: tool
    match: {tool_name: book}
  - id: model
    match: {model: special}
  - id: call
    match: {call_id: c-9}
"""
    )
    assert winner(book, context(system="x [[A]] y [[B]]")) == "system"
    assert winner(book, context(system="[[A]] only")) is None
    assert winner(book, context(user="Can I BOOK A TABLE?")) == "regex"
    assert winner(book, context(user=None)) is None
    assert winner(book, context(tools=("lookup", "book"))) == "tool"
    assert winner(book, context(tools=("book",), forced="lookup")) is None
    assert winner(book, context(tools=("book", "lookup"), forced="book")) == "tool"
    assert winner(book, context(model="special")) == "model"
    assert winner(book, context(call_id="c-9")) == "call"
    assert winner(book, context(call_id="c-1")) is None


def test_call_index_counts_per_call_id() -> None:
    book = book_of(
        """\
rules:
  - id: first
    match: {call_index: 0}
    respond: {content: one}
  - id: second
    match: {call_index: 1}
    respond: {content: two}
"""
    )
    sequence = [winner(book, context(call_id=call)) for call in ("a", "b", "a", "b", "a", None, None)]
    assert sequence == ["first", "first", "second", "second", None, "first", "second"]


def test_counters_advance_for_rules_that_did_not_win() -> None:
    book = book_of(
        """\
rules:
  - id: always
    priority: 1
  - id: third
    match: {call_index: 2}
"""
    )
    assert [winner(book, context()) for _ in range(3)] == ["always", "always", "always"]
    book.remove("always")
    # "third" was counted on all three requests above, so its index 2 is already gone.
    assert winner(book, context()) is None


def test_duplicate_ids_replace_and_move_to_the_end() -> None:
    book = book_of("rules:\n  - id: a\n    respond: {content: old}\n  - id: b\n")
    book.add(load_rules("rules:\n  - id: a\n    respond: {content: new}\n").rules, source=RUNTIME_SOURCE)
    assert [entry.rule.id for entry in book.ordered()] == ["b", "a"]
    assert book.ordered()[1].rule.respond.content == "new"
    assert len(book) == 2


def test_reset_keeps_file_rules_and_clears_counters() -> None:
    book = RuleBook()
    book.add(load_rules("rules:\n  - id: file\n    match: {call_index: 0}\n").rules, source="a.yaml")
    book.add(load_rules("rules:\n  - id: runtime\n").rules, source=RUNTIME_SOURCE)
    assert winner(book, context()) == "file"
    assert winner(book, context()) == "runtime"
    book.reset()
    assert [entry.source for entry in book.ordered()] == ["a.yaml"]
    assert winner(book, context()) == "file"
    assert book.ordered()[0].to_json()["source"] == "a.yaml"
    assert not book.remove("missing")
    book.clear_runtime()
    assert len(book) == 1
