from belvedere.diagram.graph import GraphEdge, GraphNode
from belvedere.diagram.layout import (
    _bump_same_column_edges,
    _order_columns,
    _partition_sides,
    compute_layout,
)


def _node(id_: int) -> GraphNode:
    return GraphNode(id=id_, name=str(id_), path=[str(id_)])


class TestHubSelection:
    def test_hub_is_the_max_degree_node(self) -> None:
        nodes = [_node(i) for i in range(4)]
        edges = [GraphEdge(2, 0), GraphEdge(2, 1), GraphEdge(2, 3)]
        layout = compute_layout(nodes, edges)
        assert layout.hub == 2

    def test_ties_broken_by_lowest_id(self) -> None:
        nodes = [_node(i) for i in range(3)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2), GraphEdge(0, 2)]
        layout = compute_layout(nodes, edges)
        assert layout.hub == 0


class TestSidePartition:
    def test_lpt_keeps_each_component_together_on_one_side(self) -> None:
        nodes = [_node(i) for i in range(6)]
        edges = [
            GraphEdge(0, 1),
            GraphEdge(1, 2),
            GraphEdge(2, 3),
            GraphEdge(0, 4),
            GraphEdge(4, 5),
        ]
        side = _partition_sides(nodes, edges, hub=0)
        assert side[1] == side[2] == side[3]
        assert side[4] == side[5]
        assert side[1] != side[4]

    def test_degenerate_hub_splits_single_component_into_two_sides(self) -> None:
        # Removing hub 0 leaves 1-2-3 connected as one component (hub is not
        # a cut vertex) — the bisection fallback must still produce two
        # non-empty, fully-assigned sides.
        nodes = [_node(i) for i in range(4)]
        edges = [
            GraphEdge(0, 1),
            GraphEdge(0, 2),
            GraphEdge(0, 3),
            GraphEdge(1, 2),
            GraphEdge(2, 3),
        ]
        side = _partition_sides(nodes, edges, hub=0)
        assert set(side) == {1, 2, 3}
        assert set(side.values()) == {1, -1}

    def test_partition_is_deterministic(self) -> None:
        nodes = [_node(i) for i in range(4)]
        edges = [
            GraphEdge(0, 1),
            GraphEdge(0, 2),
            GraphEdge(0, 3),
            GraphEdge(1, 2),
            GraphEdge(2, 3),
        ]
        first = _partition_sides(nodes, edges, hub=0)
        second = _partition_sides(nodes, edges, hub=0)
        assert first == second


class TestSignedColumns:
    def test_chain_places_hub_centrally_with_sequential_columns(self) -> None:
        nodes = [_node(i) for i in range(4)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2), GraphEdge(2, 3)]
        layout = compute_layout(nodes, edges)
        assert layout.hub == 1
        assert layout.column == {0: 0, 1: 1, 2: 2, 3: 3}

    def test_single_node_graph(self) -> None:
        layout = compute_layout([_node(0)], [])
        assert layout.hub == 0
        assert layout.column == {0: 0}
        assert layout.row_order == {0: [0]}

    def test_bump_pushes_a_same_column_edge_away_from_the_hub(self) -> None:
        column = {0: 0, 1: 1, 2: 1}  # hub=0; 1 and 2 collide at column 1
        _bump_same_column_edges(column, hub=0, edges=[GraphEdge(1, 2)])
        assert column[2] == 2


class TestRowOrdering:
    def test_barycenter_pass_uncrosses_a_simple_diamond(self) -> None:
        # 0,1 at column 0; 2,3 at column 1. Edges 0-3, 1-2 are crossed in
        # discovery order — ordering should realign them onto shared rows.
        nodes = [_node(i) for i in range(4)]
        edges = [GraphEdge(0, 3), GraphEdge(1, 2)]
        column = {0: 0, 1: 0, 2: 1, 3: 1}
        row_order = _order_columns(nodes, edges, column)
        assert row_order[0].index(0) == row_order[1].index(3)
        assert row_order[0].index(1) == row_order[1].index(2)

    def test_isolated_nodes_keep_stable_id_order(self) -> None:
        nodes = [_node(i) for i in range(3)]
        column = {0: 0, 1: 0, 2: 0}
        row_order = _order_columns(nodes, [], column)
        assert row_order[0] == [0, 1, 2]
