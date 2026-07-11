"""PostgreSQL query functions — one per SQL query, with typed return values."""

from dataclasses import dataclass
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from psycopg import AsyncConnection, sql

from ...protocol import IndexDescription, IndexKeyField, LobPlaceholder, TableReference
from ..base import SAMPLE_SCAN_ROWS

_CONSTRAINT_TYPE = {
    "PRIMARY KEY": "primary_key",
    "UNIQUE": "unique",
    "CHECK": "check",
    "FOREIGN KEY": "foreign_key",
}

# Pairs conkey[i] with confkey[i] element-wise so composite foreign keys keep
# the correct local/referenced column ordering (unlike a name-based join
# across information_schema views, which has no reliable ordering guarantee).
_FK_COLUMN_PAIRS_SQL = (
    "FROM pg_constraint con"
    " JOIN pg_class lc ON lc.oid = con.conrelid"
    " JOIN pg_namespace ln ON ln.oid = lc.relnamespace"
    " JOIN pg_class fc ON fc.oid = con.confrelid"
    " JOIN pg_namespace fn ON fn.oid = fc.relnamespace"
    " CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS u(conkey, confkey, ord)"
    " JOIN pg_attribute la ON la.attrelid = lc.oid AND la.attnum = u.conkey"
    " JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = u.confkey"
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
    """PostgreSQL data type string."""
    nullable: bool
    """Whether the column allows NULL."""
    default: str | None
    """Default expression, stripped of surrounding whitespace; None if not set."""


@dataclass
class IndexMeta:
    """Index-level metadata returned by fetch_index_meta* functions."""

    name: str
    """Index name."""
    index_type: str | None
    """Access method name (e.g. ``"btree"``, ``"gin"``, ``"hash"``); None if unknown."""
    unique: bool
    """Whether the index enforces uniqueness."""
    visible: bool
    """Whether the optimiser considers this index (False when ``indisvalid`` is false,
    e.g. a ``CREATE INDEX CONCURRENTLY`` that failed partway through)."""
    ddl: str | None
    """``CREATE INDEX`` statement as reported by ``pg_indexes``."""


# ---------------------------------------------------------------------------
# Explore queries
# ---------------------------------------------------------------------------


def build_preview_query(schema: str, table: str) -> str:
    """Build an SQL query to preview a table."""
    return f'SELECT * FROM "{schema}"."{table}" LIMIT 10'


@_conn_cache
async def fetch_schemas(conn: AsyncConnection) -> list[str]:
    """Return non-system schema names, ordered alphabetically."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT nspname FROM pg_namespace"
        " WHERE nspname NOT IN ('pg_catalog', 'information_schema')"
        " AND nspname NOT LIKE 'pg\\_%'"
        " ORDER BY nspname"
    )
    return [r[0] for r in await cur.fetchall()]


@_conn_cache
async def fetch_tables_and_views(
    conn: AsyncConnection, schema: str
) -> list[tuple[str, str]]:
    """Return (name, type) pairs for all tables and views in *schema*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT table_name, table_type FROM information_schema.tables"
        " WHERE table_schema = %s ORDER BY table_name",
        [schema],
    )
    return [
        (r[0], "table" if r[1] == "BASE TABLE" else r[1].lower().replace(" ", "_"))
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_column_names_and_types(
    conn: AsyncConnection, schema: str, table: str
) -> list[tuple[str, str]]:
    """Return (column_name, data_type) pairs ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
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
        "SELECT ic.relname, am.amname"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " JOIN pg_am am ON am.oid = ic.relam"
        " WHERE n.nspname = %s AND tc.relname = %s"
        " ORDER BY ic.relname",
        [schema, table],
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


@_conn_cache
async def fetch_constraint_names_and_types(
    conn: AsyncConnection, schema: str, table: str
) -> list[tuple[str, str]]:
    """Return (constraint_name, mapped_type) pairs ordered by constraint name."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT constraint_name, constraint_type"
        " FROM information_schema.table_constraints"
        " WHERE table_schema = %s AND table_name = %s"
        " ORDER BY constraint_name",
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
        "SELECT column_name, data_type, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        [schema, table],
    )
    return [
        ColumnDetail(
            name=r[0],
            type=r[1],
            nullable=r[2] == "YES",
            default=r[3].strip() if r[3] is not None else None,
        )
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_pk_columns(conn: AsyncConnection, schema: str, table: str) -> set[str]:
    """Return the set of column names that form the primary key."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT kcu.column_name"
        " FROM information_schema.table_constraints tc"
        " JOIN information_schema.key_column_usage kcu"
        "  ON tc.constraint_name = kcu.constraint_name"
        "  AND tc.table_schema = kcu.table_schema"
        " WHERE tc.table_schema = %s AND tc.table_name = %s"
        " AND tc.constraint_type = 'PRIMARY KEY'",
        [schema, table],
    )
    return {r[0] for r in await cur.fetchall()}


@_conn_cache
async def fetch_unique_columns(
    conn: AsyncConnection, schema: str, table: str
) -> set[str]:
    """Return columns covered by a single-column UNIQUE index (PKs included,
    since Postgres backs every PK with a unique index)."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT a.attname"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = ix.indkey[0]"
        " WHERE n.nspname = %s AND tc.relname = %s"
        " AND ix.indisunique AND array_length(ix.indkey, 1) = 1",
        [schema, table],
    )
    return {r[0] for r in await cur.fetchall()}


@_conn_cache
async def fetch_outgoing_references(
    conn: AsyncConnection, schema: str, table: str
) -> list[TableReference]:
    """Return foreign keys defined on *table* that reference other tables."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT la.attname, fn.nspname, fc.relname, fa.attname"
        f" {_FK_COLUMN_PAIRS_SQL}"
        " WHERE con.contype = 'f' AND ln.nspname = %s AND lc.relname = %s"
        " ORDER BY con.conname, u.ord",
        [schema, table],
    )
    unique_cols = await fetch_unique_columns(conn, schema, table)
    return [
        TableReference(
            column=r[0],
            schema=r[1],
            table=r[2],
            ref_column=r[3],
            unique=r[0] in unique_cols,
        )
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_incoming_references(
    conn: AsyncConnection, schema: str, table: str
) -> list[TableReference]:
    """Return foreign keys on other tables in *schema* that reference *table*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT fa.attname, ln.nspname, lc.relname, la.attname"
        f" {_FK_COLUMN_PAIRS_SQL}"
        " WHERE con.contype = 'f' AND fn.nspname = %s AND fc.relname = %s"
        " ORDER BY con.conname, u.ord",
        [schema, table],
    )
    references = []
    for r in await cur.fetchall():
        ref_col, fk_schema, fk_table, fk_col = r
        unique_cols = await fetch_unique_columns(conn, fk_schema, fk_table)
        references.append(
            TableReference(
                column=ref_col,
                schema=fk_schema,
                table=fk_table,
                ref_column=fk_col,
                unique=fk_col in unique_cols,
            )
        )
    return references


@_conn_cache
async def fetch_column_index_mapping(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[str]]:
    """Return ``{column_name: [index_name, ...]}`` for all indexed columns.

    Expression index entries (``indkey`` element ``0``) are skipped since they
    do not name a single backing column.
    """
    cur = conn.cursor()
    await cur.execute(
        "SELECT a.attname, ic.relname"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND tc.relname = %s",
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
        "SELECT ic.relname, am.amname, ix.indisunique, ix.indisvalid, pgi.indexdef"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " JOIN pg_am am ON am.oid = ic.relam"
        " JOIN pg_indexes pgi"
        "  ON pgi.schemaname = n.nspname AND pgi.tablename = tc.relname"
        "  AND pgi.indexname = ic.relname"
        " WHERE n.nspname = %s AND tc.relname = %s"
        " ORDER BY ic.relname",
        [schema, table],
    )
    return [
        IndexMeta(name=r[0], index_type=r[1], unique=r[2], visible=r[3], ddl=r[4])
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_index_fields_for_table(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[IndexKeyField]]:
    """Return ``{index_name: [IndexKeyField, ...]}`` key columns for all indexes on *table*.

    Excludes non-key (``INCLUDE``) columns — see :func:`fetch_index_included_for_table`.
    """
    cur = conn.cursor()
    await cur.execute(
        "SELECT ic.relname, a.attname, (ix.indoption[k.ord - 1] & 1) <> 0"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND tc.relname = %s AND k.ord <= ix.indnkeyatts"
        " ORDER BY ic.relname, k.ord",
        [schema, table],
    )
    result: dict[str, list[IndexKeyField]] = {}
    for idx_name, col_name, is_desc in await cur.fetchall():
        result.setdefault(idx_name, []).append(
            IndexKeyField(name=col_name, direction="desc" if is_desc else "asc")
        )
    return result


@_conn_cache
async def fetch_index_included_for_table(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, list[str]]:
    """Return ``{index_name: [column, ...]}`` INCLUDE columns for all indexes on *table*."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT ic.relname, a.attname"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND tc.relname = %s AND k.ord > ix.indnkeyatts"
        " ORDER BY ic.relname, k.ord",
        [schema, table],
    )
    result: dict[str, list[str]] = {}
    for idx_name, col_name in await cur.fetchall():
        result.setdefault(idx_name, []).append(col_name)
    return result


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
        "SELECT am.amname, ix.indisunique, ix.indisvalid, pgi.indexdef"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " JOIN pg_am am ON am.oid = ic.relam"
        " JOIN pg_indexes pgi"
        "  ON pgi.schemaname = n.nspname AND pgi.tablename = tc.relname"
        "  AND pgi.indexname = ic.relname"
        " WHERE n.nspname = %s AND ic.relname = %s",
        [schema, index_name],
    )
    row = await cur.fetchone()
    if row is None:
        return None
    amname, unique, valid, ddl = row
    return IndexMeta(
        name=index_name, index_type=amname, unique=unique, visible=valid, ddl=ddl
    )


@_conn_cache
async def fetch_index_fields_for_index(
    conn: AsyncConnection, schema: str, index_name: str
) -> list[IndexKeyField]:
    """Return key fields for a single index, ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT a.attname, (ix.indoption[k.ord - 1] & 1) <> 0"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND ic.relname = %s AND k.ord <= ix.indnkeyatts"
        " ORDER BY k.ord",
        [schema, index_name],
    )
    return [
        IndexKeyField(name=r[0], direction="desc" if r[1] else "asc")
        for r in await cur.fetchall()
    ]


@_conn_cache
async def fetch_index_included_for_index(
    conn: AsyncConnection, schema: str, index_name: str
) -> list[str]:
    """Return INCLUDE columns for a single index, ordered by column position."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT a.attname"
        " FROM pg_index ix"
        " JOIN pg_class ic ON ic.oid = ix.indexrelid"
        " JOIN pg_class tc ON tc.oid = ix.indrelid"
        " JOIN pg_namespace n ON n.oid = tc.relnamespace"
        " CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord)"
        " JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = k.attnum"
        " WHERE n.nspname = %s AND ic.relname = %s AND k.ord > ix.indnkeyatts"
        " ORDER BY k.ord",
        [schema, index_name],
    )
    return [r[0] for r in await cur.fetchall()]


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
# Column describe queries
# ---------------------------------------------------------------------------


@_conn_cache
async def fetch_table_comment(
    conn: AsyncConnection, schema: str, table: str
) -> str | None:
    """Return the table-level comment, or None if unsupported or not set."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT d.description"
        " FROM pg_description d"
        " JOIN pg_class c ON d.objoid = c.oid"
        " JOIN pg_namespace n ON c.relnamespace = n.oid"
        " WHERE n.nspname = %s AND c.relname = %s AND d.objsubid = 0",
        [schema, table],
    )
    row = await cur.fetchone()
    return row[0].strip() if row and row[0] and row[0].strip() else None


@_conn_cache
async def fetch_all_column_comments(
    conn: AsyncConnection, schema: str, table: str
) -> dict[str, str | None]:
    """Return ``{column_name: comment}`` for commented columns; absent columns have no comment."""
    cur = conn.cursor()
    await cur.execute(
        "SELECT a.attname, d.description"
        " FROM pg_description d"
        " JOIN pg_class c ON d.objoid = c.oid"
        " JOIN pg_namespace n ON c.relnamespace = n.oid"
        " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid"
        " WHERE n.nspname = %s AND c.relname = %s AND d.objsubid > 0",
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
        query = sql.SQL(
            "SELECT DISTINCT {col} FROM {schema}.{table} WHERE {col} IS NOT NULL LIMIT %s"
        ).format(
            col=sql.Identifier(col_name),
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
        )
        await cur.execute(query, [n])
        return [r[0] for r in await cur.fetchall()]
    except Exception:
        return []


@_conn_cache
async def fetch_table_sample_rows(
    conn: AsyncConnection, schema: str, table: str
) -> tuple[list[str], list[tuple]]:
    """Return the column names and first :data:`SAMPLE_SCAN_ROWS` rows of *table*.

    A single scan replacing one ``SELECT DISTINCT`` round trip per column;
    per-column sample values are derived client-side by the driver.
    """
    cur = conn.cursor()
    try:
        query = sql.SQL("SELECT * FROM {schema}.{table} LIMIT %s").format(
            schema=sql.Identifier(schema), table=sql.Identifier(table)
        )
        await cur.execute(query, [SAMPLE_SCAN_ROWS])
        columns = [d.name for d in cur.description or []]
        return columns, await cur.fetchall()
    except Exception:
        return [], []


def render_lob(value: Any) -> Any:
    """Render a BYTEA value as a :class:`LobPlaceholder` instead of inlining it in the row.

    psycopg fully materializes BYTEA columns as plain ``bytes``, but ``bytes``
    still isn't JSON-serialisable and can be arbitrarily large, so it's
    swapped for a placeholder like Oracle's CLOB/BLOB handling.
    """
    if not isinstance(value, (bytes, bytearray)):
        return value
    return LobPlaceholder(text=f"BYTEA ({len(value)} bytes)")
