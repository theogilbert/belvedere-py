import asyncio
import sqlite3
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

from ..protocol import (
    ColumnInfo,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    Language,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from .base import BaseDriver, DriverError

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

    def __init__(self, params: dict[str, Any], conn: sqlite3.Connection) -> None:
        super().__init__(params)
        self._conn = conn

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "SQLiteDriver":
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
        return cls(params, conn)

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

    async def explore_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        """Return column metadata for the table at the given path.

        Args:
            path: Single-element path with the table name (e.g. ``["users"]``).

        Returns:
            TableDescription if the path resolves to a table, None otherwise.
        """
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        match path:
            case [table]:
                cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                index_list = self._conn.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall()
                col_indexes: dict[str, list[str]] = {}
                for idx_row in index_list:
                    idx_name = idx_row[1]
                    xinfo = self._conn.execute(
                        f"PRAGMA index_xinfo({idx_name})"
                    ).fetchall()
                    for r in xinfo:
                        if r[5]:  # key column (not implicit rowid)
                            col_indexes.setdefault(r[2], []).append(idx_name)
                return TableDescription(
                    table=table,
                    columns=[
                        ColumnInfo(
                            name=r[1],
                            type=r[2],
                            nullable=not bool(r[3]),
                            pk=bool(r[5]),
                            indexes=col_indexes.get(r[1], []),
                        )
                        for r in cols
                    ],
                )
            case [table, "indices", index_name]:
                index_list = self._conn.execute(
                    f"PRAGMA index_list({table})"
                ).fetchall()
                index_row = next((r for r in index_list if r[1] == index_name), None)
                if index_row is None:
                    return None
                unique = bool(index_row[2])
                is_partial = bool(index_row[4])
                xinfo = self._conn.execute(
                    f"PRAGMA index_xinfo({index_name})"
                ).fetchall()
                fields = [
                    IndexKeyField(name=r[2], direction="desc" if r[3] else "asc")
                    for r in xinfo
                    if r[5]  # key=1: part of the index key; 0 = implicit rowid
                ]
                condition = None
                if is_partial:
                    row = self._conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                        (index_name,),
                    ).fetchone()
                    if row and row[0]:
                        sql: str = row[0]
                        where_pos = sql.upper().find(" WHERE ")
                        if where_pos != -1:
                            condition = sql[where_pos + 7 :].strip()
                return IndexDescription(
                    index=index_name,
                    fields=fields,
                    unique=unique,
                    condition=condition,
                )
            case _:
                return None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: fn(*args, **kwargs)
            )
        except sqlite3.Error as exc:
            raise DriverError(str(exc)) from exc
