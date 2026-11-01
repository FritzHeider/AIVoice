"""In-place ``KEY=value`` updates of a dotenv file (shared by the bootstrap and sim public-key scripts)."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = ["update_env_file"]

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*?)(?P<comment>\s+#.*)?$")


def _replace_values(lines: list[str], values: Mapping[str, str]) -> set[str]:
    """Rewrite matching ``KEY=...`` lines in place (keeping comments and line endings); the keys seen."""
    seen: set[str] = set()
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        match = _ASSIGNMENT.match(body)
        if match is None or match["key"] not in values:
            continue
        key = match["key"]
        seen.add(key)
        lines[index] = f"{key}={values[key]}{match['comment'] or ''}{line[len(body) :]}"
    return seen


def update_env_file(path: Path, values: Mapping[str, str]) -> bool:
    """Set ``values`` in the env file at ``path`` (created when absent); ``True`` when the file was written.

    Existing assignments keep their trailing ``  # comment``; missing keys are appended; every other line is
    kept byte for byte. The new content replaces the file atomically; an unchanged file is not touched.
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = original.splitlines(keepends=True)
    seen = _replace_values(lines, values)
    missing = [f"{key}={value}\n" for key, value in values.items() if key not in seen]
    if missing and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    updated = "".join(lines + missing)
    if updated == original:
        return False
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        if path.exists():
            Path(temp_name).chmod(path.stat().st_mode & 0o777)
        Path(temp_name).replace(path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return True
