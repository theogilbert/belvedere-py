import pytest

from grannos.diagram.graph import GraphEdge, GraphNode
from grannos.diagram.place import STUB_LEN, PlaceResult, Rect
from grannos.diagram.route import (
    NoRouteError,
    _adjacent_to_any,
    _astar,
    _blocked_cells,
    _stub_candidates,
    compact,
    route,
)


class TestAdjacentAnchorMalus:
    def test_anchor_adjacency_cost_steers_away_from_a_claimed_neighbor(self) -> None:
        rect_a = Rect(top=10, left=10, height=8, width=12)
        rect_b = Rect(top=10, left=50, height=8, width=12)
        blocked = _blocked_cells({0: rect_a, 1: rect_b})
        bounds = (30, 70)
        sources = _stub_candidates(rect_a, blocked, set(), bounds)
        targets = _stub_candidates(rect_b, blocked, set(), bounds)

        baseline_cells, _ = _astar(sources, targets, blocked, {}, set(), 0, bounds)
        natural_anchor = baseline_cells[0]

        claimed = {(natural_anchor[0] + 1, natural_anchor[1])}  # right next to it
        steered_cells, _ = _astar(sources, targets, blocked, {}, claimed, 1000, bounds)
        assert steered_cells[0] != natural_anchor
        assert not _adjacent_to_any(steered_cells[0], claimed)

    def test_route_never_steps_onto_a_claimed_anchor_cell(self) -> None:
        rect_a = Rect(top=10, left=10, height=6, width=12)
        rect_b = Rect(top=10, left=50, height=6, width=12)
        blocked = _blocked_cells({0: rect_a, 1: rect_b})
        bounds = (30, 70)
        sources = _stub_candidates(rect_a, blocked, set(), bounds)
        targets = _stub_candidates(rect_b, blocked, set(), bounds)
        claimed = {(13, 30)}  # sits on the natural straight path between the boxes
        cells, _ = _astar(sources, targets, blocked, {}, claimed, 0, bounds)
        assert claimed.isdisjoint(cells)


def _node(id_: int) -> GraphNode:
    return GraphNode(id=id_, name=str(id_), path=[str(id_)])


def _place_result(rects: dict[int, Rect], margin: int = 8) -> PlaceResult:
    max_row = max(r.top + r.height for r in rects.values())
    max_col = max(r.left + r.width for r in rects.values())
    bounds = (max_row + margin, max_col + margin)
    return PlaceResult(rects=rects, box_lines={}, bounds=bounds)


def _is_straight(cells: list[tuple[int, int]]) -> bool:
    rows = {r for r, _ in cells}
    cols = {c for _, c in cells}
    return len(rows) == 1 or len(cols) == 1


def _axis_cells(points: list[tuple[int, int]]) -> set[tuple[tuple[int, int], str]]:
    result: set[tuple[tuple[int, int], str]] = set()
    for a, b in zip(points, points[1:]):
        axis = "h" if a[0] == b[0] else "v"
        result.add((a, axis))
        result.add((b, axis))
    return result


class TestRouteInvariants:
    def test_no_edge_cell_lands_inside_a_box(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=5, width=10),
            1: Rect(top=10, left=40, height=5, width=10),
        }
        nodes = [_node(0), _node(1)]
        edges = [GraphEdge(0, 1, fk_column="x")]
        routed = route(nodes, edges, _place_result(rects))
        blocked = _blocked_cells(rects)
        assert not (set(routed[0].points) & blocked)

    def test_no_bend_within_stub_len_of_either_anchor(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=6, width=12),
            1: Rect(top=10, left=50, height=6, width=12),
        }
        nodes = [_node(0), _node(1)]
        edges = [GraphEdge(0, 1, fk_column="x")]
        points = route(nodes, edges, _place_result(rects))[0].points
        assert _is_straight(points[: STUB_LEN + 1])
        assert _is_straight(points[-(STUB_LEN + 1) :])

    def test_two_edges_never_share_a_collinear_cell(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=6, width=12),
            1: Rect(top=10, left=50, height=6, width=12),
            2: Rect(top=25, left=10, height=6, width=12),
            3: Rect(top=25, left=50, height=6, width=12),
        }
        nodes = [_node(i) for i in range(4)]
        edges = [GraphEdge(0, 1, fk_column="a"), GraphEdge(2, 3, fk_column="b")]
        routed = route(nodes, edges, _place_result(rects))
        assert not (_axis_cells(routed[0].points) & _axis_cells(routed[1].points))

    def test_endpoints_touch_their_own_boxes(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=6, width=12),
            1: Rect(top=10, left=50, height=6, width=12),
        }
        nodes = [_node(0), _node(1)]
        edges = [GraphEdge(0, 1, fk_column="x")]
        points = route(nodes, edges, _place_result(rects))[0].points
        source, target = rects[0], rects[1]
        assert _adjacent_to(points[0], source)
        assert _adjacent_to(points[-1], target)

    def test_routing_is_deterministic(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=6, width=12),
            1: Rect(top=10, left=50, height=6, width=12),
            2: Rect(top=25, left=10, height=6, width=12),
        }
        nodes = [_node(i) for i in range(3)]
        edges = [GraphEdge(0, 1, fk_column="a"), GraphEdge(0, 2, fk_column="b")]
        place_result = _place_result(rects)
        first = [e.points for e in route(nodes, edges, place_result)]
        second = [e.points for e in route(nodes, edges, place_result)]
        assert first == second


class TestCompact:
    def test_compact_shrinks_unused_margin(self) -> None:
        rects = {
            0: Rect(top=10, left=10, height=5, width=10),
            1: Rect(top=10, left=40, height=5, width=10),
        }
        nodes = [_node(0), _node(1)]
        edges = [GraphEdge(0, 1, fk_column="x")]
        place_result = _place_result(rects, margin=20)
        routed = route(nodes, edges, place_result)
        _, _, bounds = compact(rects, routed)
        assert bounds[0] < place_result.bounds[0]
        assert bounds[1] < place_result.bounds[1]


def _adjacent_to(cell: tuple[int, int], rect: Rect) -> bool:
    r, c = cell
    return (
        rect.top - 1 <= r <= rect.top + rect.height
        and rect.left - 1 <= c <= rect.left + rect.width
    )


class TestUnroutableEdge:
    """A box pinned between the canvas border and its neighbour has nowhere to
    put a stub, so its edge cannot be routed at all."""

    def _boxed_in(self) -> tuple[list[GraphNode], list[GraphEdge], PlaceResult]:
        rects = {
            0: Rect(top=0, left=0, height=5, width=10),
            1: Rect(top=0, left=11, height=5, width=10),
        }
        nodes = [_node(0), _node(1)]
        edges = [GraphEdge(0, 1, fk_column="x")]
        place_result = PlaceResult(rects=rects, box_lines={}, bounds=(5, 21))
        return nodes, edges, place_result

    def test_raises_rather_than_asserting(self) -> None:
        nodes, edges, place_result = self._boxed_in()
        with pytest.raises(NoRouteError):
            route(nodes, edges, place_result)
