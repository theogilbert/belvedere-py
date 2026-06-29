"""Oracle query functions — one per SQL query, with typed return values."""

from dataclasses import dataclass
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from oracledb import AsyncConnection, AsyncCursor

from ...protocol import IndexDescription, IndexKeyField

_CONSTRAINT_TYPE = {"P": "primary_key", "U": "unique", "C": "check", "R": "foreign_key"}

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


def _conn_cache(fn):
    """Cache async query results per connection, keyed on positional args after *conn*.

    Each distinct *conn* object gets its own cache dict; entries are released
    when the connection is garbage-collected.

    Raises TypeError at decoration time if the first parameter is not annotated
    as AsyncConnection.
    """
    _store: WeakKeyDictionary = WeakKeyDictionary()

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


@_conn_cache
async def fetch_constraint_names_and_types(
    conn: AsyncConnection, schema: str, table: str
) -> list[tuple[str, str]]:
    """Return (constraint_name, mapped_type) pairs for user-named enabled constraints."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE FROM ALL_CONSTRAINTS"
        " WHERE OWNER = :1 AND TABLE_NAME = :2"
        " AND CONSTRAINT_TYPE IN ('P', 'U', 'C', 'R')"
        " AND STATUS = 'ENABLED' AND GENERATED = 'USER NAME'"
        " ORDER BY CONSTRAINT_NAME",
        [schema, table],
    )
    return [
        (r[0], _CONSTRAINT_TYPE.get(r[1], r[1].lower())) for r in await cur.fetchall()
    ]


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
        "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT"
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
        return [r[0] for r in await cur.fetchall()]
    except Exception:
        return []


def build_column_index_lists(
    fields_by_index: dict[str, list[IndexKeyField]],
    all_indices: list[IndexDescription],
) -> tuple[dict[str, list[IndexDescription]], dict[str, list[IndexDescription]]]:
    """Return (exclusive, composite) dicts mapping column name to index descriptions.

    *fields_by_index* is the result of :func:`fetch_index_fields_for_table`;
    *all_indices* is the full list of :class:`IndexDescription` for the table.
    """
    idx_by_name = {idx.index: idx for idx in all_indices}
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
