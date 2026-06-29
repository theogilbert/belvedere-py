"""SQL Server driver — requires: pip install mssql-python"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import mssql_python

from ..protocol import (
    ColumnDescription,
    ColumnInfo,
    ColumnsDescription,
    DescribeResult,
    DriverParam,
    DriverParamChoice,
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
from .base import BaseDriver, ConnectionLostError, DriverError

T = TypeVar("T")

_READ_ONLY_INTENT = "READ_ONLY"


class SQLServerDriver(BaseDriver):
    """SQL Server driver backed by mssql-python.

    Args:
        params: Connect request fields (``host``, ``port``, ``user``, ``password``,
            ``database``, ``applicationIntent``).
        conn: Open mssql_python connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "SQL Server"
    LANGUAGES = [Language.SQL]

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type=ParamType.STRING, label="Host"),
        DriverParam(key="port", type=ParamType.INTEGER, label="Port", default=1433),
        DriverParam(key="database", type=ParamType.STRING, label="Database"),
        DriverParam(key="user", type=ParamType.STRING, label="User"),
        DriverParam(
            key="applicationIntent",
            type=ParamType.ENUM,
            label="Application Intent",
            choices=[
                DriverParamChoice(value="READ_WRITE", label="READ_WRITE"),
                DriverParamChoice(value="READ_ONLY", label="READ_ONLY"),
            ],
        ),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            secret=True,
            required=False,
        ),
    ]

    HELP: str = """\
## SQL Server

**Install:** `pip install mssql-python`

| Parameter             | Required | Default     | Description                 |
|-----------------------|----------|-------------|------------------------------|
| `host`                | no       | `localhost` | Server hostname or IP        |
| `port`                | no       | `1433`      | TCP port                     |
| `database`            | no       | —           | Database name                |
| `user`                | no       | —           | Login name                   |
| `password`            | no       | —           | Password (masked)            |
| `applicationIntent`   | no       | —           | `READ_WRITE` or `READ_ONLY`  |

**Queries:** Standard T-SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM dbo.orders WHERE status = ?
```

**Explore tree:**

```
(root)
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indices      → name, type (e.g. CLUSTERED)
        └── constraints  → name, type (e.g. primary_key, foreign_key)
```

System schemas (`sys`, `INFORMATION_SCHEMA`, `guest`, `db_*`) are hidden.

`explore.describe` is supported on `[schema, table]` paths and returns full
column metadata (name, type, nullability, default).
"""

    def __init__(self, params: dict[str, Any], conn: "mssql_python.Connection") -> None:
        super().__init__(params)
        self._conn = conn

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "SQLServerDriver":
        """Open a SQL Server connection and return a ready-to-use driver.

        Args:
            params: Connect request fields; see class docstring for supported keys.

        Returns:
            A connected SQLServerDriver instance.
        """
        return cls(params, await cls._open(params))

    async def reconnect(self) -> None:
        self._conn = await self._open(self.params)

    async def disconnect(self) -> None:
        await self._run(self._conn.close)

    @staticmethod
    async def _open(params: dict[str, Any]) -> mssql_python.Connection:
        intent = params.get("applicationIntent", "")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None,
                lambda: mssql_python.connect(
                    server=f"{params.get('host', 'localhost')},{params.get('port', 1433)}",
                    uid=params.get("user", ""),
                    pwd=params.get("password", ""),
                    database=params.get("database", ""),
                    intent=intent,
                    autocommit=intent == _READ_ONLY_INTENT,
                    trustservercertificate="yes",
                ),
            )
        except Exception as exc:
            raise DriverError(str(exc)) from exc

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a SQL statement.

        Args:
            query: SQL statement to execute.
            binds: Positional bind parameters (``?`` placeholders).

        Returns:
            ReadResult for queries that return rows, DMLResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        try:
            return await self._run(self._execute_sync, query, binds)
        except Exception as exc:
            if isinstance(
                exc, (mssql_python.OperationalError, mssql_python.InterfaceError)
            ):
                raise ConnectionLostError(str(exc)) from exc
            raise DriverError(str(exc)) from exc

    def _execute_sync(self, sql: str, binds: list[Any]) -> ReadResult | WriteResult:

        cur = self._conn.execute(sql, binds)
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows: list[list[Any]] = [list(r) for r in cur.fetchall()]  # ty: ignore[missing-argument]
            return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
        return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        """List child nodes at the given path in the SQL Server object tree.

        Args:
            path: Path segments (``[]`` for schemas, ``[schema]`` for tables,
                ``[schema, table]`` for groups, etc.).

        Returns:
            Child nodes, or an empty list if the path is unrecognised.
        """
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:

        cur = self._conn.cursor()
        match path:
            case []:
                cur.execute(
                    "SELECT name FROM sys.schemas"
                    " WHERE name NOT IN ('sys','INFORMATION_SCHEMA','guest','db_owner',"
                    "'db_accessadmin','db_securityadmin','db_ddladmin','db_backupoperator',"
                    "'db_datareader','db_datawriter','db_denydatareader','db_denydatawriter')"
                    " ORDER BY name"
                )
                return [
                    ExploreItem(name=r[0], type="schema", expandable=True)
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case [schema]:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES"
                    " WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
                    (schema,),
                )
                return [
                    ExploreItem(name=r[0], type=r[1].lower(), expandable=True)
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indices", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY ORDINAL_POSITION",
                    (schema, table),
                )
                return [
                    ExploreItem(name=r[0], type=r[1], expandable=False)
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case [schema, table, "indices"]:
                cur.execute(
                    "SELECT i.name, i.type_desc"
                    " FROM sys.indexes i"
                    " JOIN sys.objects o ON i.object_id = o.object_id"
                    " JOIN sys.schemas s ON o.schema_id = s.schema_id"
                    " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL"
                    " ORDER BY i.name",
                    (schema, table),
                )
                return [
                    ExploreItem(name=r[0], type=r[1].lower(), expandable=False)
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case [schema, table, "constraints"]:
                cur.execute(
                    "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE"
                    " FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY CONSTRAINT_NAME",
                    (schema, table),
                )
                return [
                    ExploreItem(
                        name=r[0],
                        type=r[1].lower().replace(" ", "_"),
                        expandable=False,
                    )
                    for r in cur.fetchall()  # ty: ignore[missing-argument]
                ]

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(
                    f"SELECT TOP 10 * FROM [{schema}].[{table}]", []
                )
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        """Return column metadata for the table at the given path.

        Args:
            path: Two-element path ``[schema, table]``, ``[schema, table, "indices"]``
                for all indexes, or ``[schema, table, "indices", index_name]`` for one.

        Returns:
            TableDescription, IndicesDescription, or IndexDescription depending on the path.
        """
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> DescribeResult:

        match path:
            case [schema, table]:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT"
                    " FROM INFORMATION_SCHEMA.COLUMNS"
                    " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
                    " ORDER BY ORDINAL_POSITION",
                    (schema, table),
                )
                col_rows = cur.fetchall()  # ty: ignore[missing-argument]
                cur.execute(
                    "SELECT c.name, i.name"
                    " FROM sys.indexes i"
                    " JOIN sys.index_columns ic"
                    "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id"
                    " JOIN sys.columns c"
                    "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
                    " JOIN sys.objects o ON i.object_id = o.object_id"
                    " JOIN sys.schemas s ON o.schema_id = s.schema_id"
                    " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL",
                    (schema, table),
                )
                col_indexes: dict[str, list[str]] = {}
                index_cols: dict[str, set[str]] = {}
                for col_name, idx_name in cur.fetchall():  # ty: ignore[missing-argument]
                    col_indexes.setdefault(col_name, []).append(idx_name)
                    index_cols.setdefault(idx_name, set()).add(col_name)
                index_col_count = {k: len(v) for k, v in index_cols.items()}
                return TableDescription(
                    table=table,
                    schema=schema,
                    columns=[
                        ColumnInfo(
                            name=r[0],
                            type=r[1],
                            nullable=r[2] == "YES",
                            default=r[3],
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
                )

            case [schema, table, "indices"]:
                return self._describe_indices_sync(schema, table)

            case [schema, table, "indices", index_name]:
                return self._describe_index_sync(schema, table, index_name)

            case [schema, table, "columns"]:
                return self._describe_columns_sync(schema, table)

            case [schema, table, "columns", col_name]:
                return self._describe_column_sync(schema, table, col_name)

            case _:
                return None

    def _describe_indices_sync(self, schema: str, table: str) -> IndicesDescription:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT i.name, i.type_desc, i.is_unique,"
            "       i.is_disabled, i.filter_definition"
            " FROM sys.indexes i"
            " JOIN sys.objects o ON i.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL"
            " ORDER BY i.name",
            (schema, table),
        )
        index_rows = cur.fetchall()  # ty: ignore[missing-argument]

        cur.execute(
            "SELECT i.name, c.name, ic.is_descending_key, ic.is_included_column"
            " FROM sys.indexes i"
            " JOIN sys.index_columns ic"
            "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id"
            " JOIN sys.columns c"
            "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
            " JOIN sys.objects o ON i.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND i.name IS NOT NULL"
            " ORDER BY i.name, ic.key_ordinal, ic.index_column_id",
            (schema, table),
        )
        index_fields: dict[str, list[IndexKeyField]] = {}
        index_included: dict[str, list[str]] = {}
        for idx_name, col_name, is_desc, is_included in cur.fetchall():  # ty: ignore[missing-argument]
            if is_included:
                index_included.setdefault(idx_name, []).append(col_name)
            else:
                index_fields.setdefault(idx_name, []).append(
                    IndexKeyField(name=col_name, direction="desc" if is_desc else "asc")
                )

        indices = []
        for (
            idx_name,
            type_desc,
            is_unique,
            is_disabled,
            filter_def,
        ) in index_rows:
            is_clustered = (type_desc or "").upper() == "CLUSTERED"
            fields = index_fields.get(idx_name, [])
            included = index_included.get(idx_name, [])
            condition = filter_def.strip() if filter_def else None
            ddl = _build_sqlserver_ddl(
                idx_name,
                schema,
                table,
                type_desc,
                bool(is_unique),
                fields,
                included,
                condition,
            )
            indices.append(
                IndexDescription(
                    index=idx_name,
                    fields=fields,
                    unique=bool(is_unique),
                    tables=[table],
                    index_type=type_desc.lower() if type_desc else None,
                    clustered=bool(is_clustered),
                    visible=not bool(is_disabled),
                    included_columns=included,
                    condition=condition,
                    ddl=ddl,
                )
            )
        return IndicesDescription(indices=indices)

    def _describe_index_sync(
        self, schema: str, table: str, index_name: str
    ) -> IndexDescription | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT i.type_desc, i.is_unique,"
            "       i.is_disabled, i.filter_definition"
            " FROM sys.indexes i"
            " JOIN sys.objects o ON i.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND i.name = ?",
            (schema, table, index_name),
        )
        row = cur.fetchone()  # ty: ignore[missing-argument]
        if row is None:
            return None
        type_desc, is_unique, is_disabled, filter_def = row
        is_clustered = (type_desc or "").upper() == "CLUSTERED"

        cur.execute(
            "SELECT c.name, ic.is_descending_key, ic.is_included_column"
            " FROM sys.indexes i"
            " JOIN sys.index_columns ic"
            "  ON i.object_id = ic.object_id AND i.index_id = ic.index_id"
            " JOIN sys.columns c"
            "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
            " JOIN sys.objects o ON i.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND i.name = ?"
            " ORDER BY ic.key_ordinal, ic.index_column_id",
            (schema, table, index_name),
        )
        fields: list[IndexKeyField] = []
        included: list[str] = []
        for col_name, is_desc, is_included in cur.fetchall():  # ty: ignore[missing-argument]
            if is_included:
                included.append(col_name)
            else:
                fields.append(
                    IndexKeyField(name=col_name, direction="desc" if is_desc else "asc")
                )

        condition = filter_def.strip() if filter_def else None
        ddl = _build_sqlserver_ddl(
            index_name,
            schema,
            table,
            type_desc,
            bool(is_unique),
            fields,
            included,
            condition,
        )
        return IndexDescription(
            index=index_name,
            fields=fields,
            unique=bool(is_unique),
            tables=[table],
            index_type=type_desc.lower() if type_desc else None,
            clustered=bool(is_clustered),
            visible=not bool(is_disabled),
            included_columns=included,
            condition=condition,
            ddl=ddl,
        )

    def _describe_columns_sync(self, schema: str, table: str) -> ColumnsDescription:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT"
            " FROM INFORMATION_SCHEMA.COLUMNS"
            " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
            " ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        col_rows = cur.fetchall()  # ty: ignore[missing-argument]

        cur.execute(
            "SELECT c.name FROM sys.key_constraints kc"
            " JOIN sys.index_columns ic"
            "  ON kc.unique_index_id = ic.index_id AND kc.parent_object_id = ic.object_id"
            " JOIN sys.columns c"
            "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
            " JOIN sys.objects o ON kc.parent_object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND kc.type = 'PK'",
            (schema, table),
        )
        pk_cols: set[str] = {r[0] for r in cur.fetchall()}  # ty: ignore[missing-argument]

        cur.execute(
            "SELECT c.name, CAST(ep.value AS NVARCHAR(MAX))"
            " FROM sys.columns c"
            " JOIN sys.objects o ON c.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " LEFT JOIN sys.extended_properties ep"
            "  ON ep.major_id = c.object_id AND ep.minor_id = c.column_id"
            "  AND ep.name = 'MS_Description'"
            " WHERE s.name = ? AND o.name = ?",
            (schema, table),
        )
        col_comments: dict[str, str | None] = {
            r[0]: (r[1].strip() if r[1] else None)
            for r in cur.fetchall()  # ty: ignore[missing-argument]
        }

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

        result = []
        for r in col_rows:
            cn = r[0]
            try:
                cur.execute(
                    f"SELECT TOP 3 [{cn}] FROM"
                    f" (SELECT DISTINCT [{cn}] FROM [{schema}].[{table}]"
                    f"  WHERE [{cn}] IS NOT NULL) AS _s"
                )
                sample: list[Any] = [row[0] for row in cur.fetchall()]  # ty: ignore[missing-argument]
            except Exception:
                sample = []
            result.append(
                ColumnDescription(
                    name=cn,
                    data_type=r[1] or "",
                    nullable=r[2] == "YES",
                    pk=cn in pk_cols,
                    default=r[3],
                    exclusive_indices=col_excl.get(cn, []),
                    composite_indices=col_comp.get(cn, []),
                    comment=col_comments.get(cn),
                    sample=sample,
                )
            )
        return ColumnsDescription(columns=result)

    def _describe_column_sync(
        self, schema: str, table: str, col_name: str
    ) -> ColumnDescription | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT"
            " FROM INFORMATION_SCHEMA.COLUMNS"
            " WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
            (schema, table, col_name),
        )
        rows = cur.fetchall()  # ty: ignore[missing-argument]
        if not rows:
            return None
        r = rows[0]

        cur.execute(
            "SELECT c.name FROM sys.key_constraints kc"
            " JOIN sys.index_columns ic"
            "  ON kc.unique_index_id = ic.index_id AND kc.parent_object_id = ic.object_id"
            " JOIN sys.columns c"
            "  ON ic.object_id = c.object_id AND ic.column_id = c.column_id"
            " JOIN sys.objects o ON kc.parent_object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " WHERE s.name = ? AND o.name = ? AND kc.type = 'PK'",
            (schema, table),
        )
        pk_cols: set[str] = {row[0] for row in cur.fetchall()}  # ty: ignore[missing-argument]

        cur.execute(
            "SELECT CAST(ep.value AS NVARCHAR(MAX))"
            " FROM sys.columns c"
            " JOIN sys.objects o ON c.object_id = o.object_id"
            " JOIN sys.schemas s ON o.schema_id = s.schema_id"
            " LEFT JOIN sys.extended_properties ep"
            "  ON ep.major_id = c.object_id AND ep.minor_id = c.column_id"
            "  AND ep.name = 'MS_Description'"
            " WHERE s.name = ? AND o.name = ? AND c.name = ?",
            (schema, table, col_name),
        )
        comment_row = cur.fetchone()  # ty: ignore[missing-argument]
        comment: str | None = (
            comment_row[0].strip() if comment_row and comment_row[0] else None
        )

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

        try:
            cur.execute(
                f"SELECT TOP 3 [{col_name}] FROM"
                f" (SELECT DISTINCT [{col_name}] FROM [{schema}].[{table}]"
                f"  WHERE [{col_name}] IS NOT NULL) AS _s"
            )
            sample: list[Any] = [row[0] for row in cur.fetchall()]  # ty: ignore[missing-argument]
        except Exception:
            sample = []

        return ColumnDescription(
            name=col_name,
            data_type=r[1] or "",
            nullable=r[2] == "YES",
            pk=col_name in pk_cols,
            default=r[3],
            exclusive_indices=exclusive_indices,
            composite_indices=composite_indices,
            comment=comment,
            sample=sample,
        )

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs)
        )


def _build_sqlserver_ddl(
    index_name: str,
    schema: str,
    table: str,
    type_desc: str | None,
    is_unique: bool,
    fields: list[IndexKeyField],
    included: list[str],
    condition: str | None,
) -> str | None:
    """Construct a CREATE INDEX DDL string for standard CLUSTERED/NONCLUSTERED indexes."""
    td = (type_desc or "").upper()
    if td not in ("CLUSTERED", "NONCLUSTERED"):
        return None
    parts = ["CREATE"]
    if is_unique:
        parts.append("UNIQUE")
    parts.append(td)
    parts.append(f"INDEX [{index_name}]")
    parts.append(f"ON [{schema}].[{table}]")
    col_list = ", ".join(f"[{f.name}] {f.direction.upper()}" for f in fields)
    parts.append(f"({col_list})")
    if included:
        parts.append("INCLUDE (" + ", ".join(f"[{c}]" for c in included) + ")")
    if condition:
        parts.append(f"WHERE {condition}")
    return " ".join(parts)
