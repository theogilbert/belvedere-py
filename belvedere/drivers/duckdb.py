import asyncio
import dataclasses
import re
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

import duckdb

from ..protocol import (
    ColumnDescription,
    ColumnInfo,
    ColumnsDescription,
    DescribeResult,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    IndicesDescription,
    Language,
    LobPlaceholder,
    ParamType,
    ReadResult,
    TableDescription,
    TableReference,
    WriteResult,
)
from .base import BaseDriver, DriverError, DriverSettings

T = TypeVar("T")


class DuckDBDriver(BaseDriver):
    """DuckDB driver backed by the duckdb package.

    Args:
        params: Connect request fields.
        conn: Open DuckDB connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "DuckDB"
    LANGUAGES = [Language.SQL]
    DEFAULT_IDLE_TIMEOUT: ClassVar[float] = 0
    """File-based driver; idle connections are never closed automatically."""

    PARAMS: list[DriverParam] = [
        DriverParam(
            key="database",
            type=ParamType.STRING,
            label="Database file path or :memory:",
            required=False,
            default=":memory:",
        ),
    ]

    HELP: str = """\
## DuckDB

**Install:** `pip install 'belvedere-py[duckdb]'`

| Parameter  | Required | Default    | Description               |
|------------|----------|------------|---------------------------|
| `database` | no       | `:memory:` | File path or `:memory:`   |

**Queries:** Standard SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM read_parquet('/path/to/file.parquet')
SELECT * FROM read_csv('/path/to/file.csv', header = true)
SELECT * FROM 'glob/**/*.parquet'
```

**Explore tree:**

```
(root)
└── <schema>
    └── <table|view>
        ├── columns       → name, type
        ├── indices       → index name
        └── foreign_keys  → "col → ref_table.ref_col"
```
"""

    def __init__(
        self,
        params: dict[str, Any],
        conn: duckdb.DuckDBPyConnection,
        settings: DriverSettings,
    ) -> None:
        super().__init__(params, settings)
        self._conn = conn

    @classmethod
    async def create(
        cls, params: dict[str, Any], settings: DriverSettings
    ) -> "DuckDBDriver":
        """Open a DuckDB connection and return a ready-to-use driver.

        Args:
            params: May contain ``database`` (file path or ``:memory:``,
                defaults to ``:memory:``).
        """
        database = params.get("database") or ":memory:"
        try:
            conn = await asyncio.get_running_loop().run_in_executor(
                None, lambda: duckdb.connect(database)
            )
        except duckdb.Error as exc:
            raise DriverError(str(exc)) from exc
        return cls(params, conn, settings)

    async def reconnect(self) -> None:
        database = self.params.get("database") or ":memory:"
        self._conn = await asyncio.get_running_loop().run_in_executor(
            None, lambda: duckdb.connect(database)
        )

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a SQL statement.

        Args:
            query: SQL statement to execute.
            binds: Positional bind parameters (``?`` placeholders).
        """
        return await self._run(self._execute_sync, query, binds)

    def _execute_sync(self, sql: str, binds: list[Any]) -> ReadResult | WriteResult:
        cur = self._conn.execute(sql, binds)
        desc = cur.description
        # DuckDB returns a synthetic "Count" column for DML instead of description=None
        if desc is None or (len(desc) == 1 and desc[0][0] == "Count"):
            row = cur.fetchone() if desc is not None else None
            return WriteResult(rows_affected=int(row[0]) if row else 0)
        columns = [d[0] for d in desc]
        rows: list[list[Any]] = [[_render_lob(v) for v in r] for r in cur.fetchall()]
        return ReadResult(columns=columns, rows=rows, rows_total=len(rows))

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        """List child nodes at the given path in the DuckDB object tree.

        Args:
            path: Path segments (e.g. ``[]`` for schemas, ``["main"]`` for tables).
        """
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                rows = self._conn.execute(
                    "SELECT schema_name FROM information_schema.schemata"
                    " WHERE catalog_name = current_catalog()"
                    " AND schema_name NOT IN ('information_schema', 'pg_catalog')"
                    " ORDER BY schema_name"
                ).fetchall()
                return [
                    ExploreItem(name=r[0], type="schema", expandable=True) for r in rows
                ]

            case [schema]:
                rows = self._conn.execute(
                    "SELECT table_name, table_type FROM information_schema.tables"
                    " WHERE table_schema = ? ORDER BY table_name",
                    [schema],
                ).fetchall()
                return [
                    ExploreItem(
                        name=r[0],
                        type="view" if r[1] == "VIEW" else "table",
                        expandable=True,
                    )
                    for r in rows
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indices", type="group", expandable=True),
                    ExploreItem(name="foreign_keys", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                rows = self._conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns"
                    " WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    [schema, table],
                ).fetchall()
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False) for r in rows
                ]

            case [schema, table, "indices"]:
                rows = self._conn.execute(
                    "SELECT index_name FROM duckdb_indexes()"
                    " WHERE schema_name = ? AND table_name = ? ORDER BY index_name",
                    [schema, table],
                ).fetchall()
                return [
                    ExploreItem(name=r[0], type="index", expandable=False) for r in rows
                ]

            case [schema, table, "foreign_keys"]:
                items = []
                for src_cols, fk_table, fk_cols in self._outgoing_fk_rows_sync(
                    schema, table
                ):
                    src = ", ".join(src_cols)
                    ref = ", ".join(fk_cols)
                    items.append(
                        ExploreItem(
                            name=f"{src} → {fk_table}.{ref}",
                            type="foreign_key",
                            expandable=False,
                        )
                    )
                return items

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(
                    f'SELECT * FROM "{schema}"."{table}" LIMIT 10', []
                )
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        """Return metadata for the node at the given path.

        Args:
            path: ``[schema, table]`` for a table description,
                ``[schema, table, "indices"]`` for all indexes, or
                ``[schema, table, "indices", index_name]`` for one index.
        """
        match path:
            case [schema, table, "columns"]:
                base = await self._run(self._describe_columns_sync, schema, table)
                columns = []
                for col in base.columns:
                    sample = await self._fetch_sample(schema, table, col.name)
                    columns.append(dataclasses.replace(col, sample=sample))
                return ColumnsDescription(columns=columns)
            case [schema, table, "columns", col_name]:
                base = await self._run(
                    self._describe_column_sync, schema, table, col_name
                )
                if base is None:
                    return None
                sample = await self._fetch_sample(schema, table, col_name)
                return dataclasses.replace(base, sample=sample)
            case _:
                return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> DescribeResult:
        match path:
            case [schema, table]:
                col_rows = self._conn.execute(
                    "SELECT column_name, data_type, is_nullable"
                    " FROM information_schema.columns"
                    " WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                    [schema, table],
                ).fetchall()
                pk_rows = self._conn.execute(
                    "SELECT constraint_column_names FROM duckdb_constraints()"
                    " WHERE schema_name = ? AND table_name = ? AND constraint_type = 'PRIMARY KEY'",
                    [schema, table],
                ).fetchall()
                pk_cols: set[str] = set(pk_rows[0][0]) if pk_rows else set()
                idx_rows = self._conn.execute(
                    "SELECT index_name, sql FROM duckdb_indexes()"
                    " WHERE schema_name = ? AND table_name = ?",
                    [schema, table],
                ).fetchall()
                col_indexes: dict[str, list[str]] = {}
                index_col_count: dict[str, int] = {}
                for idx_name, sql in idx_rows:
                    key_fields = _parse_index_columns(sql)
                    index_col_count[idx_name] = len(key_fields)
                    for key_field in key_fields:
                        col_indexes.setdefault(key_field.name, []).append(idx_name)
                table_comment: str | None = None
                try:
                    comment_rows = self._conn.execute(
                        "SELECT comment FROM duckdb_tables()"
                        " WHERE schema_name = ? AND table_name = ?",
                        [schema, table],
                    ).fetchall()
                    table_comment = (
                        comment_rows[0][0]
                        if comment_rows and comment_rows[0][0]
                        else None
                    )
                except Exception:
                    pass
                return TableDescription(
                    table=table,
                    schema=schema,
                    comment=table_comment,
                    columns=[
                        ColumnInfo(
                            name=r[0],
                            type=r[1],
                            nullable=r[2] == "YES",
                            pk=r[0] in pk_cols,
                            exclusive_index=any(
                                index_col_count[i] == 1
                                for i in col_indexes.get(r[0], [])
                            ),
                            composite_index=any(
                                index_col_count[i] > 1
                                for i in col_indexes.get(r[0], [])
                            ),
                        )
                        for r in col_rows
                    ],
                    outgoing_references=self._outgoing_references_sync(schema, table),
                    incoming_references=self._incoming_references_sync(schema, table),
                )

            case [schema, table, "indices"]:
                return self._describe_indices_sync(schema, table)

            case [schema, table, "indices", index_name]:
                rows = self._conn.execute(
                    "SELECT is_unique, sql FROM duckdb_indexes()"
                    " WHERE schema_name = ? AND table_name = ? AND index_name = ?",
                    [schema, table, index_name],
                ).fetchall()
                if not rows:
                    return None
                is_unique, sql = rows[0]
                return IndexDescription(
                    index=index_name,
                    fields=_parse_index_columns(sql) if sql else [],
                    unique=bool(is_unique),
                    tables=[table],
                    ddl=sql,
                )

            case _:
                return None

    def _describe_indices_sync(self, schema: str, table: str) -> IndicesDescription:
        rows = self._conn.execute(
            "SELECT index_name, is_unique, sql FROM duckdb_indexes()"
            " WHERE schema_name = ? AND table_name = ? ORDER BY index_name",
            [schema, table],
        ).fetchall()
        return IndicesDescription(
            indices=[
                IndexDescription(
                    index=idx_name,
                    fields=_parse_index_columns(sql) if sql else [],
                    unique=bool(is_unique),
                    tables=[table],
                    ddl=sql,
                )
                for idx_name, is_unique, sql in rows
            ]
        )

    def _describe_columns_sync(self, schema: str, table: str) -> ColumnsDescription:
        col_rows = self._conn.execute(
            "SELECT column_name, data_type, is_nullable"
            " FROM information_schema.columns"
            " WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, table],
        ).fetchall()
        pk_rows = self._conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints()"
            " WHERE schema_name = ? AND table_name = ? AND constraint_type = 'PRIMARY KEY'",
            [schema, table],
        ).fetchall()
        pk_cols: set[str] = set(pk_rows[0][0]) if pk_rows else set()

        idx_desc_list = self._describe_indices_sync(schema, table).indices
        col_excl: dict[str, list[IndexDescription]] = {}
        col_comp: dict[str, list[IndexDescription]] = {}
        for idx_desc in idx_desc_list:
            key_col_names = [f.name for f in idx_desc.fields]
            for cn in key_col_names:
                if len(key_col_names) == 1:
                    col_excl.setdefault(cn, []).append(idx_desc)
                else:
                    col_comp.setdefault(cn, []).append(idx_desc)

        col_comments: dict[str, str | None] = {}
        try:
            for r in self._conn.execute(
                "SELECT column_name, comment FROM duckdb_columns()"
                " WHERE schema_name = ? AND table_name = ?",
                [schema, table],
            ).fetchall():
                col_comments[r[0]] = r[1] if r[1] else None
        except Exception:
            pass

        result = []
        for r in col_rows:
            cn = r[0]
            result.append(
                ColumnDescription(
                    name=cn,
                    data_type=r[1] or "",
                    nullable=r[2] == "YES",
                    pk=cn in pk_cols,
                    exclusive_indices=col_excl.get(cn, []),
                    composite_indices=col_comp.get(cn, []),
                    comment=col_comments.get(cn),
                )
            )
        return ColumnsDescription(columns=result)

    def _describe_column_sync(
        self, schema: str, table: str, col_name: str
    ) -> ColumnDescription | None:
        col_rows = self._conn.execute(
            "SELECT column_name, data_type, is_nullable"
            " FROM information_schema.columns"
            " WHERE table_schema = ? AND table_name = ? AND column_name = ?",
            [schema, table, col_name],
        ).fetchall()
        if not col_rows:
            return None
        r = col_rows[0]

        pk_rows = self._conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints()"
            " WHERE schema_name = ? AND table_name = ? AND constraint_type = 'PRIMARY KEY'",
            [schema, table],
        ).fetchall()
        pk_cols: set[str] = set(pk_rows[0][0]) if pk_rows else set()

        idx_desc_list = self._describe_indices_sync(schema, table).indices
        exclusive_indices = []
        composite_indices = []
        for idx_desc in idx_desc_list:
            key_col_names = [f.name for f in idx_desc.fields]
            if col_name not in key_col_names:
                continue
            if len(key_col_names) == 1:
                exclusive_indices.append(idx_desc)
            else:
                composite_indices.append(idx_desc)

        comment: str | None = None
        try:
            rows = self._conn.execute(
                "SELECT comment FROM duckdb_columns()"
                " WHERE schema_name = ? AND table_name = ? AND column_name = ?",
                [schema, table, col_name],
            ).fetchall()
            comment = rows[0][0] if rows and rows[0][0] else None
        except Exception:
            pass

        return ColumnDescription(
            name=col_name,
            data_type=r[1] or "",
            nullable=r[2] == "YES",
            pk=col_name in pk_cols,
            exclusive_indices=exclusive_indices,
            composite_indices=composite_indices,
            comment=comment,
        )

    def _outgoing_fk_rows_sync(
        self, schema: str, table: str
    ) -> list[tuple[list[str], str, list[str]]]:
        """Raw (local_columns, ref_table, ref_columns) rows for a table's foreign keys."""
        return self._conn.execute(
            "SELECT constraint_column_names, referenced_table, referenced_column_names"
            " FROM duckdb_constraints()"
            " WHERE schema_name = ? AND table_name = ? AND constraint_type = 'FOREIGN KEY'",
            [schema, table],
        ).fetchall()

    def _outgoing_references_sync(
        self, schema: str, table: str
    ) -> list[TableReference]:
        rows = self._outgoing_fk_rows_sync(schema, table)
        unique_cols = self._unique_columns_sync(schema, table)
        return [
            TableReference(
                column=src_col,
                table=fk_table,
                ref_column=ref_col,
                schema=schema,
                unique=src_col in unique_cols,
            )
            for src_cols, fk_table, fk_cols in rows
            for src_col, ref_col in zip(src_cols, fk_cols)
        ]

    def _incoming_references_sync(
        self, schema: str, table: str
    ) -> list[TableReference]:
        rows = self._conn.execute(
            "SELECT table_name, constraint_column_names, referenced_column_names"
            " FROM duckdb_constraints()"
            " WHERE schema_name = ? AND referenced_table = ? AND constraint_type = 'FOREIGN KEY'",
            [schema, table],
        ).fetchall()
        references = []
        for other_table, fk_cols, ref_cols in rows:
            unique_cols = self._unique_columns_sync(schema, other_table)
            references.extend(
                TableReference(
                    column=ref_col,
                    table=other_table,
                    ref_column=fk_col,
                    schema=schema,
                    unique=fk_col in unique_cols,
                )
                for fk_col, ref_col in zip(fk_cols, ref_cols)
            )
        return references

    def _unique_columns_sync(self, schema: str, table: str) -> set[str]:
        """Columns constrained to unique values by a single-column PK or
        UNIQUE constraint. DuckDB doesn't back these with a listed entry in
        ``duckdb_indexes()``, so ``duckdb_constraints()`` is the source of truth."""
        rows = self._conn.execute(
            "SELECT constraint_column_names FROM duckdb_constraints()"
            " WHERE schema_name = ? AND table_name = ?"
            " AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')",
            [schema, table],
        ).fetchall()
        return {cols[0] for (cols,) in rows if len(cols) == 1}

    async def _fetch_sample(self, schema: str, table: str, col_name: str) -> list[Any]:
        try:
            return await asyncio.wait_for(
                self._run(self._fetch_sample_sync, schema, table, col_name),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return []

    def _fetch_sample_sync(self, schema: str, table: str, col_name: str) -> list[Any]:
        try:
            return [
                row[0]
                for row in self._conn.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{schema}"."{table}"'
                    f' WHERE "{col_name}" IS NOT NULL LIMIT {self._settings.column_sample_size}'
                ).fetchall()
            ]
        except Exception:
            return []

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: fn(*args, **kwargs)
            )
        except duckdb.Error as exc:
            raise DriverError(str(exc)) from exc


def _render_lob(value: Any) -> Any:
    """Render a BLOB value as a :class:`LobPlaceholder` instead of inlining it in the row.

    DuckDB fully materializes BLOB columns as plain ``bytes``, but ``bytes``
    still isn't JSON-serialisable and can be arbitrarily large, so it's
    swapped for a placeholder like Oracle's CLOB/BLOB handling.
    """
    if not isinstance(value, (bytes, bytearray)):
        return value
    return LobPlaceholder(text=f"BLOB ({len(value)} bytes)")


def _parse_index_columns(sql: str) -> list[IndexKeyField]:
    """Extract column names and sort directions from a CREATE INDEX SQL string."""
    m = re.search(r"\(([^)]+)\)", sql)
    if not m:
        return []
    fields = []
    for part in m.group(1).split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        name = tokens[0].strip('"')
        direction = "desc" if len(tokens) > 1 and tokens[1].upper() == "DESC" else "asc"
        fields.append(IndexKeyField(name=name, direction=direction))
    return fields
