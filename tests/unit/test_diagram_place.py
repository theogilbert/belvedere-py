from belvedere.diagram.graph import GraphEdge, GraphNode
from belvedere.diagram.layout import compute_layout
from belvedere.diagram.place import place
from belvedere.protocol import ColumnInfo


def _node(id_: int, n_columns: int = 1) -> GraphNode:
    columns = [ColumnInfo(name=f"c{i}", type="INTEGER") for i in range(n_columns)]
    return GraphNode(id=id_, name=f"t{id_}", path=[f"t{id_}"], columns=columns)


def _place(nodes: list[GraphNode], edges: list[GraphEdge]):
    layout = compute_layout(nodes, edges)
    return place(nodes, edges, layout)


class TestRectPlacement:
    def test_no_two_rects_overlap(self) -> None:
        nodes = [_node(i, n_columns=i + 1) for i in range(5)]
        edges = [GraphEdge(0, 1), GraphEdge(0, 2), GraphEdge(1, 3), GraphEdge(2, 4)]
        result = _place(nodes, edges)
        rects = list(result.rects.values())
        for i, a in enumerate(rects):
            for b in rects[i + 1 :]:
                row_overlap = a.top < b.top + b.height and b.top < a.top + a.height
                col_overlap = a.left < b.left + b.width and b.left < a.left + a.width
                assert not (row_overlap and col_overlap)

    def test_rects_stay_within_reported_bounds(self) -> None:
        nodes = [_node(i) for i in range(4)]
        edges = [GraphEdge(0, 1), GraphEdge(1, 2), GraphEdge(2, 3)]
        result = _place(nodes, edges)
        n_rows, n_cols = result.bounds
        for rect in result.rects.values():
            assert 0 <= rect.top and rect.top + rect.height <= n_rows
            assert 0 <= rect.left and rect.left + rect.width <= n_cols

    def test_aligned_direct_neighbors_share_a_row(self) -> None:
        # A tall box and a short one, connected directly — alignment should
        # nudge the short one so both boxes' vertical centers line up.
        tall = _node(0, n_columns=5)
        small = _node(1, n_columns=1)
        edges = [GraphEdge(0, 1)]
        result = _place([tall, small], edges)
        tall_center = result.rects[0].top + result.rects[0].height // 2
        small_center = result.rects[1].top + result.rects[1].height // 2
        assert tall_center == small_center


class TestChannelOverprovisioning:
    def test_channel_width_covers_every_crossing_edge(self) -> None:
        # 0 at column 0, 1/2 at column 1, 3 at column 2: the 0-3 edge crosses
        # both column boundaries, on top of the direct 0-1/0-2/1-3/2-3 edges.
        nodes = [_node(i) for i in range(4)]
        edges = [
            GraphEdge(0, 1),
            GraphEdge(0, 2),
            GraphEdge(1, 3),
            GraphEdge(2, 3),
            GraphEdge(0, 3),
        ]
        result = _place(nodes, edges)
        layout = compute_layout(nodes, edges)
        columns = sorted({layout.column[n.id] for n in nodes})
        for lo, hi in zip(columns, columns[1:]):
            left_edge = max(
                r.left + r.width
                for nid, r in result.rects.items()
                if layout.column[nid] == lo
            )
            right_edge = min(
                r.left for nid, r in result.rects.items() if layout.column[nid] == hi
            )
            crossing = sum(
                1
                for e in edges
                if min(layout.column[e.source], layout.column[e.target]) <= lo
                and max(layout.column[e.source], layout.column[e.target]) >= hi
            )
            assert right_edge - left_edge >= crossing
