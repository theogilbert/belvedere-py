"""PostgreSQL driver — requires: pip install psycopg[binary]"""

import asyncio
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql

from ...protocol import (
    ColumnDescription,
    ColumnInfo,
    ColumnsDescription,
    DescribeResult,
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndicesDescription,
    Language,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from ..base import (
    BaseDriver,
    ConnectionLostError,
    DriverError,
    DriverSettings,
    build_column_samples,
    build_relationship_description,
    group_references_by_column,
)
from .copy import CopyToCommand, build_copy_to_statement, parse_copy_to
from .queries import (
    build_column_index_lists,
    build_preview_query,
    fetch_all_column_comments,
    fetch_column_details,
    fetch_column_index_mapping,
    fetch_column_names_and_types,
    fetch_column_sample,
    fetch_constraint_names_and_types,
    fetch_incoming_references,
    fetch_index_fields_for_index,
    fetch_index_fields_for_table,
    fetch_index_included_for_index,
    fetch_index_included_for_table,
    fetch_index_meta,
    fetch_index_metas_for_table,
    fetch_index_names_and_types,
    fetch_outgoing_references,
    fetch_pk_columns,
    fetch_schemas,
    fetch_table_comment,
    fetch_table_sample_rows,
    fetch_tables_and_views,
    invalidate_cache,
    render_lob,
)


class PostgresDriver(BaseDriver):
    """PostgreSQL driver backed by psycopg (v3, native asyncio support).

    Args:
        params: Connect request fields (``host``, ``port``, ``database``, ``user``, ``password``).
        conn: Open psycopg async connection. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "PostgreSQL"
    LANGUAGES = [Language.SQL]

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type=ParamType.STRING, label="Host"),
        DriverParam(key="port", type=ParamType.INTEGER, label="Port", default=5432),
        DriverParam(key="database", type=ParamType.STRING, label="Database"),
        DriverParam(key="user", type=ParamType.STRING, label="User"),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            secret=True,
            required=False,
        ),
    ]

    HELP: str = """\
## PostgreSQL

**Queries:** Standard SQL.

```sql
SELECT * FROM orders WHERE status = 'open'
SELECT o.id, c.name FROM orders o JOIN customers c ON c.id = o.customer_id
INSERT INTO orders (customer_id, status) VALUES (1, 'open') RETURNING id
```

**Exporting to a file:** `\\copy { table | (query) } TO '/local/path' [(options)]`,
same syntax as psql's `\\copy`. The result is streamed straight to a file on
the machine running grannos-py rather than returned as rows. `(options)` is
passed straight through to Postgres's `COPY` command, so any option it
accepts works here too — common ones are `FORMAT csv|text|binary`, `HEADER`,
`DELIMITER '|'`, and `NULL 'value'`. Full list:
https://www.postgresql.org/docs/current/sql-copy.html

```sql
\\copy orders TO '/tmp/orders.csv' (FORMAT csv, HEADER)
\\copy (SELECT * FROM orders WHERE status = 'open') TO '/tmp/open_orders.csv' (FORMAT csv, HEADER)
```

**Resources:**

```
(root)  ← non-system schemas (pg_namespace, excluding pg_catalog/information_schema/pg_*)
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indexes      → name, access method (e.g. btree, gin)
        └── constraints  → name, type (primary_key, unique, check, foreign_key)
```

Describing a table or view returns column metadata (name, type, nullability,
primary key flag, default). Describing an index returns its key fields,
direction, uniqueness, INCLUDE columns, and DDL (as reported by `pg_indexes`).
"""

    def __init__(
        self,
        params: dict[str, Any],
        conn: AsyncConnection,
        settings: DriverSettings,
    ) -> None:
        super().__init__(params, settings)
        self._conn = conn

    @classmethod
    async def create(
        cls, params: dict[str, Any], settings: DriverSettings
    ) -> "PostgresDriver":
        return cls(params, await cls._open(params), settings)

    @staticmethod
    async def _open(params: dict[str, Any]) -> AsyncConnection:
        try:
            return await psycopg.AsyncConnection.connect(
                host=params.get("host", "localhost"),
                port=params.get("port", 5432),
                dbname=params.get("database", ""),
                user=params.get("user", ""),
                password=params.get("password", ""),
                autocommit=True,
            )
        except psycopg.Error as exc:
            raise DriverError(str(exc).strip()) from exc

    async def reconnect(self) -> None:
        try:
            await self._conn.close()
        except psycopg.Error:
            pass
        self._conn = await self._open(self.params)

    async def disconnect(self) -> None:
        await self._conn.close()

    async def execute(
        self, query: str, binds: list[Any] | None = None
    ) -> ReadResult | WriteResult:
        """Run a SQL statement. Positional bind values map to ``%s`` placeholders in the query.

        Args:
            query: SQL statement to execute.
            binds: Optional positional bind parameters (referenced as ``%s`` in the query).

        Returns:
            ReadResult for queries that return rows, WriteResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        copy_cmd = parse_copy_to(query)
        if copy_cmd is not None:
            return await self._execute_copy_to(copy_cmd)

        # psycopg only applies %-style substitution when params is not None, so
        # an empty/absent bind list must be sent as None — otherwise a literal
        # "%" in the query (e.g. a LIKE pattern) is misparsed as a placeholder.
        params = binds or None

        try:
            cur = self._conn.cursor()
            await cur.execute(sql.SQL(query), params)  # ty: ignore[invalid-argument-type]
            if cur.description is not None:
                columns = [d.name for d in cur.description]
                rows = [[render_lob(v) for v in r] for r in await cur.fetchall()]
                return ReadResult(columns=columns, rows=rows, rows_total=len(rows))
            invalidate_cache(self._conn)
            return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            if isinstance(exc, psycopg.Error):
                raise DriverError(str(exc).strip()) from exc
            raise

    async def _execute_copy_to(self, cmd: CopyToCommand) -> WriteResult:
        """Stream a ``COPY ... TO STDOUT`` result straight to a local file.

        Mirrors what psql does for ``\\copy``: Postgres has no such command
        itself, so it's rewritten to the real ``COPY TO STDOUT`` and the
        client (grannos-py, here) writes the streamed bytes to disk.
        """
        stmt = build_copy_to_statement(cmd)
        try:
            cur = self._conn.cursor()
            async with cur.copy(sql.SQL(stmt)) as copy:  # ty: ignore[invalid-argument-type]
                with open(cmd.path, "wb") as f:
                    async for chunk in copy:
                        f.write(bytes(chunk))
            return WriteResult(rows_affected=cur.rowcount if cur.rowcount >= 0 else 0)
        except OSError as exc:
            raise DriverError(f"could not write to {cmd.path!r}: {exc}") from exc
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            if isinstance(exc, psycopg.Error):
                raise DriverError(str(exc).strip()) from exc
            raise

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        try:
            return await self._explore_list(path)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    async def _explore_list(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                schemas = await fetch_schemas(self._conn)
                return [
                    ExploreItem(name=s, type="schema", expandable=True) for s in schemas
                ]

            case [schema]:
                pairs = await fetch_tables_and_views(self._conn, schema)
                return [
                    ExploreItem(name=name, type=kind, expandable=True)
                    for name, kind in pairs
                ]

            case [_schema, _table]:
                return [
                    ExploreItem(name="columns", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                    ExploreItem(name="constraints", type="group", expandable=True),
                ]

            case [schema, table, "columns"]:
                pairs = await fetch_column_names_and_types(self._conn, schema, table)
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
                ]

            case [schema, table, "indexes"]:
                pairs = await fetch_index_names_and_types(self._conn, schema, table)
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
                ]

            case [schema, table, "constraints"]:
                pairs = await fetch_constraint_names_and_types(
                    self._conn, schema, table
                )
                return [
                    ExploreItem(name=name, type=kind, expandable=False)
                    for name, kind in pairs
                ]

            case _:
                return []

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        match path:
            case [schema, table]:
                result = await self.execute(build_preview_query(schema, table))
                return result if isinstance(result, ReadResult) else None
            case _:
                return None

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        try:
            return await self._explore_describe(path)
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    async def _explore_describe(self, path: list[str]) -> DescribeResult:
        match path:
            case [schema, table]:
                return await self._describe_table(schema, table)

            case [schema, table, "indexes"]:
                return await self._describe_indices(schema, table)

            case [schema, table, "indexes", index_name]:
                return await self._describe_index(schema, table, index_name)

            case [schema, table, "columns"]:
                return await self._describe_columns(schema, table)

            case [schema, table, "columns", col_name]:
                return await self._describe_column(schema, table, col_name)

            case [schema, table, "relationships", column]:
                desc = await self._describe_table(schema, table)
                return build_relationship_description(desc, table, schema, column)

            case _:
                return None

    async def _describe_table(self, schema: str, table: str) -> TableDescription:
        col_details = await fetch_column_details(self._conn, schema, table)
        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        col_index_map = await fetch_column_index_mapping(self._conn, schema, table)
        comment = await fetch_table_comment(self._conn, schema, table)

        index_cols: dict[str, set[str]] = {}
        for col_name, idx_names in col_index_map.items():
            for idx_name in idx_names:
                index_cols.setdefault(idx_name, set()).add(col_name)
        index_col_count = {k: len(v) for k, v in index_cols.items()}

        outgoing_references = await fetch_outgoing_references(self._conn, schema, table)
        incoming_references = await fetch_incoming_references(self._conn, schema, table)

        return TableDescription(
            table=table,
            schema=schema,
            comment=comment,
            columns=[
                ColumnInfo(
                    name=col.name,
                    type=col.type,
                    nullable=col.nullable,
                    pk=col.name in pk_cols,
                    default=col.default,
                    exclusive_index=any(
                        index_col_count[i] == 1 for i in col_index_map.get(col.name, [])
                    ),
                    composite_index=any(
                        index_col_count[i] > 1 for i in col_index_map.get(col.name, [])
                    ),
                )
                for col in col_details
            ],
            outgoing_references=outgoing_references,
            incoming_references=incoming_references,
        )

    async def _describe_indices(self, schema: str, table: str) -> IndicesDescription:
        metas = await fetch_index_metas_for_table(self._conn, schema, table)
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        included_by_index = await fetch_index_included_for_table(
            self._conn, schema, table
        )

        return IndicesDescription(
            indices=[
                IndexDescription(
                    index=meta.name,
                    fields=fields_by_index.get(meta.name, []),
                    unique=meta.unique,
                    tables=[table],
                    index_type=meta.index_type,
                    visible=meta.visible,
                    included_columns=included_by_index.get(meta.name, []),
                    ddl=meta.ddl,
                )
                for meta in metas
            ]
        )

    async def _describe_index(
        self, schema: str, table: str, index_name: str
    ) -> IndexDescription | None:
        meta = await fetch_index_meta(self._conn, schema, index_name)
        if meta is None:
            return None

        fields = await fetch_index_fields_for_index(self._conn, schema, index_name)
        included = await fetch_index_included_for_index(self._conn, schema, index_name)

        return IndexDescription(
            index=index_name,
            fields=fields,
            unique=meta.unique,
            tables=[table],
            index_type=meta.index_type,
            visible=meta.visible,
            included_columns=included,
            ddl=meta.ddl,
        )

    async def _describe_columns(self, schema: str, table: str) -> ColumnsDescription:
        col_details = await fetch_column_details(self._conn, schema, table)
        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        all_indices = (await self._describe_indices(schema, table)).indices
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        excl, comp = build_column_index_lists(fields_by_index, all_indices)
        comments = await fetch_all_column_comments(self._conn, schema, table)
        samples = await self._fetch_samples(schema, table)
        refs_by_col = group_references_by_column(
            await fetch_outgoing_references(self._conn, schema, table)
        )

        return ColumnsDescription(
            columns=[
                ColumnDescription(
                    name=col.name,
                    data_type=col.type,
                    nullable=col.nullable,
                    pk=col.name in pk_cols,
                    default=col.default,
                    exclusive_indices=excl.get(col.name, []),
                    composite_indices=comp.get(col.name, []),
                    comment=comments.get(col.name),
                    sample=samples.get(col.name, []),
                    outgoing_references=refs_by_col.get(col.name, []),
                )
                for col in col_details
            ]
        )

    async def _describe_column(
        self, schema: str, table: str, col_name: str
    ) -> ColumnDescription | None:
        col_details = await fetch_column_details(self._conn, schema, table)
        col = next((c for c in col_details if c.name == col_name), None)
        if col is None:
            return None

        pk_cols = await fetch_pk_columns(self._conn, schema, table)
        all_indices = (await self._describe_indices(schema, table)).indices
        fields_by_index = await fetch_index_fields_for_table(self._conn, schema, table)
        excl, comp = build_column_index_lists(fields_by_index, all_indices)
        comments = await fetch_all_column_comments(self._conn, schema, table)
        sample = await self._fetch_sample(schema, table, col_name)
        refs_by_col = group_references_by_column(
            await fetch_outgoing_references(self._conn, schema, table)
        )

        return ColumnDescription(
            name=col.name,
            data_type=col.type,
            nullable=col.nullable,
            pk=col.name in pk_cols,
            default=col.default,
            exclusive_indices=excl.get(col_name, []),
            composite_indices=comp.get(col_name, []),
            comment=comments.get(col_name),
            sample=sample,
            outgoing_references=refs_by_col.get(col_name, []),
        )

    async def _fetch_sample(self, schema: str, table: str, col_name: str) -> list[Any]:
        try:
            return await asyncio.wait_for(
                fetch_column_sample(
                    self._conn,
                    schema,
                    table,
                    col_name,
                    self._settings.column_sample_size,
                ),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return []

    async def _fetch_samples(self, schema: str, table: str) -> dict[str, list[Any]]:
        try:
            columns, rows = await asyncio.wait_for(
                fetch_table_sample_rows(self._conn, schema, table),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return {}
        return build_column_samples(columns, rows, self._settings.column_sample_size)


def _maybe_raise_connection_lost(exc: Exception) -> None:
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        raise ConnectionLostError(str(exc).strip()) from exc
