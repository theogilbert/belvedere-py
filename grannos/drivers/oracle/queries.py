"""Oracle query functions — one per SQL query, with typed return values."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from oracledb import AsyncConnection, AsyncCursor

from ...protocol import IndexDescription, IndexKeyField, LobPlaceholder, TableReference
from ..base import SAMPLE_SCAN_ROWS

# Pre-12c fallback: ORACLE_MAINTAINED column doesn't exist before 12.1, so we
# exclude known system schemas by name instead.
_PRE12_SYSTEM_SCHEMAS_SQL = ", ".join(
    f"'{u}'"
    for u in sorted(
        {
            "ANONYMOUS",
            "APEX_030200",
            "APEX_040000",
            "APPQOSSYS",
            "AUDSYS",
            "CTXSYS",
            "DBSFWUSER",
            "DBSNMP",
            "DIP",
            "DVF",
            "DVSYS",
            "EXFSYS",
            "FLOWS_FILES",
            "GGSYS",
            "GSMADMIN_INTERNAL",
            "LBACSYS",
            "MDDATA",
            "MDSYS",
            "OJVMSYS",
            "OLAPSYS",
            "ORACLE_OCM",
            "ORDDATA",
            "ORDPLUGINS",
            "ORDSYS",
            "OUTLN",
            "SI_INFORMTN_SCHEMA",
            "SYS",
            "SYSBACKUP",
            "SYSDG",
            "SYSKM",
            "SYSRAC",
            "SYSTEM",
            "WMSYS",
            "XDB",
            "XS$NULL",
        }
    )
)


_cache_stores: list[WeakKeyDictionary] = []
"""All per-connection cache dicts, registered at decoration time for bulk invalidation."""


def _conn_cache(fn):
    """Cache async query results per connection, keyed on positional args after *conn*.

    Each distinct *conn* object gets its own cache dict; entries are released
    when the connection is garbage-collected.
    """
    _store: WeakKeyDictionary = WeakKeyDictionary()
    _cache_stores.append(_store)

    @wraps(fn)
    async def wrapper(conn, *args):
        if not isinstance(conn, AsyncConnection):
            raise TypeError(
                f"{fn.__name__}: expected AsyncConnection, got {type(conn).__name__}"
            )
        cache = _store.setdefault(conn, {})
        if args not in cache:
            cache[args] = await fn(conn, *args)
        return cache[args]

    return wrapper


def invalidate_cache(conn: AsyncConnection) -> None:
    """Drop all cached query results for *conn*.

    Call after any DDL or DML so subsequent metadata queries re-hit the database.
    """
    for store in _cache_stores:
        store.pop(conn, None)


@dataclass
class ColumnDetail:
    """Column metadata returned by :func:`fetch_column_details`."""

    name: str
    """Column name."""
    type: str
    """Oracle data type string."""
    nullable: bool
    """Whether the column allows NULL."""
    default: str | None
    """Default expression, stripped of surrounding whitespace; None if not set."""
    char_length: int | None
    """``ALL_TAB_COLUMNS.CHAR_LENGTH`` verbatim — meaningful only for character
    types (VARCHAR2, VARCHAR, CHAR, ...); 0/meaningless for others."""
    byte_length: int | None
    """``ALL_TAB_COLUMNS.DATA_LENGTH`` verbatim — the storage size in bytes for
    any type. For character columns, differs from ``char_length`` when the
    column was declared with BYTE semantics (or CHAR semantics under a
    multi-byte charset)."""


@dataclass
class IndexMeta:
    """Index-level metadata returned by fetch_index_meta* functions."""

    name: str
    """Index name."""
    owner: str
    """Schema that owns the index (may differ from the table owner)."""
    index_type: str | None
    """Index storage type in lowercase (e.g. ``"normal"``, ``"bitmap"``); None if unknown."""
    unique: bool
    """Whether the index enforces uniqueness."""
    visible: bool
    """Whether the optimiser considers this index (False for INVISIBLE indexes)."""
    generated: bool
    """True for system-generated constraint-backing indexes; their DDL is part of CREATE TABLE."""


async def fetch_explain_plan(cur: AsyncCursor) -> list[str]:
    """Return the execution plan of the last EXPLAIN PLAN query.

    Returns:
        A list of str representing the formatted lines of the plan.
    """
    await cur.execute("SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY())")
    return [r[0] for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Statement messages (DBMS_OUTPUT, compilation errors)
# ---------------------------------------------------------------------------

DBMS_OUTPUT_CHUNK = 100
"""Lines fetched per DBMS_OUTPUT.GET_LINES round trip."""

MAX_DBMS_OUTPUT_LINES = 1000
"""Cap on lines returned to the client. The buffer is still drained past this
point — leftovers would otherwise surface on the *next* statement."""


async def enable_dbms_output(conn: AsyncConnection) -> None:
    """Turn on DBMS_OUTPUT capture for this session, with an unbounded buffer.

    Unbounded is deliberate: a bounded buffer raises ORU-10027 *inside* the
    user's own block, turning a working statement into a failure. The size is
    bounded in practice by how much the statement chooses to print.
    """
    cur = conn.cursor()
    await cur.callproc("dbms_output.enable", (None,))


async def fetch_dbms_output(conn: AsyncConnection) -> tuple[list[str], bool]:
    """Drain the session's DBMS_OUTPUT buffer.

    Returns:
        The buffered lines and whether they were truncated at
        ``MAX_DBMS_OUTPUT_LINES``.
    """
    cur = conn.cursor()
    lines_var = cur.arrayvar(str, DBMS_OUTPUT_CHUNK)
    count_var = cur.var(int)
    collected: list[str] = []
    truncated = False

    while True:
        count_var.setvalue(0, DBMS_OUTPUT_CHUNK)
        await cur.callproc("dbms_output.get_lines", (lines_var, count_var))
        count = int(count_var.getvalue() or 0)
        if len(collected) < MAX_DBMS_OUTPUT_LINES:
            room = MAX_DBMS_OUTPUT_LINES - len(collected)
            batch = lines_var.getvalue()[:count]
            collected.extend(batch[:room])
            truncated = truncated or len(batch) > room
        elif count:
            truncated = True
        if count < DBMS_OUTPUT_CHUNK:
            return collected, truncated


_COMPILATION_ERRORS_SQL = """
    SELECT line, position, text
    FROM user_errors
    WHERE name = :1 AND type = :2
    ORDER BY sequence
"""


async def fetch_compilation_errors(
    conn: AsyncConnection, name: str, object_type: str
) -> list[tuple[int, int, str]]:
    """Return ``(line, position, text)`` for each compilation error on an object.

    Oracle reports a PL/SQL object that failed to compile as a *successful*
    CREATE, so this is the only way to see why it is broken.
    """
    cur = conn.cursor()
    await cur.execute(_COMPILATION_ERRORS_SQL, [name, object_type])
    return [(int(line), int(pos), text) for line, pos, text in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Explore queries
# ---------------------------------------------------------------------------


def build_preview_query(schema: str, table: str) -> str:
    """Build an SQL query to preview a table."""
    return f'SELECT * FROM "{schema}"."{table}" FETCH FIRST 10 ROWS ONLY'


@_conn_cache
async def fetch_schemas(
    conn: AsyncConnection, has_oracle_maintained: bool
) -> list[str]:
    """Return non-system schema names, ordered alphabetically."""
    cur = conn.cursor()
    if has_oracle_maintained:
        await cur.execute(
            "SELECT USERNAME FROM ALL_USERS"
            " WHERE ORACLE_MAINTAINED = 'N' ORDER BY USERNAME"
        )
    else:
        await cur.execute(
            "SELECT USERNAME FROM ALL_USERS"
            f" WHERE USERNAME NOT IN ({_PRE12_SYSTEM_SCHEMAS_SQL})"
            " ORDER BY USERNAME"
        )
    return [r[0] for r in await cur.fetchall()]


@_conn_cache
async def fetch_tables_and_views(
    conn: AsyncConnection, schema: str
) -> list[tuple[str, str]]:
    """Return (name, type) pairs for all tables and views in *schema*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT TABLE_NAME AS N, 'table' AS T FROM ALL_TABLES WHERE OWNER = :1"
        " UNION ALL"
        " SELECT VIEW_NAME, 'view' FROM ALL_VIEWS WHERE OWNER = :2"
        " ORDER BY 1",
        [schema, schema],
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


@_conn_cache
async def fetch_column_names_and_types(
    conn: AsyncConnection, schema: str, table: str
) -> list[tuple[str, str]]:
    """Return (column_name, data_type) pairs ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS"
        " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
        [schema, table],
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


@_conn_cache
async def fetch_index_names_and_types(
    conn: AsyncConnection, schema: str, table: str
) -> list[tuple[str, str]]:
    """Return (index_name, index_type) pairs ordered by index name."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT INDEX_NAME, LOWER(INDEX_TYPE) FROM ALL_INDEXES"
        " WHERE TABLE_OWNER = :1 AND TABLE_NAME = :2 ORDER BY INDEX_NAME",
        [schema, table],
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Symbol lookup (explore.find)
# ---------------------------------------------------------------------------
#
# One query per node type, resolving a name across every schema at once rather
# than listing schema by schema. Each predicate is an equality against an
# indexed dictionary column, and the owner filter repeats what fetch_schemas
# selects — a find must never resolve to a path the object tree does not hold.


def _owner_filter(column: str, has_oracle_maintained: bool) -> str:
    """Return an AND-predicate restricting *column* to the non-system schemas
    :func:`fetch_schemas` lists, expressed as a subquery so the two cannot drift
    apart and the whole find stays one round trip."""
    if has_oracle_maintained:
        return (
            f" AND {column} IN"
            " (SELECT USERNAME FROM ALL_USERS WHERE ORACLE_MAINTAINED = 'N')"
        )
    return f" AND {column} NOT IN ({_PRE12_SYSTEM_SCHEMAS_SQL})"


def _in_binds(values: tuple[str, ...], start: int) -> str:
    """Render *values* as a positional bind list starting at ``:start``."""
    return ", ".join(f":{start + i}" for i in range(len(values)))


def _scope_filter(column: str, names: tuple[str, ...], binds: list[str]) -> str:
    """Return an AND-predicate restricting *column* to *names*, appending the
    bind values to *binds*. Empty *names* leaves the column unconstrained."""
    if not names:
        return ""
    start = len(binds) + 1
    binds.extend(names)
    return f" AND {column} IN ({_in_binds(names, start)})"


def _name_forms(name: str) -> tuple[str, ...]:
    """Return the stored forms *name* may take: as written, and folded upper.

    Oracle stores an unquoted identifier upper-cased, so a symbol typed in a
    query buffer almost always needs folding; a quoted identifier is stored
    verbatim. Matching both as equalities keeps the dictionary index usable,
    where an ``UPPER(column) =`` predicate would not.
    """
    return (name,) if name == name.upper() else (name, name.upper())


def _find_object_branch(
    view: str,
    name_column: str,
    names: tuple[str, ...],
    schemas: tuple[str, ...],
    has_oracle_maintained: bool,
    binds: list[str],
) -> str:
    """Render one branch of the table/view union, appending its bind values to
    *binds*.

    Each branch gets its own bind numbers rather than reusing the first's:
    oracledb counts a repeated ``:1`` as a second positional bind, so a shared
    placeholder list fails with DPY-4009.
    """
    start = len(binds) + 1
    binds.extend(names)
    return (
        f"SELECT OWNER, {name_column} FROM {view}"
        f" WHERE {name_column} IN ({_in_binds(names, start)})"
        f"{_owner_filter('OWNER', has_oracle_maintained)}"
        f"{_scope_filter('OWNER', schemas, binds)}"
    )


@_conn_cache
async def fetch_find_tables_and_views(
    conn: AsyncConnection,
    name: str,
    schemas: tuple[str, ...],
    has_oracle_maintained: bool,
) -> list[tuple[str, str]]:
    """Return (owner, name) pairs for every table or view called *name*.

    Unions the same two dictionary views :func:`fetch_tables_and_views` lists
    from, so a find and the tree agree on what exists.

    Args:
        conn: Open connection.
        name: Symbol name as written by the client.
        schemas: Owner names to restrict to; empty searches every schema.
        has_oracle_maintained: True on Oracle 12c+.
    """
    names = _name_forms(name)
    binds: list[str] = []
    tables = _find_object_branch(
        "ALL_TABLES", "TABLE_NAME", names, schemas, has_oracle_maintained, binds
    )
    views = _find_object_branch(
        "ALL_VIEWS", "VIEW_NAME", names, schemas, has_oracle_maintained, binds
    )
    cur = conn.cursor()
    await cur.execute(f"{tables} UNION {views} ORDER BY 1, 2", binds)
    return [(r[0], r[1]) for r in await cur.fetchall()]


@_conn_cache
async def fetch_find_columns(
    conn: AsyncConnection,
    name: str,
    schemas: tuple[str, ...],
    tables: tuple[str, ...],
    has_oracle_maintained: bool,
) -> list[tuple[str, str, str]]:
    """Return (owner, table, column) triples for every column called *name*.

    ``ALL_TAB_COLUMNS`` covers views as well as tables, matching the object
    tree, which hangs a ``columns`` group off both.

    Args:
        conn: Open connection.
        name: Symbol name as written by the client.
        schemas: Owner names to restrict to; empty searches every schema.
        tables: Table/view names to restrict to; empty searches every table.
        has_oracle_maintained: True on Oracle 12c+.
    """
    names = _name_forms(name)
    binds: list[str] = list(names)
    cur = conn.cursor()
    await cur.execute(
        "SELECT OWNER, TABLE_NAME, COLUMN_NAME FROM ALL_TAB_COLUMNS"
        f" WHERE COLUMN_NAME IN ({_in_binds(names, 1)})"
        f"{_owner_filter('OWNER', has_oracle_maintained)}"
        f"{_scope_filter('OWNER', schemas, binds)}"
        f"{_scope_filter('TABLE_NAME', tables, binds)}"
        " ORDER BY 1, 2, 3",
        binds,
    )
    return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


@_conn_cache
async def fetch_find_indexes(
    conn: AsyncConnection,
    name: str,
    schemas: tuple[str, ...],
    tables: tuple[str, ...],
    has_oracle_maintained: bool,
) -> list[tuple[str, str, str]]:
    """Return (table_owner, table, index) triples for every index called *name*.

    Keyed on ``TABLE_OWNER``/``TABLE_NAME`` rather than the index's own owner,
    since the tree hangs an index off the table it indexes — the same basis
    :func:`fetch_index_names_and_types` lists on.

    Args:
        conn: Open connection.
        name: Symbol name as written by the client.
        schemas: Owner names to restrict to; empty searches every schema.
        tables: Table names to restrict to; empty searches every table.
        has_oracle_maintained: True on Oracle 12c+.
    """
    names = _name_forms(name)
    binds: list[str] = list(names)
    cur = conn.cursor()
    await cur.execute(
        "SELECT TABLE_OWNER, TABLE_NAME, INDEX_NAME FROM ALL_INDEXES"
        f" WHERE INDEX_NAME IN ({_in_binds(names, 1)})"
        f"{_owner_filter('TABLE_OWNER', has_oracle_maintained)}"
        f"{_scope_filter('TABLE_OWNER', schemas, binds)}"
        f"{_scope_filter('TABLE_NAME', tables, binds)}"
        " ORDER BY 1, 2, 3",
        binds,
    )
    return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Table describe queries
# ---------------------------------------------------------------------------


@_conn_cache
async def fetch_column_details(
    conn: AsyncConnection, schema: str, table: str
) -> list[ColumnDetail]:
    """Return column metadata ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT,"
        " CHAR_LENGTH, DATA_LENGTH"
        " FROM ALL_TAB_COLUMNS"
        " WHERE OWNER = :1 AND TABLE_NAME = :2 ORDER BY COLUMN_ID",
        [schema, table],
    )
    return [
        ColumnDetail(
            name=r[0],
            type=r[1],
            nullable=r[2] == "Y",
            default=r[3].strip() if r[3] is not None else None,
            char_length=r[4],
            byte_length=r[5],
        )
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_pk_columns(conn: AsyncConnection, schema: str, table: str) -> set[str]:
    """Return the set of column names that form the primary key."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS con"
        " JOIN ALL_CONS_COLUMNS cc"
        "  ON con.OWNER = cc.OWNER"
        "  AND con.CONSTRAINT_NAME = cc.CONSTRAINT_NAME"
        "  AND con.TABLE_NAME = cc.TABLE_NAME"
        " WHERE con.OWNER = :1 AND con.TABLE_NAME = :2"
        "  AND con.CONSTRAINT_TYPE = 'P'",
        [schema, table],
    )
    return {r[0] for r in await cur.fetchall()}


@_conn_cache
async def fetch_unique_columns(
    conn: AsyncConnection, schema: str, table: str
) -> set[str]:
    """Return columns constrained to unique values by a single-column PK or
    UNIQUE constraint."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT con.CONSTRAINT_NAME, cc.COLUMN_NAME"
        " FROM ALL_CONSTRAINTS con"
        " JOIN ALL_CONS_COLUMNS cc"
        "  ON con.OWNER = cc.OWNER AND con.CONSTRAINT_NAME = cc.CONSTRAINT_NAME"
        " WHERE con.OWNER = :1 AND con.TABLE_NAME = :2"
        " AND con.CONSTRAINT_TYPE IN ('P', 'U')",
        [schema, table],
    )
    by_constraint: dict[str, set[str]] = {}
    for constraint_name, col_name in await cur.fetchall():
        by_constraint.setdefault(constraint_name, set()).add(col_name)
    return {next(iter(cols)) for cols in by_constraint.values() if len(cols) == 1}


@_conn_cache
async def fetch_outgoing_references(
    conn: AsyncConnection, schema: str, table: str
) -> list[TableReference]:
    """Return foreign keys defined on *table* that reference other tables."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT lc.COLUMN_NAME, rcon.OWNER, rcon.TABLE_NAME, rc.COLUMN_NAME,"
        " con.CONSTRAINT_NAME"
        " FROM ALL_CONSTRAINTS con"
        " JOIN ALL_CONS_COLUMNS lc"
        "  ON con.OWNER = lc.OWNER AND con.CONSTRAINT_NAME = lc.CONSTRAINT_NAME"
        " JOIN ALL_CONSTRAINTS rcon"
        "  ON con.R_OWNER = rcon.OWNER AND con.R_CONSTRAINT_NAME = rcon.CONSTRAINT_NAME"
        " JOIN ALL_CONS_COLUMNS rc"
        "  ON rcon.OWNER = rc.OWNER AND rcon.CONSTRAINT_NAME = rc.CONSTRAINT_NAME"
        "  AND lc.POSITION = rc.POSITION"
        " WHERE con.CONSTRAINT_TYPE = 'R' AND con.OWNER = :1 AND con.TABLE_NAME = :2"
        " ORDER BY con.CONSTRAINT_NAME, lc.POSITION",
        [schema, table],
    )
    rows = await cur.fetchall()
    unique_cols = await fetch_unique_columns(conn, schema, table)
    return [
        TableReference(
            table=table,
            schema=schema,
            column=r[0],
            ref_table=r[2],
            ref_schema=r[1],
            ref_column=r[3],
            unique=r[0] in unique_cols,
            constraint_name=r[4],
        )
        for r in rows
    ]


@_conn_cache
async def fetch_incoming_references(
    conn: AsyncConnection, schema: str, table: str
) -> list[TableReference]:
    """Return foreign keys on other tables in *schema* that reference *table*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT rc.COLUMN_NAME, con.OWNER, con.TABLE_NAME, lc.COLUMN_NAME,"
        " con.CONSTRAINT_NAME"
        " FROM ALL_CONSTRAINTS con"
        " JOIN ALL_CONS_COLUMNS lc"
        "  ON con.OWNER = lc.OWNER AND con.CONSTRAINT_NAME = lc.CONSTRAINT_NAME"
        " JOIN ALL_CONSTRAINTS rcon"
        "  ON con.R_OWNER = rcon.OWNER AND con.R_CONSTRAINT_NAME = rcon.CONSTRAINT_NAME"
        " JOIN ALL_CONS_COLUMNS rc"
        "  ON rcon.OWNER = rc.OWNER AND rcon.CONSTRAINT_NAME = rc.CONSTRAINT_NAME"
        "  AND lc.POSITION = rc.POSITION"
        " WHERE con.CONSTRAINT_TYPE = 'R' AND rcon.OWNER = :1 AND rcon.TABLE_NAME = :2"
        " ORDER BY con.CONSTRAINT_NAME, lc.POSITION",
        [schema, table],
    )
    references = []
    for r in await cur.fetchall():
        fk_schema, fk_table, fk_col, constraint_name = r[1], r[2], r[3], r[4]
        unique_cols = await fetch_unique_columns(conn, fk_schema, fk_table)
        references.append(
            TableReference(
                table=fk_table,
                schema=fk_schema,
                column=fk_col,
                ref_table=table,
                ref_schema=schema,
                ref_column=r[0],
                unique=fk_col in unique_cols,
                constraint_name=constraint_name,
            )
        )
    return references


@_conn_cache
async def fetch_column_index_mapping(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[str]]:
    """Return ``{column_name: [index_name, ...]}`` for all indexed columns."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT aic.COLUMN_NAME, aic.INDEX_NAME"
        " FROM ALL_IND_COLUMNS aic"
        " JOIN ALL_INDEXES ai"
        "  ON aic.INDEX_OWNER = ai.OWNER AND aic.INDEX_NAME = ai.INDEX_NAME"
        " WHERE ai.TABLE_OWNER = :1 AND ai.TABLE_NAME = :2",
        [schema, table],
    )
    result: dict[str, list[str]] = {}
    for col_name, idx_name in await cur.fetchall():
        result.setdefault(col_name, []).append(idx_name)
    return result


# ---------------------------------------------------------------------------
# Index describe queries — table-scoped (batch)
# ---------------------------------------------------------------------------


@_conn_cache
async def fetch_index_metas_for_table(
    conn: AsyncConnection, schema: str, table: str
) -> list[IndexMeta]:
    """Return metadata for all indexes on *table*, ordered by index name."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT OWNER, INDEX_NAME, INDEX_TYPE, UNIQUENESS, VISIBILITY, GENERATED"
        " FROM ALL_INDEXES"
        " WHERE TABLE_OWNER = :1 AND TABLE_NAME = :2"
        " ORDER BY INDEX_NAME",
        [schema, table],
    )
    return [
        IndexMeta(
            name=r[1],
            owner=r[0],
            index_type=r[2].lower() if r[2] else None,
            unique=r[3] == "UNIQUE",
            visible=r[4] != "INVISIBLE",
            generated=r[5] == "Y",
        )
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_index_fields_for_table(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[IndexKeyField]]:
    """Return ``{index_name: [IndexKeyField, ...]}`` for all indexes on *table*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT INDEX_NAME, COLUMN_NAME, DESCEND"
        " FROM ALL_IND_COLUMNS"
        " WHERE INDEX_OWNER = :1 AND TABLE_NAME = :2"
        " ORDER BY INDEX_NAME, COLUMN_POSITION",
        [schema, table],
    )
    result: dict[str, list[IndexKeyField]] = {}
    for idx_name, col_name, descend in await cur.fetchall():
        result.setdefault(idx_name, []).append(
            IndexKeyField(
                name=col_name, direction="desc" if descend == "DESC" else "asc"
            )
        )
    return result


@_conn_cache
async def fetch_join_tables_for_table(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[str]]:
    """Return ``{index_name: [table, ...]}`` for bitmap join indexes.

    Returns an empty dict if ``ALL_JOIN_IND_COLUMNS`` is unavailable.
    """
    cur = conn.cursor()
    try:
        await cur.execute(
            "SELECT DISTINCT INDEX_NAME, INNER_TABLE_NAME, OUTER_TABLE_NAME"
            " FROM ALL_JOIN_IND_COLUMNS"
            " WHERE INDEX_OWNER = :1",
            [schema],
        )
        result: dict[str, list[str]] = {}
        for idx_name, inner, outer in await cur.fetchall():
            joined = result.setdefault(idx_name, [table])
            for t in (inner, outer):
                if t != table and t not in joined:
                    joined.append(t)
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Index describe queries — single-index
# ---------------------------------------------------------------------------


@_conn_cache
async def fetch_index_meta(
    conn: AsyncConnection, schema: str, index_name: str
) -> IndexMeta | None:
    """Return metadata for a single index, or None if not found."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT INDEX_TYPE, UNIQUENESS, VISIBILITY, GENERATED"
        " FROM ALL_INDEXES"
        " WHERE OWNER = :1 AND INDEX_NAME = :2",
        [schema, index_name],
    )
    row = await cur.fetchone()
    if row is None:
        return None
    idx_type, uniqueness, visibility, generated = row
    return IndexMeta(
        name=index_name,
        owner=schema,
        index_type=idx_type.lower() if idx_type else None,
        unique=uniqueness == "UNIQUE",
        visible=visibility != "INVISIBLE",
        generated=generated == "Y",
    )


@_conn_cache
async def fetch_index_fields_for_index(
    conn: AsyncConnection, schema: str, index_name: str
) -> list[IndexKeyField]:
    """Return key fields for a single index, ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT COLUMN_NAME, DESCEND FROM ALL_IND_COLUMNS"
        " WHERE INDEX_OWNER = :1 AND INDEX_NAME = :2"
        " ORDER BY COLUMN_POSITION",
        [schema, index_name],
    )
    return [
        IndexKeyField(name=r[0], direction="desc" if r[1] == "DESC" else "asc")
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_join_tables_for_index(
    conn: AsyncConnection, schema: str, index_name: str, table: str
) -> list[str]:
    """Return the list of tables for a bitmap join index.

    Returns ``[table]`` if the index is not a join index or if
    ``ALL_JOIN_IND_COLUMNS`` is unavailable.
    """
    cur = conn.cursor()
    tables = [table]
    try:
        await cur.execute(
            "SELECT DISTINCT INNER_TABLE_NAME, OUTER_TABLE_NAME"
            " FROM ALL_JOIN_IND_COLUMNS"
            " WHERE INDEX_OWNER = :1 AND INDEX_NAME = :2",
            [schema, index_name],
        )
        for inner, outer in await cur.fetchall():
            for t in (inner, outer):
                if t != table and t not in tables:
                    tables.append(t)
    except Exception:
        pass
    return tables


# ---------------------------------------------------------------------------
# Column describe queries
# ---------------------------------------------------------------------------


@_conn_cache
async def fetch_table_comment(
    conn: AsyncConnection, schema: str, table: str
) -> str | None:
    """Return the table-level comment, or None if unsupported or not set."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT COMMENTS FROM ALL_TAB_COMMENTS WHERE OWNER = :1 AND TABLE_NAME = :2",
        [schema, table],
    )
    row = await cur.fetchone()
    return row[0].strip() if row and row[0] and row[0].strip() else None


@_conn_cache
async def fetch_all_column_comments(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, str | None]:
    """Return ``{column_name: comment}`` for all columns; value is None when no comment is set."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS"
        " WHERE OWNER = :1 AND TABLE_NAME = :2",
        [schema, table],
    )
    return {
        r[0]: (r[1].strip() if r[1] and r[1].strip() else None)
        for r in await cur.fetchall()
    }


@_conn_cache
async def fetch_column_sample(
    conn: AsyncConnection, schema: str, table: str, col_name: str, n: int = 3
) -> list[Any]:
    """Return up to *n* distinct non-null values sampled from *col_name*."""
    cur = conn.cursor()
    try:
        await cur.execute(
            f'SELECT DISTINCT "{col_name}" FROM "{schema}"."{table}"'
            f' WHERE "{col_name}" IS NOT NULL FETCH FIRST {n} ROWS ONLY'
        )
        return [await render_lob(r[0]) for r in await cur.fetchall()]
    except Exception:
        return []


@_conn_cache
async def fetch_table_sample_rows(
    conn: AsyncConnection, schema: str, table: str
) -> tuple[list[str], list[list[Any]]]:
    """Return the column names and first :data:`SAMPLE_SCAN_ROWS` rows of *table*.

    A single scan replacing one ``SELECT DISTINCT`` round trip per column;
    per-column sample values are derived client-side by the driver.
    """
    cur = conn.cursor()
    try:
        await cur.execute(
            f'SELECT * FROM "{schema}"."{table}"'
            f" FETCH FIRST {SAMPLE_SCAN_ROWS} ROWS ONLY"
        )
        columns = [d[0] for d in cur.description or []]
        rows = [[await render_lob(v) for v in row] for row in await cur.fetchall()]
        return columns, rows
    except Exception:
        return [], []


async def render_lob(
    value: Any,
    register_lob: Callable[[bytes | str, str], LobPlaceholder] | None = None,
) -> Any:
    """Render a LOB locator as a :class:`LobPlaceholder`.

    A LOB locator can only be read while the cursor is still positioned on the
    row it came from — reading it after the cursor's next internal fetch (or
    after it closes) crashes the process. That means the *only* safe time to
    read one is right now, immediately after the row was fetched.

    When `register_lob` is given, the full content is read now and cached
    under a fresh ref via that callback so the cell can be re-downloaded
    later (the cache holds the materialized value, never the locator). With
    no callback, this falls back to a cheap ``size()`` round trip and a
    non-downloadable placeholder — used for schema-browsing previews where
    eagerly reading every sampled LOB's full content isn't worth the cost.
    """
    if not hasattr(value, "read"):
        return value
    type_name = value.type.name.removeprefix("DB_TYPE_")
    unit = "bytes" if type_name == "BLOB" else "chars"
    if register_lob is None:
        return LobPlaceholder(text=f"{type_name} ({await value.size()} {unit})")
    try:
        content = await value.read()
    except UnicodeDecodeError:
        # A CLOB holding bytes that aren't valid in the database charset. Unlike
        # a VARCHAR2 there is no decoding hook to soften this (the LOB is
        # decoded as it is read), so the cell reports the damage instead of
        # failing the statement that happened to select it.
        return LobPlaceholder(text=f"{type_name} (undecodable text)")
    return register_lob(content, f"{type_name} ({len(content)} {unit})")


def build_column_index_lists(
    fields_by_index: dict[str, list[IndexKeyField]],
    all_indices: list[IndexDescription],
) -> tuple[dict[str, list[IndexDescription]], dict[str, list[IndexDescription]]]:
    """Return (exclusive, composite) dicts mapping column name to index descriptions.

    *fields_by_index* is the result of :func:`fetch_index_fields_for_table`;
    *all_indices* is the full list of :class:`IndexDescription` for the table.
    """
    idx_by_name = {idx.name: idx for idx in all_indices}
    excl: dict[str, list[IndexDescription]] = {}
    comp: dict[str, list[IndexDescription]] = {}
    for idx_name, fields in fields_by_index.items():
        idx_desc = idx_by_name.get(idx_name)
        if idx_desc is None:
            continue
        col_names = [f.name for f in fields]
        for cn in col_names:
            if len(col_names) == 1:
                excl.setdefault(cn, []).append(idx_desc)
            else:
                comp.setdefault(cn, []).append(idx_desc)
    return excl, comp


# ---------------------------------------------------------------------------
# DDL queries
# ---------------------------------------------------------------------------


async def apply_metadata_transform(conn: AsyncConnection) -> None:
    """Set DBMS_METADATA session transform params to suppress storage/segment clauses."""
    cur = conn.cursor()
    await cur.execute(
        "BEGIN"
        " DBMS_METADATA.SET_TRANSFORM_PARAM("
        "  DBMS_METADATA.SESSION_TRANSFORM, 'STORAGE', FALSE);"
        " DBMS_METADATA.SET_TRANSFORM_PARAM("
        "  DBMS_METADATA.SESSION_TRANSFORM, 'SEGMENT_ATTRIBUTES', FALSE);"
        " END;"
    )


@_conn_cache
async def fetch_index_ddl(
    conn: AsyncConnection, schema: str, index_name: str
) -> str | None:
    """Return the CREATE INDEX DDL for *index_name*, or None if unavailable."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT DBMS_METADATA.GET_DDL('INDEX', :1, :2) FROM DUAL",
        [index_name, schema],
    )
    row = await cur.fetchone()
    if not row or row[0] is None:
        return None
    val = row[0]
    if hasattr(val, "read"):
        val = await val.read()
    return str(val).strip() or None
