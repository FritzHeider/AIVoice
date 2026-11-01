"""Generate the NANP area code -> state/province table read by `va_common.nanp` (specs/16 §6.2).

For every NPA 200-999 the script samples exchanges 200..999 (step 37) with the `phonenumbers` geocoder, keeps
valid numbers with a description, maps the description's region (the text after the last ", ", or the whole
description) to a two-letter USPS/Canada Post code, and keeps the majority code (ties: alphabetical). NPAs
without any mapped description (toll-free, non-geographic, other NANP countries) are omitted. Combined
regions ("Nova Scotia/Prince Edward Island") map to their first member.

Modes: default writes `packages/va_common/src/va_common/data/npa_states.json` (sorted keys, trailing
newline); `--check` exits 1 when the committed file differs from a regeneration; `--out PATH` picks another
file. A `phonenumbers` upgrade can change the output: regenerate deliberately and commit the result.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import phonenumbers
from phonenumbers import geocoder

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
OUT_FILE: Final = REPO_ROOT / "packages" / "va_common" / "src" / "va_common" / "data" / "npa_states.json"
NXX_START: Final = 200
NXX_STOP: Final = 1000
NXX_STEP: Final = 37
NPA_START: Final = 200
NPA_STOP: Final = 1000
SUBSCRIBER: Final = "5550"

US_STATES: Final[Mapping[str, str]] = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "Washington State": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
    "Washington D.C.": "DC",
    "District of Columbia": "DC",
    "Puerto Rico": "PR",
    "U.S. Virgin Islands": "VI",
    "Guam": "GU",
    "American Samoa": "AS",
    "Northern Mariana Islands": "MP",
}
CANADA_PROVINCES: Final[Mapping[str, str]] = {
    "Alberta": "AB",
    "British Columbia": "BC",
    "British Colombia": "BC",
    "Manitoba": "MB",
    "New Brunswick": "NB",
    "Newfoundland and Labrador": "NL",
    "Nova Scotia": "NS",
    "Prince Edward Island": "PE",
    "Ontario": "ON",
    "Quebec": "QC",
    "Saskatchewan": "SK",
    "Northwest Territories": "NT",
    "Nunavut": "NU",
    "Yukon": "YT",
}
NAMES: Final[Mapping[str, str]] = {**US_STATES, **CANADA_PROVINCES}
CODES: Final = frozenset(NAMES.values())


def region_code(description: str) -> str | None:
    """The two-letter code of a geocoder description's region, `None` when it is not a US/CA region."""
    region = description.rsplit(", ", 1)[-1].strip()
    if region in CODES:
        return region
    first = region.split("/", 1)[0].strip()
    return NAMES.get(region) or NAMES.get(first)


def _votes(npa: int) -> Counter[str]:
    votes: Counter[str] = Counter()
    for nxx in range(NXX_START, NXX_STOP, NXX_STEP):
        number = phonenumbers.parse(f"+1{npa}{nxx}{SUBSCRIBER}")
        if not phonenumbers.is_valid_number(number):
            continue
        code = region_code(geocoder.description_for_number(number, "en"))
        if code is not None:
            votes[code] += 1
    return votes


def build_table() -> dict[str, str]:
    """NPA (3 digits) -> majority region code, for every NPA with at least one mapped sample."""
    table: dict[str, str] = {}
    for npa in range(NPA_START, NPA_STOP):
        votes = _votes(npa)
        if votes:
            table[str(npa)] = min(votes.items(), key=lambda item: (-item[1], item[0]))[0]
    return table


def render(table: Mapping[str, str]) -> str:
    return json.dumps(dict(sorted(table.items())), indent=2, sort_keys=True) + "\n"


def write(out: Path) -> int:
    text = render(build_table())
    if out.exists() and out.read_text(encoding="utf-8") == text:
        print(f"unchanged {out}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def check(out: Path) -> int:
    if not out.exists() or out.read_text(encoding="utf-8") != render(build_table()):
        print(f"{out} differs from a regeneration; run scripts/generate_npa_states.py", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the NANP area code -> state table.")
    parser.add_argument("--check", action="store_true", help="exit 1 if the file differs from regeneration")
    parser.add_argument("--out", type=Path, default=OUT_FILE, help="output file")
    args = parser.parse_args(argv)
    return check(args.out) if args.check else write(args.out)


if __name__ == "__main__":
    sys.exit(main())
