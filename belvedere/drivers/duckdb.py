import asyncio
import re
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

import duckdb

from ..protocol import (
    ColumnInfo,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from .base import BaseDriver, DriverError

T = TypeVar("T")


class DuckDBDriver(BaseDriver):
    """DuckDB driver backed by the duckdb package.

    Args:
        params: Connect request fields.
        conn: Open DuckDB connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "DuckDB"
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

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `database` | no | `:memory:` | File path or `:memory:` |

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

    def __init__(self, params: dict[str, Any], conn: duckdb.DuckDBPyConnection) -> None:
        super().__init__(params)
        self._conn = conn

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "DuckDBDriver":
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
        return cls(params, conn)

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
        rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
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
                rows = self._conn.execute(
                    "SELECT constraint_column_names, referenced_table, referenced_column_names"
                    " FROM duckdb_constraints()"
                    " WHERE schema_name = ? AND table_name = ? AND constraint_type = 'FOREIGN KEY'",
                    [schema, table],
                ).fetchall()
                items = []
                for src_cols, fk_table, fk_cols in rows:
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

    async def explore_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        """Return metadata for the node at the given path.

        Args:
            path: ``[schema, table]`` for a table description, or
                ``[schema, table, "indices", index_name]`` for an index description.
        """
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
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
                return TableDescription(
                    table=table,
                    schema=schema,
                    columns=[
                        ColumnInfo(
                            name=r[0],
                            type=r[1],
                            nullable=r[2] == "YES",
                            pk=r[0] in pk_cols,
                        )
                        for r in col_rows
                    ],
                )

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
                    fields=_parse_index_columns(sql),
                    unique=bool(is_unique),
                    entity=table,
                    condition=_parse_index_condition(sql),
                )

            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: fn(*args, **kwargs)
            )
        except duckdb.Error as exc:
            raise DriverError(str(exc)) from exc


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


def _parse_index_condition(sql: str) -> str | None:
    """Extract the WHERE clause from a partial index SQL string, or None."""
    where_pos = sql.upper().find(" WHERE ")
    if where_pos == -1:
        return None
    return sql[where_pos + 7 :].strip()
