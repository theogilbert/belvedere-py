"""Per-edge A* routing over the placed canvas's character grid. Boxes are
removed from the grid entirely, so "an edge never crosses a table box"
(constraint 1) is unsatisfiable rather than checked. Pure: rects + edges →
paths — no canvas or drawing concerns, so the router is testable directly on
hand-built rectangles.

Each edge gets a mandatory ``STUB_LEN``-cell straight run out of its anchor
before any bend is allowed (constraint 4), searched multi-source (every
candidate anchor on the source box) to multi-target (every candidate anchor
on the target box) — the search itself picks which side of each box the
edge leaves from. Edges are routed most-constrained-first (shortest span),
then a rip-up-and-reroute pass re-routes the costliest few against the
finished field. A final compaction pass deletes unused grid rows/columns,
reclaiming the overprovisioned channel/margin space ``place.py`` reserved.
"""

import heapq
from dataclasses import dataclass

from .graph import GraphEdge, GraphNode
from .place import STUB_LEN, PlaceResult, Rect

W_BEND = 4
"""Cost added when a step's heading differs from the previous step's."""
W_CROSS = 12
"""Cost added when a step lands on a cell another edge already occupies on
the perpendicular axis (a crossing, as opposed to a forbidden collinear
overlap on the same axis)."""
W_HUG = 1
"""Cost added when a step lands on a cell orthogonally adjacent to any box —
a small penalty to keep connectors from hugging box borders when a route
further out is just as cheap otherwise."""
_RIP_UP_COUNT = 3
"""Number of highest-cost edges re-routed against the finished field after
the main pass, so an early edge's anchor/lane choice doesn't permanently
saddle a later edge with an avoidable detour."""

Cell = tuple[int, int]
"""``(row, col)`` grid coordinate."""
_State = tuple[Cell, str]
"""A search node: the cell plus the heading of the move that reached it —
needed because the bend cost depends on the previous step's direction."""

_DIRS: dict[str, tuple[int, int]] = {
    "N": (-1, 0),
    "S": (1, 0),
    "E": (0, 1),
    "W": (0, -1),
}
"""Row/col delta for each of the 4 headings a search step can move in."""
_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E"}
"""The heading a path must arrive with to continue straight into a stub
that exits in the given heading."""
_SIDE_HEADING = {"top": "N", "bottom": "S", "left": "W", "right": "E"}
"""Outward travel direction of a box side's mandatory stub."""


@dataclass
class RoutedEdge:
    points: list[Cell]
    """Every unit cell the connector passes through, source anchor to target
    anchor inclusive."""
    start: str
    end: str
    path: list[str]
    """``["relationships", fk_column]`` region path, prefixed by the owning
    table's own path."""


@dataclass
class _Stub:
    anchor: Cell
    """The cell touching the box border."""
    tip: Cell
    """``STUB_LEN`` steps out from ``anchor``, where the search proper begins."""
    heading: str
    """Direction of travel from ``anchor`` to ``tip`` (away from the box)."""
    cells: list[Cell]
    """``anchor``..``tip`` inclusive."""


@dataclass
class _RouteInfo:
    cells: list[Cell]
    cost: float
    source_anchor: Cell
    target_anchor: Cell


def route(
    nodes: list[GraphNode], edges: list[GraphEdge], place_result: PlaceResult
) -> list[RoutedEdge]:
    node_by_id = {n.id: n for n in nodes}
    rects = place_result.rects
    blocked = _blocked_cells(rects)
    bounds = place_result.bounds
    used_anchors: dict[int, set[Cell]] = {n.id: set() for n in nodes}
    occupied: dict[Cell, dict[str, int]] = {}

    order = sorted(
        range(len(edges)),
        key=lambda i: (
            _manhattan_span(edges[i], rects),
            edges[i].source,
            edges[i].target,
        ),
    )

    routed: dict[int, _RouteInfo] = {}
    for i in order:
        info = _route_edge(edges[i], rects, blocked, used_anchors, occupied, bounds)
        routed[i] = info
        _mark_occupied(occupied, info, i)
        _consume_anchors(used_anchors, edges[i], info)

    worst = sorted(routed, key=lambda i: -routed[i].cost)[:_RIP_UP_COUNT]
    for i in worst:
        _mark_occupied(occupied, routed[i], i, remove=True)
        _release_anchors(used_anchors, edges[i], routed[i])
        info = _route_edge(edges[i], rects, blocked, used_anchors, occupied, bounds)
        routed[i] = info
        _mark_occupied(occupied, info, i)
        _consume_anchors(used_anchors, edges[i], info)

    return [_emit(edges[i], node_by_id, routed[i]) for i in range(len(edges))]


def compact(
    rects: dict[int, Rect], routed: list[RoutedEdge]
) -> tuple[dict[int, Rect], list[RoutedEdge], tuple[int, int]]:
    """Deletes grid rows/columns with no box cell and no edge cell, down to a
    minimum gap of 1 between remaining content and 0 at the outer border —
    reclaiming the channel/margin space ``place.py`` overprovisioned."""
    used = _blocked_cells(rects) | {cell for r in routed for cell in r.points}
    used_rows = sorted({r for r, _ in used})
    used_cols = sorted({c for _, c in used})
    row_map = _compact_axis(used_rows)
    col_map = _compact_axis(used_cols)

    new_rects = {
        nid: Rect(
            top=row_map[r.top], left=col_map[r.left], height=r.height, width=r.width
        )
        for nid, r in rects.items()
    }
    new_routed = [
        RoutedEdge(
            points=[(row_map[r], col_map[c]) for r, c in re.points],
            start=re.start,
            end=re.end,
            path=re.path,
        )
        for re in routed
    ]
    n_rows = row_map[used_rows[-1]] + 1 if used_rows else 0
    n_cols = col_map[used_cols[-1]] + 1 if used_cols else 0
    return new_rects, new_routed, (n_rows, n_cols)


def _blocked_cells(rects: dict[int, Rect]) -> set[Cell]:
    return {
        (r, c)
        for rect in rects.values()
        for r in range(rect.top, rect.top + rect.height)
        for c in range(rect.left, rect.left + rect.width)
    }


def _manhattan_span(edge: GraphEdge, rects: dict[int, Rect]) -> int:
    a, b = rects[edge.source], rects[edge.target]
    ar = a.top + a.height // 2, a.left + a.width // 2
    br = b.top + b.height // 2, b.left + b.width // 2
    return abs(ar[0] - br[0]) + abs(ar[1] - br[1])


def _side_positions(rect: Rect, side: str) -> list[Cell]:
    top, left, h, w = rect.top, rect.left, rect.height, rect.width
    if side == "left":
        return [(r, left - 1) for r in range(top + 1, top + h - 1)]
    if side == "right":
        return [(r, left + w) for r in range(top + 1, top + h - 1)]
    if side == "top":
        return [(top - 1, c) for c in range(left + 1, left + w - 1)]
    return [(top + h, c) for c in range(left + 1, left + w - 1)]  # bottom


def _stub_cells(anchor: Cell, heading: str, steps: int) -> list[Cell]:
    dr, dc = _DIRS[heading]
    return [(anchor[0] + dr * i, anchor[1] + dc * i) for i in range(steps + 1)]


def _in_bounds(cell: Cell, bounds: tuple[int, int]) -> bool:
    r, c = cell
    n_rows, n_cols = bounds
    return 0 <= r < n_rows and 0 <= c < n_cols


def _stub_candidates(
    rect: Rect,
    blocked: set[Cell],
    used: set[Cell],
    bounds: tuple[int, int],
) -> list[_Stub]:
    def build(exclude: set[Cell]) -> list[_Stub]:
        stubs = []
        for side, heading in _SIDE_HEADING.items():
            for anchor in _side_positions(rect, side):
                if anchor in exclude:
                    continue
                cells = _stub_cells(anchor, heading, STUB_LEN)
                if all(_in_bounds(c, bounds) and c not in blocked for c in cells):
                    stubs.append(
                        _Stub(
                            anchor=anchor, tip=cells[-1], heading=heading, cells=cells
                        )
                    )
        return stubs

    stubs = build(used)
    return stubs if stubs else build(set())  # pool exhausted — fall back to reuse


def _heuristic(cell: Cell, tips: list[Cell]) -> float:
    if not tips:
        return 0
    return min(
        abs(cell[0] - t[0])
        + abs(cell[1] - t[1])
        + (W_BEND if cell[0] != t[0] and cell[1] != t[1] else 0)
        for t in tips
    )


def _adjacent_to_box(cell: Cell, blocked: set[Cell]) -> bool:
    r, c = cell
    return any((r + dr, c + dc) in blocked for dr, dc in _DIRS.values())


def _astar(
    sources: list[_Stub],
    targets: list[_Stub],
    blocked: set[Cell],
    occupied: dict[Cell, dict[str, int]],
    bounds: tuple[int, int],
) -> tuple[list[Cell], float]:
    tip_to_stub: dict[Cell, _Stub] = {s.tip: s for s in targets}
    tip_cells = list(tip_to_stub)

    g_score: dict[_State, float] = {}
    came_from: dict[_State, _State | None] = {}
    heap: list[tuple[float, float, _State]] = []
    for s in sources:
        state = (s.tip, s.heading)
        g_score[state] = STUB_LEN
        came_from[state] = None
        heapq.heappush(heap, (STUB_LEN + _heuristic(s.tip, tip_cells), STUB_LEN, state))

    visited: set[_State] = set()
    best: tuple[float, _State] | None = None
    """``(total_cost, state)`` of the cheapest completed route found so far —
    a completion is any visited state whose cell is a target's stub tip; the
    heading it was reached with may differ from the stub's own direction,
    since a bend exactly at the tip (3 cells from the anchor) is allowed."""
    while heap:
        f, g, state = heapq.heappop(heap)
        if best is not None and f >= best[0]:
            break  # heuristic is admissible: no unexplored state can beat best
        if state in visited:
            continue
        visited.add(state)
        cell, heading = state
        if cell in tip_to_stub:
            target_stub = tip_to_stub[cell]
            bend = 0 if heading == _OPPOSITE[target_stub.heading] else W_BEND
            total = g + bend + STUB_LEN
            if best is None or total < best[0]:
                best = (total, state)
        for new_heading, (dr, dc) in _DIRS.items():
            neighbor = (cell[0] + dr, cell[1] + dc)
            if not _in_bounds(neighbor, bounds) or neighbor in blocked:
                continue
            axis = "h" if new_heading in ("E", "W") else "v"
            cell_axes = occupied.get(neighbor, {})
            if axis in cell_axes:
                continue  # collinear overlap with another edge — forbidden
            step_cost = 1.0
            if new_heading != heading:
                step_cost += W_BEND
            if cell_axes:
                step_cost += W_CROSS  # perpendicular crossing
            if _adjacent_to_box(neighbor, blocked):
                step_cost += W_HUG
            new_state = (neighbor, new_heading)
            new_g = g + step_cost
            if new_g < g_score.get(new_state, float("inf")):
                g_score[new_state] = new_g
                came_from[new_state] = state
                heapq.heappush(
                    heap, (new_g + _heuristic(neighbor, tip_cells), new_g, new_state)
                )

    assert best is not None, "no route found despite overprovisioned margin"
    total_cost, goal_state = best

    states = []
    state: _State | None = goal_state
    while state is not None:
        states.append(state)
        state = came_from[state]
    states.reverse()
    search_path = [c for c, _ in states]

    source_stub = next(s for s in sources if (s.tip, s.heading) == states[0])
    target_stub = tip_to_stub[goal_state[0]]
    full = source_stub.cells + search_path[1:] + list(reversed(target_stub.cells))[1:]
    return full, total_cost


def _route_edge(
    edge: GraphEdge,
    rects: dict[int, Rect],
    blocked: set[Cell],
    used_anchors: dict[int, set[Cell]],
    occupied: dict[Cell, dict[str, int]],
    bounds: tuple[int, int],
) -> _RouteInfo:
    sources = _stub_candidates(
        rects[edge.source], blocked, used_anchors[edge.source], bounds
    )
    targets = _stub_candidates(
        rects[edge.target], blocked, used_anchors[edge.target], bounds
    )
    cells, cost = _astar(sources, targets, blocked, occupied, bounds)
    return _RouteInfo(
        cells=cells, cost=cost, source_anchor=cells[0], target_anchor=cells[-1]
    )


def _mark_occupied(
    occupied: dict[Cell, dict[str, int]],
    info: _RouteInfo,
    edge_id: int,
    remove: bool = False,
) -> None:
    for a, b in zip(info.cells, info.cells[1:]):
        axis = "h" if a[0] == b[0] else "v"
        for cell in (a, b):
            if remove:
                if occupied.get(cell, {}).get(axis) == edge_id:
                    del occupied[cell][axis]
            else:
                occupied.setdefault(cell, {})[axis] = edge_id


def _consume_anchors(
    used_anchors: dict[int, set[Cell]], edge: GraphEdge, info: _RouteInfo
) -> None:
    used_anchors[edge.source].add(info.source_anchor)
    used_anchors[edge.target].add(info.target_anchor)


def _release_anchors(
    used_anchors: dict[int, set[Cell]], edge: GraphEdge, info: _RouteInfo
) -> None:
    used_anchors[edge.source].discard(info.source_anchor)
    used_anchors[edge.target].discard(info.target_anchor)


def _emit(
    edge: GraphEdge, node_by_id: dict[int, GraphNode], info: _RouteInfo
) -> RoutedEdge:
    if edge.one_to_one:
        start, end = "1", "1"
    else:
        start, end = ("*", "1") if edge.fk_side == "source" else ("1", "*")
    owner_id = edge.source if edge.fk_side == "source" else edge.target
    owner_path = node_by_id[owner_id].path
    edge_path = [*owner_path, "relationships", edge.fk_column]
    return RoutedEdge(points=info.cells, start=start, end=end, path=edge_path)


def _compact_axis(used: list[int]) -> dict[int, int]:
    remap: dict[int, int] = {}
    cursor = 0
    prev: int | None = None
    for v in used:
        if prev is not None:
            gap = v - prev - 1
            cursor += 1 + min(gap, 1)
        remap[v] = cursor
        prev = v
    return remap
