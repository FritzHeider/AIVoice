from __future__ import annotations

import re
from pathlib import Path

import pytest

from va_common.errors import ALTERNATE_STATUSES, CODES, AppError, status_for

CATALOG = Path(__file__).resolve().parents[3] / "specs" / "31-error-catalog.md"
ROW_RE = re.compile(r"^\| `(?P<code>[A-Z_]+)` \| (?P<statuses>[0-9]{3}(?:/[0-9]{3})*) \|")


def _catalog_table() -> dict[str, list[int]]:
    """Parse the specs/31 §3 table: code -> statuses in the order the cell lists them."""
    lines = CATALOG.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 3. "))
    end = next(i for i, line in enumerate(lines) if line.startswith("## 3a. "))
    table: dict[str, list[int]] = {}
    for line in lines[start:end]:
        match = ROW_RE.match(line)
        if match:
            assert match["code"] not in table, f"duplicate row {match['code']}"
            table[match["code"]] = [int(s) for s in match["statuses"].split("/")]
    return table


TABLE = _catalog_table()


def test_catalog_table_parsed_completely() -> None:
    assert len(TABLE) == 62


def test_codes_match_catalog_exactly() -> None:
    assert list(CODES) == list(TABLE)


@pytest.mark.parametrize("code", list(TABLE))
def test_code_statuses_match_catalog(code: str) -> None:
    statuses = TABLE[code]
    assert CODES[code] == statuses[0]
    assert {CODES[code]} | ALTERNATE_STATUSES.get(code, frozenset()) == set(statuses)


def test_alternates_only_for_catalog_codes() -> None:
    assert set(ALTERNATE_STATUSES) <= set(CODES)


def test_status_for() -> None:
    assert status_for("NOT_FOUND") == 404
    with pytest.raises(ValueError, match="unknown error code"):
        status_for("NOPE")


def test_app_error_defaults_status_from_catalog() -> None:
    error = AppError("RATE_LIMITED", "Slow down")
    assert error.http_status == 429
    assert error.code == "RATE_LIMITED"
    assert error.message == "Slow down"
    assert error.field is None
    assert error.details == {}
    assert error.headers == {}


def test_app_error_headers_are_not_in_the_error_item() -> None:
    error = AppError("AUTH_LOCKED", "Locked", headers={"Retry-After": "30"})
    assert error.headers == {"Retry-After": "30"}
    assert error.to_error_item() == {"error": "AUTH_LOCKED", "message": "Locked"}


def test_app_error_accepts_alternate_status() -> None:
    assert AppError("KB_ERROR", "Embedding failed", 500).http_status == 500
    assert AppError("KB_ERROR", "Bad file").http_status == 400


def test_app_error_rejects_wrong_status_and_unknown_code() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        AppError("KB_ERROR", "x", 404)
    with pytest.raises(ValueError, match="unknown error code"):
        AppError("NOT_A_CODE", "x")


def test_to_error_item_omits_absent_fields() -> None:
    assert AppError("NOT_FOUND", "Missing").to_error_item() == {"error": "NOT_FOUND", "message": "Missing"}


def test_to_error_item_includes_field_and_details() -> None:
    error = AppError(
        "CARRIER_ERROR", "Telnyx failed", field="phone_number", details={"telnyx_errors": [{"code": "10001"}]}
    )
    assert error.to_error_item() == {
        "error": "CARRIER_ERROR",
        "message": "Telnyx failed",
        "field": "phone_number",
        "details": {"telnyx_errors": [{"code": "10001"}]},
    }


def test_details_may_be_passed_positionally() -> None:
    error = AppError("KB_ERROR", "m", 500, {"code": "x"})
    assert error.http_status == 500
    assert error.details == {"code": "x"}
    assert error.field is None


def test_details_are_copied() -> None:
    details = {"a": 1}
    error = AppError("CONFLICT", "Busy", details=details)
    details["b"] = 2
    assert error.details == {"a": 1}
    error.to_error_item()["details"]["c"] = 3
    assert error.details == {"a": 1}


def test_str_and_repr_do_not_leak_details() -> None:
    error = AppError("INVALID_INPUT", "Bad value", details={"secret": "s3cr3t"})
    assert str(error) == "INVALID_INPUT: Bad value"
    assert "s3cr3t" not in repr(error)
    assert repr(error) == "AppError(code='INVALID_INPUT', message='Bad value', http_status=400)"
