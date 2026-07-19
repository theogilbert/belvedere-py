"""Client-side ``\\copy`` support.

Postgres has no ``\\copy`` — psql parses it itself and issues a real
``COPY ... TO STDOUT``, streaming the result to a local file as it arrives.
grannos-py sits in the same position psql normally does (a process on the
user's machine, talking to the database), so it plays the same role here.
"""

import re
from dataclasses import dataclass

_COPY_TO_RE = re.compile(
    r"""
    ^\s*\\copy\s+
    (?P<source>\(.*\)|[^\s(]+)
    \s+to\s+
    '(?P<path>(?:[^']|'')*)'
    \s*(?P<options>.*?)\s*;?\s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class CopyToCommand:
    source: str
    """Table name or parenthesized query to copy from."""
    path: str
    """Local filesystem path to write the result to."""
    options: str
    """Trailing ``[WITH] (...)`` clause, verbatim, or empty."""


def parse_copy_to(query: str) -> CopyToCommand | None:
    """Parse a ``\\copy ... TO 'path'`` command, or return None if *query* isn't one."""
    match = _COPY_TO_RE.match(query)
    if match is None:
        return None
    return CopyToCommand(
        source=match.group("source").strip(),
        path=match.group("path").replace("''", "'"),
        options=match.group("options").strip(),
    )


def build_copy_to_statement(cmd: CopyToCommand) -> str:
    stmt = f"COPY {cmd.source} TO STDOUT"
    if cmd.options:
        stmt += f" {cmd.options}"
    return stmt
