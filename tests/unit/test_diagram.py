from collections.abc import Awaitable, Callable

import pytest

from belvedere.diagram import DiagramError, build_diagram
from belvedere.protocol import (
    ColumnInfo,
    DescribeResult,
    IndexDescription,
    TableDescription,
    TableReference,
)

Describe = Callable[[list[str]], Awaitable[DescribeResult]]


def _describe_from(table_by_path: dict[tuple[str, ...], DescribeResult]) -> Describe:
    async def describe(path: list[str]) -> DescribeResult:
        return table_by_path.get(tuple(path))

    return describe


class TestBuildDiagram:
    async def test_raises_when_path_does_not_resolve_to_a_table(self) -> None:
        describe = _describe_from({})
        with pytest.raises(DiagramError):
            await build_diagram(["users"], describe)

    async def test_single_table_renders_a_box_with_its_name(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "users" in result
        assert "id" in result

    async def test_schema_qualified_table_shows_schema_dot_table(self) -> None:
        desc = TableDescription(schema="dbo", table="users", columns=[])
        describe = _describe_from({("dbo", "users"): desc})
        result = await build_diagram(["dbo", "users"], describe)
        assert "dbo.users" in result

    async def test_pk_column_is_marked(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "PK" in result

    async def test_fk_column_is_marked(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): desc, ("users",): users})
        result = await build_diagram(["orders"], describe)
        assert "FK" in result

    async def test_outgoing_reference_renders_connected_table(self) -> None:
        orders = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): orders, ("users",): users})
        result = await build_diagram(["orders"], describe)
        assert "users" in result
        assert "user_id → users.id" in result

    async def test_incoming_reference_renders_connected_table(self) -> None:
        users = TableDescription(
            table="users",
            columns=[ColumnInfo(name="id", type="INTEGER", pk=True)],
            incoming_references=[
                TableReference(column="id", table="orders", ref_column="user_id")
            ],
        )
        orders = TableDescription(
            table="orders", columns=[ColumnInfo(name="user_id", type="INTEGER")]
        )
        describe = _describe_from({("users",): users, ("orders",): orders})
        result = await build_diagram(["users"], describe)
        assert "orders" in result
        assert "orders.user_id → id" in result

    async def test_self_reference_is_not_duplicated(self) -> None:
        desc = TableDescription(
            table="employees",
            columns=[ColumnInfo(name="manager_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="manager_id", table="employees", ref_column="id")
            ],
        )
        describe = _describe_from({("employees",): desc})
        result = await build_diagram(["employees"], describe)
        assert result.count("┌─ employees") == 1

    async def test_cycle_does_not_recurse_infinitely(self) -> None:
        a = TableDescription(
            table="a",
            columns=[ColumnInfo(name="b_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="b_id", table="b", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[ColumnInfo(name="a_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id")
            ],
        )
        describe = _describe_from({("a",): a, ("b",): b})
        result = await build_diagram(["a"], describe)
        assert result.count("┌─ a") == 1
        assert result.count("┌─ b") == 1

    async def test_unresolvable_reference_is_shown_without_a_box(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "users" in result
        assert "┌─ users" not in result

    async def test_non_table_describe_result_raises(self) -> None:
        describe = _describe_from({("i",): IndexDescription(index="i", fields=[])})
        with pytest.raises(DiagramError):
            await build_diagram(["i"], describe)
