from belvedere.diagram.graph import GraphEdge, GraphNode
from belvedere.diagram.layout import compute_layout


def _node(id_: int, rank: int) -> GraphNode:
    return GraphNode(id=id_, name=str(id_), path=[str(id_)], rank=rank)


class TestRankAssignment:
    def test_rank_carries_through_from_bfs_depth(self) -> None:
        nodes = [_node(0, 0), _node(1, 1), _node(2, 2)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2)]
        layout = compute_layout(nodes, edges)
        assert layout.positions[0].rank == 0
        assert layout.positions[1].rank == 1
        assert layout.positions[2].rank == 2

    def test_same_rank_edge_bumps_the_target(self) -> None:
        nodes = [_node(0, 0), _node(1, 1), _node(2, 1)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2)]
        layout = compute_layout(nodes, edges)
        assert layout.positions[1].rank == 1
        assert layout.positions[2].rank == 2

    def test_same_rank_bump_cascades_until_stable(self) -> None:
        # 1 and 2 tied at rank 1 with a direct edge; bumping 2 to rank 2 must
        # not collide with anything else discovered at that rank.
        nodes = [_node(0, 0), _node(1, 1), _node(2, 1), _node(3, 2)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2), GraphEdge(1, 3)]
        layout = compute_layout(nodes, edges)
        assert layout.positions[2].rank != layout.positions[1].rank
        assert layout.positions[2].rank != layout.positions[0].rank


class TestLongEdgeDecomposition:
    def test_adjacent_rank_edge_is_not_decomposed(self) -> None:
        nodes = [_node(0, 0), _node(1, 1)]
        edges = [GraphEdge(0, 1)]
        layout = compute_layout(nodes, edges)
        assert layout.routed_edges[0].nodes == [0, 1]

    def test_long_edge_inserts_a_dummy_at_each_intermediate_rank(self) -> None:
        nodes = [_node(0, 0), _node(1, 1), _node(2, 2), _node(3, 3)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2), GraphEdge(2, 3), GraphEdge(0, 3)]
        layout = compute_layout(nodes, edges)
        long_edge = next(
            e for e in layout.routed_edges if e.nodes[0] == 0 and e.nodes[-1] == 3
        )
        assert len(long_edge.nodes) == 4  # source, 2 dummies (ranks 1 and 2), target
        dummy_ranks = [layout.positions[n].rank for n in long_edge.nodes[1:-1]]
        assert dummy_ranks == [1, 2]

    def test_dummy_nodes_are_flagged_as_dummy(self) -> None:
        nodes = [_node(0, 0), _node(1, 2)]
        edges = [GraphEdge(0, 1)]
        layout = compute_layout(nodes, edges)
        dummy_ids = [nid for nid, pos in layout.positions.items() if pos.dummy]
        assert len(dummy_ids) == 1
        assert layout.positions[dummy_ids[0]].rank == 1

    def test_adjacent_band_edge_is_not_decomposed(self) -> None:
        # Ranks 3 and 6 sit in adjacent bands; the edge rides the wrap lane
        # between the bands as a single hop, so no dummies are needed.
        nodes = [_node(0, 3), _node(1, 6)]
        edges = [GraphEdge(0, 1)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert layout.routed_edges[0].nodes == [0, 1]
        assert not any(pos.dummy for pos in layout.positions.values())

    def test_reversed_long_edge_decomposes_in_the_right_direction(self) -> None:
        # source at the higher rank, target at the lower rank
        nodes = [_node(0, 3), _node(1, 0)]
        edges = [GraphEdge(0, 1)]
        layout = compute_layout(nodes, edges)
        chain = layout.routed_edges[0].nodes
        ranks = [layout.positions[n].rank for n in chain]
        assert ranks == [3, 2, 1, 0]


class TestOrdering:
    def test_barycenter_pass_uncrosses_a_simple_diamond(self) -> None:
        # A,B at rank 0; X,Y at rank 1. Edges A-Y, B-X are "crossed" in
        # discovery order — ordering should realign them (A with Y, B with X)
        # on the same row.
        nodes = [_node(0, 0), _node(1, 0), _node(2, 1), _node(3, 1)]
        edges = [GraphEdge(0, 3), GraphEdge(1, 2)]
        layout = compute_layout(nodes, edges)
        assert layout.positions[0].row == layout.positions[3].row
        assert layout.positions[1].row == layout.positions[2].row


class TestWrapNodeOrdering:
    def test_node_with_downward_wrap_hop_sinks_to_the_stack_bottom(self) -> None:
        # 0 and 1 share rank 4; only 0 connects into the band below, so it
        # must end up below 1 to keep its bottom-border drop clear.
        nodes = [_node(0, 4), _node(1, 4), _node(2, 5)]
        edges = [GraphEdge(0, 2)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert layout.positions[0].row == 1
        assert layout.positions[1].row == 0

    def test_node_with_upward_wrap_hop_floats_to_the_stack_top(self) -> None:
        # 1 and 2 share rank 5; only 2 connects into the band above, so it
        # must end up above 1 to keep its top-border drop clear.
        nodes = [_node(0, 4), _node(1, 5), _node(2, 5)]
        edges = [GraphEdge(0, 2)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert layout.positions[2].row == 0
        assert layout.positions[1].row == 1


class TestBandChunking:
    def test_nodes_within_band_size_share_band_zero(self) -> None:
        nodes = [_node(i, i) for i in range(5)]
        edges = [GraphEdge(i, i + 1) for i in range(4)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert {layout.positions[i].band for i in range(5)} == {0}
        assert [layout.positions[i].col for i in range(5)] == [0, 1, 2, 3, 4]

    def test_rank_past_band_size_wraps_to_the_next_band(self) -> None:
        nodes = [_node(i, i) for i in range(7)]
        edges = [GraphEdge(i, i + 1) for i in range(6)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert layout.positions[4].band == 0
        assert layout.positions[5].band == 1

    def test_odd_bands_snake_right_to_left(self) -> None:
        # Rank 5 wraps into band 1, which runs right-to-left: it lands in the
        # rightmost column, directly below its rank-4 neighbor.
        nodes = [_node(i, i) for i in range(7)]
        edges = [GraphEdge(i, i + 1) for i in range(6)]
        layout = compute_layout(nodes, edges, band_size=5)
        assert layout.positions[5].col == 4
        assert layout.positions[6].col == 3
