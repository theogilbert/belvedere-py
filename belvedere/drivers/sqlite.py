import asyncio
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
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from .base import BaseDriver, DriverError, DriverSettings

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
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]
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
                rows = self._conn.execute(
                    f"PRAGMA foreign_key_list({table})"
                ).fetchall()
                return [
                    ExploreItem(
                        name=f"{r[3]} → {r[2]}.{r[4]}",
                        type="foreign_key",
                        expandable=False,
                    )
                    for r in rows
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
                )

            case [table, "indices"]:
                return self._describe_indices_sync(table)

            case [table, "indices", index_name]:
                return self._describe_index_sync(table, index_name)

            case [table, "columns"]:
                return self._describe_columns_sync(table)

            case [table, "columns", col_name]:
                return self._describe_column_sync(table, col_name)

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
            try:
                sample = [
                    row[0]
                    for row in self._conn.execute(
                        f'SELECT DISTINCT "{cn}" FROM "{table}"'
                        f' WHERE "{cn}" IS NOT NULL LIMIT {self._settings.column_sample_size}'
                    ).fetchall()
                ]
            except Exception:
                sample = []
            result.append(
                ColumnDescription(
                    name=cn,
                    data_type=r[2] or "",
                    nullable=not bool(r[3]),
                    pk=bool(r[5]),
                    exclusive_indices=col_excl.get(cn, []),
                    composite_indices=col_comp.get(cn, []),
                    sample=sample,
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

        try:
            sample = [
                r[0]
                for r in self._conn.execute(
                    f'SELECT DISTINCT "{col_name}" FROM "{table}"'
                    f' WHERE "{col_name}" IS NOT NULL LIMIT {self._settings.column_sample_size}'
                ).fetchall()
            ]
        except Exception:
            sample = []

        return ColumnDescription(
            name=col_name,
            data_type=row[2] or "",
            nullable=not bool(row[3]),
            pk=bool(row[5]),
            exclusive_indices=exclusive_indices,
            composite_indices=composite_indices,
            sample=sample,
        )

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: fn(*args, **kwargs)
            )
        except sqlite3.Error as exc:
            raise DriverError(str(exc)) from exc
