"""
Integration tests for the PostgreSQL driver.

Requires a running PostgreSQL instance. Configure via environment variables:
  POSTGRES_HOST      (default: localhost)
  POSTGRES_PORT      (default: 5432)
  POSTGRES_USER       (default: testuser)
  POSTGRES_PASSWORD  (required — no default)
  POSTGRES_DATABASE  (default: testdb)

Tests are skipped automatically when psycopg is not installed or the
server is unreachable.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.base import DriverSettings
from belvedere.drivers.postgres import PostgresDriver
from belvedere.protocol import (
    ColumnDescription,
    ColumnsDescription,
    IndexDescription,
    ReadResult,
    TableDescription,
    TableReference,
    WriteResult,
)

pytestmark = pytest.mark.external


def _params() -> dict:
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "user": os.environ.get("POSTGRES_USER", "testuser"),
        "password": os.environ.get("POSTGRES_PASSWORD", ""),
        "database": os.environ.get("POSTGRES_DATABASE", "testdb"),
    }


@pytest.fixture
async def driver() -> AsyncGenerator[PostgresDriver, None]:
    pytest.importorskip("psycopg")
    try:
        d = await PostgresDriver.create(_params(), DriverSettings())
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture
async def schema(driver: PostgresDriver) -> AsyncGenerator[str, None]:
    name = "s_" + uuid.uuid4().hex[:12]
    await driver.execute(f'CREATE SCHEMA "{name}"', [])
    yield name
    await driver.execute(f'DROP SCHEMA "{name}" CASCADE', [])


@pytest.fixture
async def table(driver: PostgresDriver, schema: str) -> AsyncGenerator[str, None]:
    name = "t_" + uuid.uuid4().hex[:12]
    yield name
    try:
        await driver.execute(f'DROP TABLE "{schema}"."{name}" CASCADE', [])
    except Exception:
        pass


@pytest.fixture
async def tables(
    driver: PostgresDriver, schema: str
) -> AsyncGenerator[tuple[str, str], None]:
    parent = "t_" + uuid.uuid4().hex[:12]
    child = "t_" + uuid.uuid4().hex[:12]
    yield parent, child
    for name in (child, parent):
        try:
            await driver.execute(f'DROP TABLE "{schema}"."{name}" CASCADE', [])
        except Exception:
            pass


class TestReconnect:
    async def test_reconnect_succeeds_when_connection_is_dead(
        self, driver: PostgresDriver
    ) -> None:
        await driver._conn.close()
        await driver.reconnect()
        result = await driver.execute("SELECT 1", [])
        assert isinstance(result, ReadResult)


class TestExecute:
    async def test_should_return_columns_and_rows(self, driver: PostgresDriver) -> None:
        result = await driver.execute("SELECT 1 AS n, 'hello' AS s", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["n", "s"]
        assert result.rows == [[1, "hello"]]

    async def test_should_support_positional_params(
        self, driver: PostgresDriver
    ) -> None:
        result = await driver.execute("SELECT %s AS val", [42])
        assert isinstance(result, ReadResult)
        assert result.rows == [[42]]

    async def test_should_not_choke_on_literal_percent_without_binds(
        self, driver: PostgresDriver
    ) -> None:
        result = await driver.execute("SELECT 'a%b' AS val", [])
        assert isinstance(result, ReadResult)
        assert result.rows == [["a%b"]]

    async def test_should_return_dml_result_for_insert(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        result = await driver.execute(
            f'INSERT INTO "{schema}"."{table}" VALUES (%s, %s)', [1, "hello"]
        )
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_should_return_dml_result_for_delete(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        await driver.execute(f'INSERT INTO "{schema}"."{table}" VALUES (%s)', [1])
        await driver.execute(f'INSERT INTO "{schema}"."{table}" VALUES (%s)', [2])
        result = await driver.execute(f'DELETE FROM "{schema}"."{table}"', [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 2

    async def test_should_persist_inserts_within_connection(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        await driver.execute(
            f'INSERT INTO "{schema}"."{table}" VALUES (%s, %s)', [1, "hello"]
        )
        result = await driver.execute(f'SELECT id, val FROM "{schema}"."{table}"', [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["id", "val"]
        assert result.rows == [[1, "hello"]]


class TestExploreList:
    async def test_root_lists_non_system_schemas(
        self, driver: PostgresDriver, schema: str
    ) -> None:
        items = await driver.explore_list([])
        names = {i.name for i in items}
        assert schema in names
        assert "pg_catalog" not in names
        assert "information_schema" not in names
        assert all(i.type == "schema" for i in items)
        assert all(i.expandable for i in items)

    async def test_schema_lists_created_table(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        items = await driver.explore_list([schema])
        assert any(i.name == table and i.type == "table" for i in items)

    async def test_schema_lists_view(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        view = table + "_v"
        await driver.execute(
            f'CREATE VIEW "{schema}"."{view}" AS SELECT * FROM "{schema}"."{table}"', []
        )
        items = await driver.explore_list([schema])
        assert any(i.name == view and i.type == "view" for i in items)

    async def test_table_returns_groups(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        items = await driver.explore_list([schema, table])
        assert {i.name for i in items} == {"columns", "indexes", "constraints"}
        assert all(i.type == "group" and i.expandable for i in items)

    async def test_columns_lists_in_ordinal_order(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text, active boolean)',
            [],
        )
        items = await driver.explore_list([schema, table, "columns"])
        assert [i.name for i in items] == ["id", "val", "active"]
        assert all(not i.expandable for i in items)

    async def test_columns_reflect_data_type(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, name varchar(100))', []
        )
        items = await driver.explore_list([schema, table, "columns"])
        by_name = {i.name: i for i in items}
        assert by_name["id"].type == "integer"
        assert by_name["name"].type == "character varying"

    async def test_indexes_lists_created_index(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        idx = "idx_" + table
        await driver.execute(f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val)', [])
        items = await driver.explore_list([schema, table, "indexes"])
        assert any(i.name == idx and i.type == "btree" for i in items)
        assert all(not i.expandable for i in items)

    async def test_indexes_empty_when_none_exist(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        items = await driver.explore_list([schema, table, "indexes"])
        assert items == []

    async def test_constraints_lists_primary_key(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        pk = "pk_" + table
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}"'
            f' (id integer, CONSTRAINT "{pk}" PRIMARY KEY (id))',
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert pk in by_name
        assert by_name[pk].type == "primary_key"
        assert not by_name[pk].expandable

    async def test_constraints_lists_unique(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        uq = "uq_" + table
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}"'
            f' (id integer, val text, CONSTRAINT "{uq}" UNIQUE (val))',
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert uq in by_name
        assert by_name[uq].type == "unique"

    async def test_constraints_lists_check(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        ck = "ck_" + table
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}"'
            f' (id integer, CONSTRAINT "{ck}" CHECK (id > 0))',
            [],
        )
        items = await driver.explore_list([schema, table, "constraints"])
        by_name = {i.name: i for i in items}
        assert ck in by_name
        assert by_name[ck].type == "check"

    async def test_constraints_maps_foreign_key_type(
        self, driver: PostgresDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        pk = "pk_" + parent
        fk = "fk_" + child
        await driver.execute(
            f'CREATE TABLE "{schema}"."{parent}"'
            f' (id integer, CONSTRAINT "{pk}" PRIMARY KEY (id))',
            [],
        )
        await driver.execute(
            f'CREATE TABLE "{schema}"."{child}" ('
            f"  id integer, parent_id integer,"
            f'  CONSTRAINT "{fk}" FOREIGN KEY (parent_id) REFERENCES "{schema}"."{parent}"(id)'
            ")",
            [],
        )
        items = await driver.explore_list([schema, child, "constraints"])
        by_name = {i.name: i for i in items}
        assert fk in by_name
        assert by_name[fk].type == "foreign_key"

    async def test_unknown_path_returns_empty(self, driver: PostgresDriver) -> None:
        assert (
            await driver.explore_list(["no_such_schema", "no_such_table", "extra"])
            == []
        )


class TestExploreDescribe:
    async def test_returns_column_metadata(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer NOT NULL, val text)', []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        assert desc.schema == schema
        assert desc.table == table
        by_name = {c.name: c for c in desc.columns}
        assert list(by_name) == ["id", "val"]
        assert by_name["id"].type == "integer"
        assert by_name["id"].nullable is False
        assert by_name["val"].type == "text"
        assert by_name["val"].nullable is True

    async def test_returns_pk_flag(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer PRIMARY KEY, val text)', []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].pk is True
        assert by_name["val"].pk is False

    async def test_returns_default_value(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer DEFAULT 0, val text)', []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].default == "0"
        assert by_name["val"].default is None

    async def test_returns_none_for_unknown_path(self, driver: PostgresDriver) -> None:
        assert await driver.explore_describe([]) is None

    async def test_comment(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        await driver.execute(
            f'COMMENT ON TABLE "{schema}"."{table}" IS \'A test table comment\'', []
        )
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        assert desc.comment == "A test table comment"

    async def test_comment_defaults_to_none(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        assert desc.comment is None

    async def test_returns_outgoing_references(
        self, driver: PostgresDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f'CREATE TABLE "{schema}"."{parent}" (id integer PRIMARY KEY)', []
        )
        await driver.execute(
            f'CREATE TABLE "{schema}"."{child}" ('
            f"  id integer, parent_id integer,"
            f'  FOREIGN KEY (parent_id) REFERENCES "{schema}"."{parent}"(id)'
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, child])
        assert isinstance(desc, TableDescription)
        assert desc.outgoing_references == [
            TableReference(
                column="parent_id", table=parent, ref_column="id", schema=schema
            )
        ]
        assert desc.incoming_references == []

    async def test_returns_incoming_references(
        self, driver: PostgresDriver, schema: str, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f'CREATE TABLE "{schema}"."{parent}" (id integer PRIMARY KEY)', []
        )
        await driver.execute(
            f'CREATE TABLE "{schema}"."{child}" ('
            f"  id integer, parent_id integer,"
            f'  FOREIGN KEY (parent_id) REFERENCES "{schema}"."{parent}"(id)'
            ")",
            [],
        )
        desc = await driver.explore_describe([schema, parent])
        assert isinstance(desc, TableDescription)
        assert desc.incoming_references == [
            TableReference(
                column="id", table=child, ref_column="parent_id", schema=schema
            )
        ]
        assert desc.outgoing_references == []

    async def test_references_default_to_empty(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        desc = await driver.explore_describe([schema, table])
        assert isinstance(desc, TableDescription)
        assert desc.outgoing_references == []
        assert desc.incoming_references == []


class TestExploreDescribeIndex:
    async def test_basic_fields_and_type(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        idx = "idx_" + table
        await driver.execute(f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val)', [])
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.index == idx
        assert len(desc.fields) == 1
        assert desc.fields[0].name == "val"
        assert desc.fields[0].direction == "asc"
        assert desc.index_type == "btree"

    async def test_descending_direction(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val integer)', []
        )
        idx = "idx_" + table
        await driver.execute(
            f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val DESC)', []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.fields[0].direction == "desc"

    async def test_unique_index(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, email text)', []
        )
        idx = "idx_" + table
        await driver.execute(
            f'CREATE UNIQUE INDEX "{idx}" ON "{schema}"."{table}"(email)', []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.unique is True

    async def test_multi_column_index_field_order(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" ("last" text, "first" text)',
            [],
        )
        idx = "idx_" + table
        await driver.execute(
            f'CREATE INDEX "{idx}" ON "{schema}"."{table}"("last", "first")', []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert [f.name for f in desc.fields] == ["last", "first"]

    async def test_include_columns(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text, extra text)', []
        )
        idx = "idx_" + table
        await driver.execute(
            f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val) INCLUDE (extra)', []
        )
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert [f.name for f in desc.fields] == ["val"]
        assert desc.included_columns == ["extra"]

    async def test_ddl_populated(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        idx = "idx_" + table
        await driver.execute(f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val)', [])
        desc = await driver.explore_describe([schema, table, "indexes", idx])
        assert isinstance(desc, IndexDescription)
        assert desc.ddl is not None
        assert idx in desc.ddl

    async def test_unknown_index_returns_none(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        assert (
            await driver.explore_describe([schema, table, "indexes", "no_such_idx"])
            is None
        )


class TestExploreDescribeColumns:
    async def test_returns_all_columns(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        assert [c.name for c in desc.columns] == ["id", "val"]

    async def test_data_type(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].data_type == "integer"
        assert by_name["val"].data_type == "text"

    async def test_pk_and_nullable(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}"'
            f" (id integer PRIMARY KEY, val text NOT NULL)",
            [],
        )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].pk is True
        assert by_name["val"].pk is False
        assert by_name["id"].nullable is False
        assert by_name["val"].nullable is False

    async def test_exclusive_index(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        idx = "idx_" + table
        await driver.execute(f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val)', [])
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert len(by_name["val"].exclusive_indices) == 1
        assert by_name["val"].exclusive_indices[0].index == idx
        assert by_name["id"].exclusive_indices == []
        assert by_name["val"].composite_indices == []

    async def test_composite_index(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text, other text)',
            [],
        )
        idx = "idx_" + table
        await driver.execute(
            f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val, other)', []
        )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert len(by_name["val"].composite_indices) == 1
        assert by_name["val"].exclusive_indices == []

    async def test_comment(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        await driver.execute(
            f'COMMENT ON COLUMN "{schema}"."{table}".val IS \'A test comment\'', []
        )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["val"].comment == "A test comment"
        assert by_name["id"].comment is None

    async def test_sample_values(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        for i, v in enumerate(["x", "y", "z", "x"]):
            await driver.execute(
                f'INSERT INTO "{schema}"."{table}" VALUES (%s, %s)', [i, v]
            )
        desc = await driver.explore_describe([schema, table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        sample = by_name["val"].sample
        assert len(sample) <= 3
        assert set(sample).issubset({"x", "y", "z"})


class TestExploreDescribeColumn:
    async def test_basic_fields(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text NOT NULL)', []
        )
        desc = await driver.explore_describe([schema, table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert desc.name == "val"
        assert desc.data_type == "text"
        assert desc.nullable is False
        assert desc.pk is False

    async def test_pk_column(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer PRIMARY KEY, val text)', []
        )
        desc = await driver.explore_describe([schema, table, "columns", "id"])
        assert isinstance(desc, ColumnDescription)
        assert desc.pk is True

    async def test_exclusive_index(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        idx = "idx_" + table
        await driver.execute(f'CREATE INDEX "{idx}" ON "{schema}"."{table}"(val)', [])
        desc = await driver.explore_describe([schema, table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert len(desc.exclusive_indices) == 1
        assert desc.exclusive_indices[0].index == idx
        assert desc.composite_indices == []

    async def test_comment(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        await driver.execute(
            f'COMMENT ON COLUMN "{schema}"."{table}".val IS \'Column comment\'', []
        )
        desc = await driver.explore_describe([schema, table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert desc.comment == "Column comment"

    async def test_sample_values(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(
            f'CREATE TABLE "{schema}"."{table}" (id integer, val text)', []
        )
        for i, v in enumerate(["a", "b", "c", "a"]):
            await driver.execute(
                f'INSERT INTO "{schema}"."{table}" VALUES (%s, %s)', [i, v]
            )
        desc = await driver.explore_describe([schema, table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert len(desc.sample) <= 3
        assert set(desc.sample).issubset({"a", "b", "c"})

    async def test_unknown_column_returns_none(
        self, driver: PostgresDriver, schema: str, table: str
    ) -> None:
        await driver.execute(f'CREATE TABLE "{schema}"."{table}" (id integer)', [])
        assert (
            await driver.explore_describe([schema, table, "columns", "no_such_col"])
            is None
        )
