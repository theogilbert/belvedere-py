"""Client-side ``\\copy`` support.

Postgres has no ``\\copy`` — psql parses it itself and issues a real
``COPY ... TO STDOUT`` or ``COPY ... FROM STDIN``, streaming the data to or
from a local file as it goes. grannos-py sits in the same position psql
normally does (a process on the user's machine, talking to the database), so
it plays the same role here.
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

_COPY_FROM_RE = re.compile(
    r"""
    ^\s*\\copy\s+
    (?P<target>.+?)
    \s+from\s+
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


@dataclass(frozen=True)
class CopyFromCommand:
    target: str
    """Table name, optionally with a column list, to copy into."""
    path: str
    """Local filesystem path to read the input from."""
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


def parse_copy_from(query: str) -> CopyFromCommand | None:
    """Parse a ``\\copy ... FROM 'path'`` command, or return None if *query* isn't one."""
    match = _COPY_FROM_RE.match(query)
    if match is None:
        return None
    return CopyFromCommand(
        target=match.group("target").strip(),
        path=match.group("path").replace("''", "'"),
        options=match.group("options").strip(),
    )


def build_copy_to_statement(cmd: CopyToCommand) -> str:
    stmt = f"COPY {cmd.source} TO STDOUT"
    if cmd.options:
        stmt += f" {cmd.options}"
    return stmt


def build_copy_from_statement(cmd: CopyFromCommand) -> str:
    stmt = f"COPY {cmd.target} FROM STDIN"
    if cmd.options:
        stmt += f" {cmd.options}"
    return stmt
