#!/usr/bin/env python3
"""Fail CI when database.py adds a tightening constraint without either
auto-coverage (_migrate_enforce_unique_indexes) or a sibling _migrate_*
function in the same diff.

Compares `database.py` at HEAD to `origin/main`. Tightenings we scan for:

  - UNIQUE INDEX            — auto-covered by _migrate_enforce_unique_indexes
  - CHECK constraints       — require a per-case _migrate_* helper
  - NOT NULL w/o DEFAULT    — require a per-case _migrate_* helper
  - MIGRATION_COLUMNS additions with NOT NULL and no DEFAULT

Pass conditions (any):
  1. No new tightenings in the diff.
  2. Every non-UNIQUE tightening is accompanied by ≥1 new _migrate_* function.

Exit 0 = pass, 1 = fail. Prints an actionable report either way.
"""
from __future__ import annotations

import re
import subprocess
import sys


def git_show_main(path: str) -> str:
    """Return the contents of `path` on origin/main, or empty if unavailable."""
    try:
        return subprocess.check_output(
            ["git", "show", f"origin/main:{path}"],
            stderr=subprocess.DEVNULL,
        ).decode()
    except subprocess.CalledProcessError:
        return ""


def extract_block(code: str, name: str) -> str:
    """Grab SCHEMA = '''...''' or MIGRATION_COLUMNS = [...]."""
    triple = re.search(rf'{name}\s*=\s*"""(.*?)"""', code, re.DOTALL)
    if triple:
        return triple.group(1)
    bracket = re.search(rf"{name}\s*=\s*\[(.*?)\]", code, re.DOTALL)
    if bracket:
        return bracket.group(1)
    return ""


def new_lines(old: str, new: str) -> list[str]:
    old_set = {line.strip() for line in old.splitlines() if line.strip()}
    return [
        line.strip()
        for line in new.splitlines()
        if line.strip() and line.strip() not in old_set
    ]


def detect_schema_tightenings(old_schema: str, new_schema: str) -> list[tuple[str, str]]:
    """Return [(category, line), ...] for each newly-introduced tightening."""
    hits: list[tuple[str, str]] = []
    for line in new_lines(old_schema, new_schema):
        low = line.lower()
        if "create unique index" in low:
            hits.append(("unique_index", line))
        elif re.search(r"\bcheck\s*\(", low):
            hits.append(("check", line))
        elif "not null" in low and "default" not in low and "primary key" not in low:
            # NOT NULL on a column in CREATE TABLE: fine if the table is fresh,
            # breaking if the table pre-exists with NULL rows. Err on the side
            # of flagging and letting the author wave it off with a migration.
            hits.append(("not_null", line))
    return hits


def detect_migration_column_tightenings(old_mig: str, new_mig: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    pattern = re.compile(r'\(\s*"(\w+)"\s*,\s*"([^"]+)"\s*\)')
    old_tuples = set(pattern.findall(old_mig))
    for m in pattern.finditer(new_mig):
        col, sqltype = m.group(1), m.group(2)
        if (col, sqltype) in old_tuples:
            continue
        low = sqltype.lower()
        if "not null" in low and "default" not in low:
            hits.append(("migration_column_not_null", f'("{col}", "{sqltype}")'))
        if re.search(r"\bcheck\s*\(", low):
            hits.append(("migration_column_check", f'("{col}", "{sqltype}")'))
    return hits


def new_migrate_functions(old_code: str, new_code: str) -> set[str]:
    fn_re = re.compile(r"async\s+def\s+(_migrate_\w+)")
    old = set(fn_re.findall(old_code))
    new = set(fn_re.findall(new_code))
    return new - old


def main() -> int:
    old_code = git_show_main("database.py")
    try:
        new_code = open("database.py").read()
    except FileNotFoundError:
        print("database.py not found at HEAD — skipping.")
        return 0
    if not old_code:
        # First commit, or origin/main missing — nothing to diff.
        return 0

    schema_hits = detect_schema_tightenings(
        extract_block(old_code, "SCHEMA"),
        extract_block(new_code, "SCHEMA"),
    )
    migration_hits = detect_migration_column_tightenings(
        extract_block(old_code, "MIGRATION_COLUMNS"),
        extract_block(new_code, "MIGRATION_COLUMNS"),
    )
    hits = schema_hits + migration_hits
    if not hits:
        return 0

    unique_only = all(cat == "unique_index" for cat, _ in hits)
    if unique_only:
        print("Schema diff vs origin/main:")
        for _, line in hits:
            print(f"  + UNIQUE INDEX  {line}")
        print("\nAuto-covered by _migrate_enforce_unique_indexes — no action needed.")
        return 0

    # The generic dedupe helper covers UNIQUE only — don't count it as an
    # acknowledgement for CHECK/NOT NULL tightenings.
    GENERIC = {"_migrate_enforce_unique_indexes"}
    new_migrators = new_migrate_functions(old_code, new_code) - GENERIC
    print("Schema diff vs origin/main introduces tightenings:")
    for category, line in hits:
        print(f"  + [{category}]  {line}")
    print()
    if new_migrators:
        print(
            "Accepted — this diff also adds new migrator(s): "
            + ", ".join(sorted(new_migrators))
        )
        return 0

    print(
        "FAIL: no new _migrate_* helper accompanies these tightenings.\n"
        "Add an `async def _migrate_<topic>(db)` in database.py that backfills\n"
        "or cleans existing rows, and call it from init_db() *before*\n"
        "executescript(SCHEMA). This keeps existing DBs bootable after the\n"
        "constraint lands."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
