import re
from collections.abc import Awaitable, Callable, Mapping

import pytest

from grannos.diagram import DiagramError, build_diagram
from grannos.protocol import (
    ColumnInfo,
    DescribeResult,
    IndexDescription,
    TableDescription,
    TableReference,
)

Describe = Callable[[list[str]], Awaitable[DescribeResult]]


def _describe_from(table_by_path: Mapping[tuple[str, ...], DescribeResult]) -> Describe:
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
        assert "users" in result.diagram
        assert "id" in result.diagram

    async def test_schema_qualified_table_shows_schema_dot_table(self) -> None:
        desc = TableDescription(schema="dbo", table="users", columns=[])
        describe = _describe_from({("dbo", "users"): desc})
        result = await build_diagram(["dbo", "users"], describe)
        assert "dbo.users" in result.diagram

    async def test_pk_column_is_marked(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "PK" in result.diagram

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
        assert "FK" in result.diagram

    async def test_many_to_one_edge_shows_star_and_one_markers(self) -> None:
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
        # "*" (many) at the FK side, "1" at the referenced side.
        assert re.search(r"│\*─*1│", result.diagram)

    async def test_one_to_one_edge_shows_one_markers_on_both_ends(self) -> None:
        orders = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(
                    column="user_id", table="users", ref_column="id", unique=True
                )
            ],
        )
        users = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("orders",): orders, ("users",): users})
        result = await build_diagram(["orders"], describe)
        # FK column is itself unique, so both ends show "1" instead of "*".
        assert re.search(r"│1─*1│", result.diagram)

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
        assert "users" in result.diagram

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
        assert "orders" in result.diagram

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
        assert result.diagram.count("┌─ employees") == 1

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
        assert result.diagram.count("┌─ a") == 1
        assert result.diagram.count("┌─ b") == 1

    async def test_unresolvable_reference_gets_a_placeholder_box(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "┌─ users" in result.diagram
        assert "(unavailable)" in result.diagram

    async def test_non_table_describe_result_raises(self) -> None:
        describe = _describe_from({("i",): IndexDescription(index="i", fields=[])})
        with pytest.raises(DiagramError):
            await build_diagram(["i"], describe)

    async def test_non_key_columns_are_collapsed_to_an_ellipsis(self) -> None:
        desc = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="name", type="TEXT"),
                ColumnInfo(name="email", type="TEXT"),
            ],
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)
        assert "..." in result.diagram
        assert "name" not in result.diagram
        assert "email" not in result.diagram

    async def test_incoming_fk_column_is_kept(self) -> None:
        users = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="username", type="TEXT"),
                ColumnInfo(name="bio", type="TEXT"),
            ],
            incoming_references=[
                TableReference(column="username", table="posts", ref_column="author")
            ],
        )
        posts = TableDescription(
            table="posts", columns=[ColumnInfo(name="author", type="TEXT")]
        )
        describe = _describe_from({("users",): users, ("posts",): posts})
        result = await build_diagram(["users"], describe)
        assert "username" in result.diagram
        assert "bio" not in result.diagram
        assert "..." in result.diagram

    async def test_no_ellipsis_when_all_columns_are_keys(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[
                ColumnInfo(name="user_id", type="INTEGER"),
                ColumnInfo(name="product_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id"),
                TableReference(column="product_id", table="products", ref_column="id"),
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)
        assert "..." not in result.diagram

    async def test_diamond_reference_draws_exactly_one_box(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        shared = TableDescription(
            table="shared", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from(
            {("root",): root, ("a",): a, ("b",): b, ("shared",): shared}
        )
        result = await build_diagram(["root"], describe)
        assert result.diagram.count("┌─ shared") == 1

    async def test_edge_between_boxes_of_different_heights_is_straight(self) -> None:
        tall = TableDescription(
            table="tall",
            columns=[
                ColumnInfo(name=f"k{i}", type="INTEGER", pk=True) for i in range(4)
            ]
            + [ColumnInfo(name="small_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="small_id", table="small", ref_column="id")
            ],
        )
        small = TableDescription(
            table="small", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("tall",): tall, ("small",): small})
        result = await build_diagram(["tall"], describe)
        # small is nudged down so both anchors share a row — one straight
        # border-to-border line, no jog in the channel. Endpoints carry
        # cardinality markers: "*" (many) at the FK side, "1" at the
        # referenced side.
        assert re.search(r"│\*─*1│", result.diagram)

    async def test_skip_edge_routes_past_the_intermediate_box_via_a_bypass_lane(
        self,
    ) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="mid_id", type="INTEGER"),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="mid_id", table="mid", ref_column="id"),
                TableReference(column="leaf_id", table="leaf", ref_column="id"),
            ],
        )
        mid = TableDescription(
            table="mid",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="leaf_id", table="leaf", ref_column="id")
            ],
        )
        leaf = TableDescription(
            table="leaf", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("root",): root, ("mid",): mid, ("leaf",): leaf})
        result = await build_diagram(["root"], describe)
        # The root→leaf edge crosses mid's rank; it should bypass mid via a
        # side lane instead of forcing mid out of its natural row.
        lines = result.diagram.splitlines()
        row_of = {
            t: next(r for r, s in enumerate(lines) if f"┌─ {t} " in s)
            for t in ("root", "mid")
        }
        assert row_of["mid"] == row_of["root"]

    async def test_multiple_relationships_on_the_same_side_get_distinct_anchors(
        self,
    ) -> None:
        # a and c both land on the hub's same side (b takes the other side by
        # LPT balancing) — their edges must not both bunch at one anchor.
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
                ColumnInfo(name="c_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
                TableReference(column="c_id", table="c", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        b = TableDescription(
            table="b", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        c = TableDescription(
            table="c", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("root",): root, ("a",): a, ("b",): b, ("c",): c})
        result = await build_diagram(["root"], describe)

        rows_by_edge: dict[tuple[str, ...], set[int]] = {}
        for r in result.regions:
            if r.kind == "edge":
                rows_by_edge.setdefault(tuple(r.path), set()).add(r.row)
        a_rows = rows_by_edge[("root", "relationships", "a_id")]
        c_rows = rows_by_edge[("root", "relationships", "c_id")]
        assert min(a_rows) != min(c_rows)


class TestBuildDiagramRegions:
    async def test_table_header_region_resolves_to_table_path(self) -> None:
        desc = TableDescription(schema="dbo", table="users", columns=[])
        describe = _describe_from({("dbo", "users"): desc})
        result = await build_diagram(["dbo", "users"], describe)

        region = next(r for r in result.regions if r.path == ["dbo", "users"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert "dbo.users" in span

    async def test_column_region_resolves_to_column_path(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns", "id"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "id"

    async def test_column_region_offset_accounts_for_multibyte_box_chars(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns", "id"])
        line = result.diagram.splitlines()[region.row]
        assert line.startswith("│ id")
        # "│ " is 4 bytes (│ is 3-byte UTF-8) but only 2 characters — the
        # byte offset must reflect that, not the character index.
        assert region.col_start == 4

    async def test_table_referenced_twice_still_gets_drawn_as_one_box(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        b = TableDescription(
            table="b",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="shared_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="shared_id", table="shared", ref_column="id")
            ],
        )
        shared = TableDescription(
            table="shared", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from(
            {("root",): root, ("a",): a, ("b",): b, ("shared",): shared}
        )
        result = await build_diagram(["root"], describe)

        # "shared" has one column, so a single box is 3 rows (top border,
        # one column row, bottom border); a duplicate box would add more rows.
        matches = [r for r in result.regions if r.path == ["shared"]]
        assert len({r.row for r in matches}) == 3

    async def test_ellipsis_region_resolves_to_the_table_column_list(self) -> None:
        desc = TableDescription(
            table="users",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="name", type="TEXT"),
            ],
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert span == "..."

    async def test_unresolved_reference_still_gets_a_region(self) -> None:
        desc = TableDescription(
            table="orders",
            columns=[ColumnInfo(name="user_id", type="INTEGER")],
            outgoing_references=[
                TableReference(column="user_id", table="users", ref_column="id")
            ],
        )
        describe = _describe_from({("orders",): desc})
        result = await build_diagram(["orders"], describe)

        region = next(r for r in result.regions if r.path == ["users"])
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert "users" in span

    async def test_table_header_region_has_table_kind(self) -> None:
        desc = TableDescription(table="users", columns=[])
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users"])
        assert region.kind == "table"

    async def test_column_region_has_column_kind(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        region = next(r for r in result.regions if r.path == ["users", "columns", "id"])
        assert region.kind == "column"

    async def test_relationship_gets_an_edge_kind_region(self) -> None:
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

        edge_regions = [r for r in result.regions if r.kind == "edge"]
        assert len(edge_regions) == 1
        region = edge_regions[0]
        assert region.path == ["orders", "relationships", "user_id"]
        line = result.diagram.splitlines()[region.row]
        span = line.encode()[region.col_start : region.col_end].decode()
        assert re.fullmatch(r"\*─+1", span)

    async def test_multi_row_edge_regions_share_the_same_path(self) -> None:
        # The mid->leaf edge in the skip-edge fixture detours vertically past
        # root's rank, spanning several rows — every row it touches gets its
        # own region, all sharing the same path.
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="mid_id", type="INTEGER"),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="mid_id", table="mid", ref_column="id"),
                TableReference(column="leaf_id", table="leaf", ref_column="id"),
            ],
        )
        mid = TableDescription(
            table="mid",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="leaf_id", table="leaf", ref_column="id")
            ],
        )
        leaf = TableDescription(
            table="leaf", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("root",): root, ("mid",): mid, ("leaf",): leaf})
        result = await build_diagram(["root"], describe)

        edge_regions = [
            r
            for r in result.regions
            if r.kind == "edge" and r.path == ["mid", "relationships", "leaf_id"]
        ]
        assert len(edge_regions) > 1
        assert len({tuple(r.path) for r in edge_regions}) == 1

    async def test_table_box_border_gets_a_region_on_every_row(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        table_regions = [r for r in result.regions if r.path == ["users"]]
        # top border, one column row (left + right border), bottom border.
        assert len({r.row for r in table_regions}) == 3

    async def test_table_border_region_on_column_row_does_not_overlap_column(
        self,
    ) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        column_region = next(
            r for r in result.regions if r.path == ["users", "columns", "id"]
        )
        border_regions = [
            r
            for r in result.regions
            if r.path == ["users"] and r.row == column_region.row
        ]
        for border in border_regions:
            overlap = border.col_start < column_region.col_end and (
                column_region.col_start < border.col_end
            )
            assert not overlap

    async def test_table_regions_all_share_the_same_path(self) -> None:
        desc = TableDescription(
            table="users", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("users",): desc})
        result = await build_diagram(["users"], describe)

        table_regions = [r for r in result.regions if r.kind == "table"]
        assert all(r.path == ["users"] for r in table_regions)

    async def test_every_interior_row_gets_a_left_and_right_border_region(self) -> None:
        desc = TableDescription(
            table="t",
            columns=[ColumnInfo(name=n, type="INTEGER", pk=True) for n in "abcd"],
        )
        describe = _describe_from({("t",): desc})
        result = await build_diagram(["t"], describe)

        table_regions = [r for r in result.regions if r.path == ["t"]]
        interior_rows = range(1, 5)  # rows between the top and bottom border
        for row in interior_rows:
            assert sum(1 for r in table_regions if r.row == row) == 2

    async def test_skip_edge_never_overlaps_an_unrelated_table_region(self) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="mid_id", type="INTEGER"),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="mid_id", table="mid", ref_column="id"),
                TableReference(column="leaf_id", table="leaf", ref_column="id"),
            ],
        )
        mid = TableDescription(
            table="mid",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="leaf_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="leaf_id", table="leaf", ref_column="id")
            ],
        )
        leaf = TableDescription(
            table="leaf", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from({("root",): root, ("mid",): mid, ("leaf",): leaf})
        result = await build_diagram(["root"], describe)

        # The root->leaf edge skips over mid's rank; its path must never
        # share cells with mid's box, or the canvas would silently swallow
        # that stretch of the connector (box cells always win).
        skip_edge = [
            r
            for r in result.regions
            if r.kind == "edge" and r.path == ["root", "relationships", "leaf_id"]
        ]
        mid_regions = [r for r in result.regions if r.path == ["mid"]]
        for edge in skip_edge:
            for table in mid_regions:
                if edge.row != table.row:
                    continue
                overlap = edge.col_start < table.col_end and (
                    table.col_start < edge.col_end
                )
                assert not overlap

    async def test_edges_fanning_out_from_the_same_box_never_share_a_cell(
        self,
    ) -> None:
        root = TableDescription(
            table="root",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="a_id", type="INTEGER"),
                ColumnInfo(name="b_id", type="INTEGER"),
                ColumnInfo(name="c_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="a_id", table="a", ref_column="id"),
                TableReference(column="b_id", table="b", ref_column="id"),
                TableReference(column="c_id", table="c", ref_column="id"),
            ],
        )
        a = TableDescription(
            table="a", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        b = TableDescription(
            table="b",
            columns=[
                ColumnInfo(name="id", type="INTEGER", pk=True),
                ColumnInfo(name="d_id", type="INTEGER"),
            ],
            outgoing_references=[
                TableReference(column="d_id", table="d", ref_column="id")
            ],
        )
        c = TableDescription(
            table="c", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        d = TableDescription(
            table="d", columns=[ColumnInfo(name="id", type="INTEGER", pk=True)]
        )
        describe = _describe_from(
            {("root",): root, ("a",): a, ("b",): b, ("c",): c, ("d",): d}
        )
        result = await build_diagram(["root"], describe)

        # Every relationship leaving "root" shares its box side with the
        # others; none of their routed paths may occupy the same cell,
        # whichever direction each one has to jog to reach its target.
        cells_by_path: dict[tuple[str, ...], set[tuple[int, int]]] = {}
        for r in result.regions:
            if r.kind != "edge":
                continue
            cells = cells_by_path.setdefault(tuple(r.path), set())
            cells.update((r.row, c) for c in range(r.col_start, r.col_end))

        paths = list(cells_by_path)
        for i, path_a in enumerate(paths):
            for path_b in paths[i + 1 :]:
                assert not (cells_by_path[path_a] & cells_by_path[path_b])
