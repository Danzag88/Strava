"""Split project SQL scripts into single statements."""

from __future__ import annotations


def sql_statements(sql_text: str) -> list[str]:
    kept_lines: list[str] = []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        kept_lines.append(line)
    return [part.strip() for part in "\n".join(kept_lines).split(";") if part.strip()]
