"""Client-side ``LOAD`` support.

Oracle has no server-side counterpart to psql's ``\\copy ... FROM``: SQL*Loader
is a separate binary, and an external table reads the file on the *database*
server rather than on the user's machine. SQLcl closes that gap from the client
side — it parses the delimited file itself and sends ordinary INSERTs — and
grannos-py sits in exactly that position (a process on the user's machine,
talking to the database), so it plays the same role here.
"""

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, TextIO

from ..base import DriverError

DEFAULT_BATCH = 1000
"""Rows sent per array-DML round-trip when the statement doesn't say."""

_LOAD_RE = re.compile(
    r"""
    ^\s*load\s+(?:table\s+)?
    (?P<table>[^\s'(]+)
    \s*(?P<columns>\([^)]*\))?
    \s+(?:from\s+)?
    '(?P<path>(?:[^']|'')*)'
    \s*(?P<options>.*?)\s*;?\s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_LOAD_PREFIX_RE = re.compile(r"^\s*load\b", re.IGNORECASE)
"""A statement starting with LOAD can only be meant as one: no Oracle SQL does."""

_UNQUOTED_PATH_RE = re.compile(
    r"""
    ^\s*load\s+(?:table\s+)?
    [^\s'(]+
    \s*(?:\([^)]*\))?
    \s+(?:from\s+)?
    (?P<path>[^\s'(][^\s]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SYNTAX = "LOAD <table> [(column_list)] FROM '<path>' [(options)]"

_IDENTIFIER_RE = re.compile(r'^(?:[A-Za-z_$#][A-Za-z0-9_$#]*|"[^"]+")$')

_OPTION_RE = re.compile(
    r"""
    ^(?P<name>[A-Za-z_]+)
    (?:\s+(?:'(?P<text>(?:[^']|'')*)'|(?P<word>[^\s']+)))?$
    """,
    re.VERBOSE | re.DOTALL,
)

_ESCAPES = {"\\t": "\t", "\\0": "\0"}
"""Two-character escapes accepted for DELIMITER, which no one can type literally."""


@dataclass(frozen=True)
class LoadOptions:
    """The ``(...)`` clause of a ``LOAD``, parsed."""

    delimiter: str = ","
    quote: str = '"'
    header: bool = False
    """First row (after SKIP) names the target columns rather than carrying data."""
    skip: int = 0
    null: str | None = None
    """Field value to load as NULL. Empty fields already load as NULL, Oracle
    storing a zero-length string that way in every column type."""
    batch: int = DEFAULT_BATCH
    encoding: str = "utf-8"
    date_format: str | None = None
    """Oracle format model the file's datetime values are written in, applied to
    the session for the duration of the load. None leaves the session's own."""
    timestamp_format: str | None = None
    """As :attr:`date_format`, for TIMESTAMP columns; defaults to it."""


@dataclass(frozen=True)
class LoadCommand:
    table: str
    """Target table, optionally schema-qualified."""
    columns: list[str] | None
    """Explicit column list, or None to take it from the header or table order."""
    path: str
    """Local filesystem path to read the rows from."""
    options: LoadOptions = field(default_factory=LoadOptions)


def parse_load(query: str) -> LoadCommand | None:
    """Parse a ``LOAD ... FROM 'path'`` command, or return None if *query* isn't one.

    Raises:
        DriverError: If *query* is a LOAD but its target or options are malformed.
    """
    match = _LOAD_RE.match(query)
    if match is None:
        _reject_malformed_load(query)
        return None

    table = match.group("table")
    for part in table.split("."):
        validate_identifier(part, "table name")

    columns = None
    if match.group("columns") is not None:
        names = [c.strip() for c in match.group("columns")[1:-1].split(",")]
        columns = [validate_identifier(n, "column name") for n in names if n]

    return LoadCommand(
        table=table,
        columns=columns,
        path=match.group("path").replace("''", "'"),
        options=parse_options(match.group("options")),
    )


def _reject_malformed_load(query: str) -> None:
    """Raise if *query* was meant as a LOAD but didn't parse as one.

    Nothing else it could be: no Oracle statement starts with LOAD, so letting
    it through means the user reads ``ORA-00900: invalid SQL statement``, which
    says nothing about the quotes they left off the path.
    """
    if not _LOAD_PREFIX_RE.match(query):
        return

    # An odd quote count means one is missing rather than all of them, so the
    # path the other branch would quote back would be a fragment of the tail.
    if query.count("'") % 2:
        raise DriverError(
            f"the file path has an unterminated quote — syntax is: {_SYNTAX}"
        )

    unquoted = _UNQUOTED_PATH_RE.match(query)
    if unquoted is not None and "'" not in unquoted.group("path"):
        path = unquoted.group("path").rstrip(";")
        raise DriverError(
            f"the file path must be in single quotes: FROM '{path}' "
            f"— syntax is: {_SYNTAX}"
        )
    raise DriverError(f"malformed LOAD — syntax is: {_SYNTAX}")


def validate_identifier(name: str, what: str) -> str:
    """Return *name* if it is a bare or double-quoted Oracle identifier, else raise.

    Column names can reach the generated INSERT from a file's header row, which
    is not the user's own SQL — nothing that isn't an identifier may get there.
    """
    if not _IDENTIFIER_RE.match(name):
        raise DriverError(f"{what} {name!r} is not a valid Oracle identifier")
    return name


def parse_options(text: str) -> LoadOptions:
    """Parse a parenthesised ``(FORMAT csv, HEADER, ...)`` option clause."""
    text = text.strip()
    if not text:
        return LoadOptions()
    if not (text.startswith("(") and text.endswith(")")):
        raise DriverError(
            "LOAD options must be parenthesised, e.g. (FORMAT csv, HEADER)"
        )

    values: dict[str, Any] = {}
    for item in _split_items(text[1:-1]):
        match = _OPTION_RE.match(item)
        if match is None:
            raise DriverError(f"malformed LOAD option: {item!r}")
        name = match.group("name").lower()
        text_value = match.group("text")
        value = (
            text_value.replace("''", "'")
            if text_value is not None
            else match.group("word")
        )
        _apply_option(values, name, value)
    return LoadOptions(**values)


def _apply_option(values: dict[str, Any], name: str, value: str | None) -> None:
    match name:
        case "format":
            if value is None or value.lower() != "csv":
                raise DriverError("only FORMAT csv is supported")
        case "header":
            _reject_value(name, value)
            values["header"] = True
        case "delimiter" | "quote":
            char = _ESCAPES.get(value or "", value)
            if not char or len(char) != 1:
                raise DriverError(f"{name.upper()} must be a single character")
            values[name] = char
        case "null" | "encoding":
            if value is None:
                raise DriverError(f"{name.upper()} needs a value")
            values[name] = value
        case "dateformat" | "timestampformat":
            if value is None:
                raise DriverError(f"{name.upper()} needs a value")
            values[name.replace("format", "_format")] = value
        case "skip" | "batch":
            number = int(value) if value is not None and value.isdigit() else -1
            if number < 0 or (name == "batch" and number < 1):
                raise DriverError(f"{name.upper()} must be a positive whole number")
            values[name] = number
        case _:
            raise DriverError(
                f"unknown LOAD option {name.upper()!r} — valid options are "
                "FORMAT, HEADER, DELIMITER, QUOTE, NULL, SKIP, BATCH, ENCODING, "
                "DATEFORMAT, TIMESTAMPFORMAT"
            )


def _reject_value(name: str, value: str | None) -> None:
    if value is not None:
        raise DriverError(f"{name.upper()} takes no value")


def _split_items(text: str) -> list[str]:
    """Split an option list on its top-level commas, ignoring quoted ones."""
    items: list[str] = []
    buf: list[str] = []
    in_quote = False
    for char in text:
        if char == "'":
            in_quote = not in_quote
        elif char == "," and not in_quote:
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(char)
    items.append("".join(buf).strip())
    return [item for item in items if item]


def read_rows(
    f: TextIO, options: LoadOptions
) -> tuple[list[str] | None, Iterator[tuple[int, list[str | None]]]]:
    """Return the header row (if any) and a lazy iterator over the data rows.

    Empty fields are left as empty strings rather than read back as None:
    Oracle stores a zero-length string as NULL in every column type, so the
    quoted/unquoted distinction ``csv.QUOTE_NOTNULL`` draws cannot reach a
    table — and the reader honours that flag only from Python 3.13. A file
    that marks its nulls some other way is served by the ``NULL`` option.
    """
    reader = csv.reader(
        f,
        delimiter=options.delimiter,
        quotechar=options.quote,
    )
    for _ in range(options.skip):
        next(reader, None)

    header = None
    if options.header:
        header = [(name or "").strip() for name in next(reader, [])]

    return header, _data_rows(reader, options)


def _data_rows(
    reader: Any, options: LoadOptions
) -> Iterator[tuple[int, list[str | None]]]:
    """Yield each data row paired with the file line it ends on.

    The line, not the row's ordinal: a header and any SKIPped lines come before
    the first row, and a quoted field may itself span lines, so an ordinal
    sends the reader to the wrong place in the file.
    """
    for row in reader:
        if not row:  # a blank line in the middle of the file
            continue
        values: list[str | None] = list(row)
        if options.null is not None:
            values = [None if v == options.null else v for v in values]
        yield reader.line_num, values


def build_insert_statement(table: str, columns: list[str] | None, width: int) -> str:
    """Build the INSERT a batch of rows is bound to.

    With no column list Oracle falls back to the table's own column order,
    which is what ``\\copy`` and SQL*Loader do with a headerless file too.
    """
    binds = ", ".join(f":{i}" for i in range(1, width + 1))
    target = f"{table} ({', '.join(columns)})" if columns else table
    return f"INSERT INTO {target} VALUES ({binds})"
