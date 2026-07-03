"""
Integration tests for the SQL Server driver.

Requires a running SQL Server instance. Configure via environment variables:
  MSSQL_HOST      (default: localhost)
  MSSQL_PORT      (default: 1433)
  MSSQL_USER      (default: sa)
  MSSQL_PASSWORD  (required — no default)
  MSSQL_DATABASE  (default: tempdb)

Tests are skipped automatically when mssql_python is not installed or the
server is unreachable.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

from belvedere.drivers.base import DriverSettings
from belvedere.drivers.sqlserver import SQLServerDriver
from belvedere.protocol import (
    ColumnDescription,
    ColumnsDescription,
    IndexDescription,
    ReadResult,
    TableDescription,
    WriteResult,
)

pytestmark = pytest.mark.external


def _params() -> dict:
    return {
        "host": os.environ.get("MSSQL_HOST", "localhost"),
        "port": int(os.environ.get("MSSQL_PORT", "1433")),
        "user": os.environ.get("MSSQL_USER", "sa"),
        "password": os.environ.get("MSSQL_PASSWORD", ""),
        "database": os.environ.get("MSSQL_DATABASE", "tempdb"),
    }


@pytest.fixture
async def driver() -> AsyncGenerator[SQLServerDriver, None]:
    pytest.importorskip("mssql_python")
    try:
        d = await SQLServerDriver.create(_params(), DriverSettings())
    except Exception as exc:
        pytest.skip(f"SQL Server not available: {exc}")
    yield d
    await d.disconnect()


@pytest.fixture
async def table(driver: SQLServerDriver) -> AsyncGenerator[str, None]:
    """Yields a unique table name and drops it on teardown."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    yield name
    await driver.execute(
        f"IF OBJECT_ID(N'dbo.{name}', N'U') IS NOT NULL DROP TABLE dbo.{name}", []
    )


@pytest.fixture
async def tables(driver: SQLServerDriver) -> AsyncGenerator[tuple[str, str], None]:
    """Yields two unique table names and drops both on teardown."""
    parent = f"t_{uuid.uuid4().hex[:12]}"
    child = f"t_{uuid.uuid4().hex[:12]}"
    yield parent, child
    for name in (child, parent):  # child first to satisfy FK
        await driver.execute(
            f"IF OBJECT_ID(N'dbo.{name}', N'U') IS NOT NULL DROP TABLE dbo.{name}", []
        )


class TestExecute:
    async def test_should_return_columns_and_rows(
        self, driver: SQLServerDriver
    ) -> None:
        result = await driver.execute("SELECT 1 AS n, 'hello' AS s", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["n", "s"]
        assert result.rows == [[1, "hello"]]

    async def test_should_return_rows_affected_for_insert(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        result = await driver.execute(
            f"INSERT INTO dbo.{table} VALUES (?, ?)", [1, "hello"]
        )
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_should_return_rows_affected_for_delete(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        await driver.execute(f"INSERT INTO dbo.{table} VALUES (?)", [1])
        await driver.execute(f"INSERT INTO dbo.{table} VALUES (?)", [2])
        result = await driver.execute(f"DELETE FROM dbo.{table}", [])
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 2

    async def test_should_persist_inserts_within_connection(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"INSERT INTO dbo.{table} VALUES (?, ?)", [1, "hello"])
        result = await driver.execute(f"SELECT * FROM dbo.{table}", [])
        assert isinstance(result, ReadResult)
        assert result.columns == ["id", "val"]
        assert result.rows == [[1, "hello"]]


class TestExploreList:
    async def test_should_list_dbo_schema(self, driver: SQLServerDriver) -> None:
        items = await driver.explore_list([])
        assert any(i.name == "dbo" and i.type == "schema" for i in items)

    async def test_should_not_list_system_schemas(
        self, driver: SQLServerDriver
    ) -> None:
        items = await driver.explore_list([])
        names = {i.name for i in items}
        assert "sys" not in names
        assert "INFORMATION_SCHEMA" not in names

    async def test_should_list_created_table(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        items = await driver.explore_list(["dbo"])
        assert any(i.name == table for i in items)

    async def test_should_return_groups_for_table(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        items = await driver.explore_list(["dbo", table])
        assert {i.name for i in items} == {"columns", "indices", "constraints"}
        assert all(i.type == "group" and i.expandable for i in items)

    async def test_should_list_columns_in_ordinal_order(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50), active BIT)", []
        )
        items = await driver.explore_list(["dbo", table, "columns"])
        assert [i.name for i in items] == ["id", "val", "active"]
        assert all(not i.expandable for i in items)

    async def test_should_list_explicit_index(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val)", [])
        items = await driver.explore_list(["dbo", table, "indices"])
        assert any(i.name == "ix_val" for i in items)
        assert all(not i.expandable for i in items)

    async def test_should_list_unique_index(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE UNIQUE INDEX uix_val ON dbo.{table}(val)", [])
        items = await driver.explore_list(["dbo", table, "indices"])
        assert any(i.name == "uix_val" for i in items)

    async def test_should_list_primary_key_constraint(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT CONSTRAINT pk_{table} PRIMARY KEY)", []
        )
        items = await driver.explore_list(["dbo", table, "constraints"])
        names = {i.name for i in items}
        assert f"pk_{table}" in names
        by_name = {i.name: i for i in items}
        assert by_name[f"pk_{table}"].type == "primary_key"

    async def test_should_list_unique_constraint(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} ("
            f"  id INT, val VARCHAR(50) CONSTRAINT uq_{table} UNIQUE"
            ")",
            [],
        )
        items = await driver.explore_list(["dbo", table, "constraints"])
        by_name = {i.name: i for i in items}
        assert f"uq_{table}" in by_name
        assert by_name[f"uq_{table}"].type == "unique"

    async def test_should_list_check_constraint(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} ("
            f"  id INT CONSTRAINT chk_{table} CHECK (id > 0)"
            ")",
            [],
        )
        items = await driver.explore_list(["dbo", table, "constraints"])
        by_name = {i.name: i for i in items}
        assert f"chk_{table}" in by_name
        assert by_name[f"chk_{table}"].type == "check"

    async def test_should_list_foreign_key_constraint(
        self, driver: SQLServerDriver, tables: tuple[str, str]
    ) -> None:
        parent, child = tables
        await driver.execute(
            f"CREATE TABLE dbo.{parent} (id INT CONSTRAINT pk_{parent} PRIMARY KEY)", []
        )
        await driver.execute(
            f"CREATE TABLE dbo.{child} ("
            f"  id INT,"
            f"  parent_id INT CONSTRAINT fk_{child} REFERENCES dbo.{parent}(id)"
            ")",
            [],
        )
        items = await driver.explore_list(["dbo", child, "constraints"])
        by_name = {i.name: i for i in items}
        assert f"fk_{child}" in by_name
        assert by_name[f"fk_{child}"].type == "foreign_key"

    async def test_should_return_empty_for_unknown_path(
        self, driver: SQLServerDriver
    ) -> None:
        assert (
            await driver.explore_list(["dbo", "no_such_table", "columns", "extra"])
            == []
        )


class TestExploreDescribe:
    async def test_should_return_column_metadata(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT NOT NULL, val VARCHAR(50) NULL)", []
        )
        desc = await driver.explore_describe(["dbo", table])
        assert isinstance(desc, TableDescription)
        assert desc.schema == "dbo"
        assert desc.table == table
        cols = {c.name: c for c in desc.columns}
        assert list(cols) == ["id", "val"]
        assert cols["id"].type == "int"
        assert cols["id"].nullable is False
        assert cols["val"].type == "varchar"
        assert cols["val"].nullable is True

    async def test_should_return_none_for_unknown_path(
        self, driver: SQLServerDriver
    ) -> None:
        assert await driver.explore_describe([]) is None

    async def test_comment(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        await driver.execute(
            "EXEC sp_addextendedproperty"
            " @name = N'MS_Description', @value = N'A test table comment',"
            " @level0type = N'Schema', @level0name = N'dbo',"
            f" @level1type = N'Table', @level1name = N'{table}'",
            [],
        )
        desc = await driver.explore_describe(["dbo", table])
        assert isinstance(desc, TableDescription)
        assert desc.comment == "A test table comment"

    async def test_comment_defaults_to_none(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        desc = await driver.explore_describe(["dbo", table])
        assert isinstance(desc, TableDescription)
        assert desc.comment is None


class TestExploreDescribeIndex:
    async def test_basic_fields_and_direction(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_val"])
        assert isinstance(desc, IndexDescription)
        assert desc.index == "ix_val"
        assert len(desc.fields) == 1
        assert desc.fields[0].name == "val"
        assert desc.fields[0].direction == "asc"

    async def test_descending_direction(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val INT)", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val DESC)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_val"])
        assert isinstance(desc, IndexDescription)
        assert desc.fields[0].direction == "desc"

    async def test_unique_index(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, email VARCHAR(100))", []
        )
        await driver.execute(f"CREATE UNIQUE INDEX uix_email ON dbo.{table}(email)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "uix_email"])
        assert isinstance(desc, IndexDescription)
        assert desc.unique is True

    async def test_clustered_index(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE CLUSTERED INDEX cix_id ON dbo.{table}(id)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "cix_id"])
        assert isinstance(desc, IndexDescription)
        assert desc.clustered is True
        assert desc.index_type == "clustered"

    async def test_included_columns(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50), extra VARCHAR(50))",
            [],
        )
        await driver.execute(
            f"CREATE INDEX ix_val ON dbo.{table}(val) INCLUDE (extra)", []
        )
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_val"])
        assert isinstance(desc, IndexDescription)
        assert desc.included_columns == ["extra"]

    async def test_filtered_index_ddl_contains_where(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50), active BIT)", []
        )
        await driver.execute(
            f"CREATE INDEX ix_active ON dbo.{table}(val) WHERE active = 1", []
        )
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_active"])
        assert isinstance(desc, IndexDescription)
        assert desc.ddl is not None
        assert "active" in desc.ddl

    async def test_multi_column_index_field_order(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, last VARCHAR(50), first VARCHAR(50))",
            [],
        )
        await driver.execute(f"CREATE INDEX ix_name ON dbo.{table}(last, first)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_name"])
        assert isinstance(desc, IndexDescription)
        assert [f.name for f in desc.fields] == ["last", "first"]

    async def test_ddl_populated(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val)", [])
        desc = await driver.explore_describe(["dbo", table, "indices", "ix_val"])
        assert isinstance(desc, IndexDescription)
        assert desc.ddl is not None
        assert "ix_val" in desc.ddl

    async def test_unknown_index_returns_none(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        assert (
            await driver.explore_describe(["dbo", table, "indices", "no_such_idx"])
            is None
        )


class TestExploreDescribeColumns:
    async def test_returns_all_columns(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        assert [c.name for c in desc.columns] == ["id", "val"]

    async def test_data_type(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].data_type == "int"
        assert by_name["val"].data_type == "varchar"

    async def test_pk_and_nullable(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT PRIMARY KEY, val VARCHAR(50) NOT NULL)",
            [],
        )
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["id"].pk is True
        assert by_name["val"].pk is False
        assert by_name["id"].nullable is False
        assert by_name["val"].nullable is False

    async def test_exclusive_index(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val)", [])
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert len(by_name["val"].exclusive_indices) == 1
        assert by_name["val"].exclusive_indices[0].index == "ix_val"
        assert by_name["id"].exclusive_indices == []
        assert by_name["val"].composite_indices == []

    async def test_composite_index(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50), other VARCHAR(50))", []
        )
        await driver.execute(f"CREATE INDEX ix_vc ON dbo.{table}(val, other)", [])
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert len(by_name["val"].composite_indices) == 1
        assert by_name["val"].exclusive_indices == []

    async def test_comment(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(
            "EXEC sp_addextendedproperty"
            " @name = N'MS_Description', @value = N'A test comment',"
            " @level0type = N'Schema', @level0name = N'dbo',"
            f" @level1type = N'Table', @level1name = N'{table}',"
            " @level2type = N'Column', @level2name = N'val'",
            [],
        )
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        assert by_name["val"].comment == "A test comment"
        assert by_name["id"].comment is None

    async def test_sample_values(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(10))", [])
        for i, v in enumerate(["x", "y", "z", "x"]):
            await driver.execute(f"INSERT INTO dbo.{table} VALUES (?, ?)", [i, v])
        desc = await driver.explore_describe(["dbo", table, "columns"])
        assert isinstance(desc, ColumnsDescription)
        by_name = {c.name: c for c in desc.columns}
        sample = by_name["val"].sample
        assert len(sample) <= 3
        assert set(sample).issubset({"x", "y", "z"})


class TestExploreDescribeColumn:
    async def test_basic_fields(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50) NOT NULL)", []
        )
        desc = await driver.explore_describe(["dbo", table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert desc.name == "val"
        assert desc.data_type == "varchar"
        assert desc.nullable is False
        assert desc.pk is False

    async def test_pk_column(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(
            f"CREATE TABLE dbo.{table} (id INT PRIMARY KEY, val VARCHAR(50))", []
        )
        desc = await driver.explore_describe(["dbo", table, "columns", "id"])
        assert isinstance(desc, ColumnDescription)
        assert desc.pk is True

    async def test_exclusive_index(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(f"CREATE INDEX ix_val ON dbo.{table}(val)", [])
        desc = await driver.explore_describe(["dbo", table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert len(desc.exclusive_indices) == 1
        assert desc.exclusive_indices[0].index == "ix_val"
        assert desc.composite_indices == []

    async def test_comment(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(50))", [])
        await driver.execute(
            "EXEC sp_addextendedproperty"
            " @name = N'MS_Description', @value = N'Column comment',"
            " @level0type = N'Schema', @level0name = N'dbo',"
            f" @level1type = N'Table', @level1name = N'{table}',"
            " @level2type = N'Column', @level2name = N'val'",
            [],
        )
        desc = await driver.explore_describe(["dbo", table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert desc.comment == "Column comment"

    async def test_sample_values(self, driver: SQLServerDriver, table: str) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT, val VARCHAR(10))", [])
        for i, v in enumerate(["a", "b", "c", "a"]):
            await driver.execute(f"INSERT INTO dbo.{table} VALUES (?, ?)", [i, v])
        desc = await driver.explore_describe(["dbo", table, "columns", "val"])
        assert isinstance(desc, ColumnDescription)
        assert len(desc.sample) <= 3
        assert set(desc.sample).issubset({"a", "b", "c"})

    async def test_unknown_column_returns_none(
        self, driver: SQLServerDriver, table: str
    ) -> None:
        await driver.execute(f"CREATE TABLE dbo.{table} (id INT)", [])
        assert (
            await driver.explore_describe(["dbo", table, "columns", "no_such_col"])
            is None
        )
