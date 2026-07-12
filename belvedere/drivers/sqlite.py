import asyncio
import dataclasses
import sqlite3
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

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
from .base import (
    BaseDriver,
    DriverError,
    DriverSettings,
    build_relationship_description,
)

T = TypeVar("T")


class SQLiteDriver(BaseDriver):
    """SQLite driver backed by the stdlib sqlite3 module.

    Args:
        params: Connect request fields; must include ``database`` (file path or ``":memory:"``).
        conn: Open sqlite3 connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "SQLite"
    LANGUAGES = [Language.SQL]
    DEFAULT_IDLE_TIMEOUT: ClassVar[float] = 0
    """File-based driver; idle connections are never closed automatically."""

    PARAMS: list[DriverParam] = [
        DriverParam(key="database", type=ParamType.STRING, label="Database file path"),
    ]

    HELP: str = """\
## SQLite

**Install:** none (stdlib)

| Parameter  | Required | Default | Description               |
|------------|----------|---------|---------------------------|
| `database` | yes      | —       | File path or `:memory:`   |

**Queries:** Standard SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM users WHERE age > ?
```

**Explore tree:**

```
(root)
└── <table|view>
    ├── columns       → name, type
    ├── indices       → index name
    └── foreign_keys  → "col → ref_table.ref_col"
```

`explore.describe` is supported on tables and views and returns full column
metadata (name, type, nullability, primary key flag).
"""

    def __init__(
        self, params: dict[str, Any], conn: sqlite3.Connection, settings: DriverSettings
    ) -> None:
        super().__init__(params, settings)
        self._conn = conn

    @classmethod
    async def create(
        cls, params: dict[str, Any], settings: DriverSettings
    ) -> "SQLiteDriver":
        """Open a SQLite connection and return a ready-to-use driver.

        Args:
            params: Must contain ``database`` — a file path or ``":memory:"``.

        Returns:
            A connected SQLiteDriver instance.
        """
        loop = asyncio.get_running_loop()
        try:
            conn = await loop.run_in_executor(
                None,
                lambda: sqlite3.connect(
                    params["database"], check_same_thread=False, isolation_level=None
                ),
            )
        except sqlite3.OperationalError as exc:
            raise DriverError(str(exc)) from exc
        return cls(params, conn, settings)

    async def reconnect(self) -> None:
        self._conn = await self._run(
            sqlite3.connect,
            self.params["database"],
            check_same_thread=False,
            isolation_level=None,
        )

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a SQL statement.

        Args:
            query: SQL statement to execute.
            binds: Positional bind parameters (``?`` placeholders).

        Returns:
            ReadResult for queries that return rows, DMLResult otherwise.
        """
        return await self._run(self._execute_sync, query, binds)

    def _execute_sync(self, sql: str, binds: list[Any]) -> ReadResult | WriteResult:

        cur = self._conn.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [
                [_render_lob(v) for v in r] for r in cur.fetchall()
            ]
            return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
        return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        """List child nodes at the given path in the SQLite object tree.

        Args:
            path: Path segments (e.g. ``[]`` for root, ``["users"]`` for a table's groups).

        Returns:
            Child nodes, or an empty list if the path is unrecognised.
        """
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:

        match path:
            case []:
                rows = self._conn.execute(
                    "SELECT name, type FROM sqlite_master"
                    " WHERE type IN ('table','view') ORDER BY name"
                ).fetchall()
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=True) for r in rows
                ]

            case [_table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indices", type="group", expandable=True),
                    ExploreItem(name="foreign_keys", type="group", expandable=True),
                ]

            case [table, "columns"]:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                return [
                    ExploreItem(name=r[1], type=r[2], expandable=False) for r in rows
                ]

            case [table, "indices"]:
                rows = self._conn.execute(f"PRAGMA index_list({table})").fetchall()
                return [
                    ExploreItem(name=r[1], type="index", expandable=False) for r in rows
                ]

            case [table, "foreign_keys"]:
                return [
                    ExploreItem(
                        name=f"{ref.column} → {ref.table}.{ref.ref_column}",
                        type="foreign_key",
                        expandable=False,
                    )
                    for ref in self._outgoing_references_sync(table)
                ]

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [table]:
                result = await self.execute(f"SELECT * FROM {table} LIMIT 10", [])
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        """Return column metadata for the table at the given path.

        Args:
            path: Single-element path with the table name (e.g. ``["users"]``),
                ``[table, "indices"]`` for all indexes, or
                ``[table, "indices", index_name]`` for a single index.

        Returns:
            TableDescription, IndicesDescription, or IndexDescription depending on the path.
        """
        match path:
            case [table, "columns"]:
                base = await self._run(self._describe_columns_sync, table)
                columns = []
                for col in base.columns:
                    sample = await self._fetch_sample(table, col.name)
                    columns.append(dataclasses.replace(col, sample=sample))
                return ColumnsDescription(columns=columns)
            case [table, "columns", col_name]:
                base = await self._run(self._describe_column_sync, table, col_name)
                if base is None:
                    return None
                sample = await self._fetch_sample(table, col_name)
                return dataclasses.replace(base, sample=sample)
            case _:
                return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> DescribeResult:
        match path:
            case [table]:
                cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                index_list = self._conn.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall()
                col_indexes: dict[str, list[str]] = {}
                index_col_count: dict[str, int] = {}
                for idx_row in index_list:
                    idx_name = idx_row[1]
                    xinfo = self._conn.execute(
                        f"PRAGMA index_xinfo({idx_name})"
                    ).fetchall()
                    key_cols = [r for r in xinfo if r[5]]
                    index_col_count[idx_name] = len(key_cols)
                    for r in key_cols:
                        col_indexes.setdefault(r[2], []).append(idx_name)
                return TableDescription(
                    table=table,
                    columns=[
                        ColumnInfo(
                            name=r[1],
                            type=r[2],
                            nullable=not bool(r[3]),
                            pk=bool(r[5]),
                            exclusive_index=any(
                                index_col_count[i] == 1
                                for i in col_indexes.get(r[1], [])
                            ),
                            composite_index=any(
                                index_col_count[i] > 1
                                for i in col_indexes.get(r[1], [])
                            ),
                        )
                        for r in cols
                    ],
                    outgoing_references=self._outgoing_references_sync(table),
                    incoming_references=self._incoming_references_sync(table),
                )

            case [table, "indices"]:
                return self._describe_indices_sync(table)

            case [table, "indices", index_name]:
                return self._describe_index_sync(table, index_name)

            case [table, "columns"]:
                return self._describe_columns_sync(table)

            case [table, "columns", col_name]:
                return self._describe_column_sync(table, col_name)

            case [table, "relationships", column]:
                desc = self._explore_describe_sync([table])
                if not isinstance(desc, TableDescription):
                    return None
                return build_relationship_description(desc, table, None, column)

            case _:
                return None

    def _describe_indices_sync(self, table: str) -> IndicesDescription:
        index_list = self._conn.execute(f"PRAGMA index_list({table})").fetchall()
        indices = []
        for idx_row in index_list:
            idx = self._describe_index_sync(table, idx_row[1])
            if idx is not None:
                indices.append(idx)
        return IndicesDescription(indices=indices)

    def _describe_index_sync(
        self, table: str, index_name: str
    ) -> IndexDescription | None:
        index_list = self._conn.execute(f"PRAGMA index_list({table})").fetchall()
        index_row = next((r for r in index_list if r[1] == index_name), None)
        if index_row is None:
            return None
        unique = bool(index_row[2])
        xinfo = self._conn.execute(f"PRAGMA index_xinfo({index_name})").fetchall()
        fields = [
            IndexKeyField(name=r[2], direction="desc" if r[3] else "asc")
            for r in xinfo
            if r[5]  # key=1: part of the index key; 0 = implicit rowid
        ]
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        ddl: str | None = row[0] if row and row[0] else None
        return IndexDescription(
            index=index_name,
            fields=fields,
            unique=unique,
            tables=[table],
            index_type="btree",
            ddl=ddl,
        )

    def _describe_columns_sync(self, table: str) -> ColumnsDescription:
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        idx_desc_list = self._describe_indices_sync(table).indices
        col_excl: dict[str, list[IndexDescription]] = {}
        col_comp: dict[str, list[IndexDescription]] = {}
        for idx_desc in idx_desc_list:
            key_col_names = [f.name for f in idx_desc.fields]
            for cn in key_col_names:
                if len(key_col_names) == 1:
                    col_excl.setdefault(cn, []).append(idx_desc)
                else:
                    col_comp.setdefault(cn, []).append(idx_desc)

        result = []
        for r in cols:
            cn = r[1]
            result.append(
                ColumnDescription(
                    name=cn,
                    data_type=r[2] or "",
                    nullable=not bool(r[3]),
                    pk=bool(r[5]),
                    exclusive_indices=col_excl.get(cn, []),
                    composite_indices=col_comp.get(cn, []),
                )
            )
        return ColumnsDescription(columns=result)

    def _describe_column_sync(
        self, table: str, col_name: str
    ) -> ColumnDescription | None:
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        row = next((r for r in cols if r[1] == col_name), None)
        if row is None:
            return None

        idx_desc_list = self._describe_indices_sync(table).indices
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

        return ColumnDescription(
            name=col_name,
            data_type=row[2] or "",
            nullable=not bool(row[3]),
            pk=bool(row[5]),
            exclusive_indices=exclusive_indices,
            composite_indices=composite_indices,
        )

    def _outgoing_references_sync(self, table: str) -> list[TableReference]:
        rows = self._conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        unique_cols = self._unique_columns_sync(table)
        return [
            TableReference(
                column=r[3], table=r[2], ref_column=r[4], unique=r[3] in unique_cols
            )
            for r in rows
        ]

    def _incoming_references_sync(self, table: str) -> list[TableReference]:
        other_tables = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name != ?",
            (table,),
        ).fetchall()
        references = []
        for (other_table,) in other_tables:
            rows = self._conn.execute(
                f"PRAGMA foreign_key_list({other_table})"
            ).fetchall()
            matching = [r for r in rows if r[2].lower() == table.lower()]
            if not matching:
                continue
            unique_cols = self._unique_columns_sync(other_table)
            references.extend(
                TableReference(
                    column=r[4],
                    table=other_table,
                    ref_column=r[3],
                    unique=r[3] in unique_cols,
                )
                for r in matching
            )
        return references

    def _unique_columns_sync(self, table: str) -> set[str]:
        """Columns constrained to unique values: the table's own PK (unless
        composite) or covered by a single-column UNIQUE index."""
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        pk_cols = [r[1] for r in cols if r[5]]
        unique = set(pk_cols) if len(pk_cols) == 1 else set()
        index_list = self._conn.execute(f"PRAGMA index_list({table})").fetchall()
        for idx_row in index_list:
            if not idx_row[2]:
                continue  # not a UNIQUE index
            xinfo = self._conn.execute(f"PRAGMA index_xinfo({idx_row[1]})").fetchall()
            key_cols = [r[2] for r in xinfo if r[5]]
            if len(key_cols) == 1:
                unique.add(key_cols[0])
        return unique

    async def _fetch_sample(self, table: str, col_name: str) -> list[Any]:
        try:
            return await asyncio.wait_for(
                self._run(self._fetch_sample_sync, table, col_name),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return []

    def _fetch_sample_sync(self, table: str, col_name: str) -> list[Any]:
        try:
            return [
                r[0]
                for r in self._conn.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{table}"'
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
        except sqlite3.Error as exc:
            raise DriverError(str(exc)) from exc


def _render_lob(value: Any) -> Any:
    """Render a BLOB value as a :class:`LobPlaceholder` instead of inlining it in the row.

    sqlite3 fully materializes BLOB columns as plain ``bytes``, but ``bytes``
    still isn't JSON-serialisable and can be arbitrarily large, so it's
    swapped for a placeholder like Oracle's CLOB/BLOB handling.
    """
    if not isinstance(value, (bytes, bytearray)):
        return value
    return LobPlaceholder(text=f"BLOB ({len(value)} bytes)")
