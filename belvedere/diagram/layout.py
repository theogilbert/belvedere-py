"""Layered graph layout (Sugiyama-style): assigns every node a rank (from
BFS-derived depth), decomposes long edges into per-rank hops through dummy
nodes, orders nodes within each rank to reduce line crossings (best-effort,
not optimal), and chunks ranks into vertically-stacked bands once a row would
grow past ``band_size`` columns wide. Bands snake: odd bands run right-to-left,
so a wrapped rank lands directly below its neighbor instead of jumping back to
the far-left edge of the diagram.

Pure functions over plain graph data — no rendering or canvas concerns, so the
algorithm can be tested against small synthetic graphs directly.
"""

from collections import defaultdict
from dataclasses import dataclass

from .graph import GraphEdge, GraphNode

_DEFAULT_BAND_SIZE = 5


@dataclass
class LayoutNode:
    rank: int
    band: int
    """Which vertically-stacked band this node's rank falls into."""
    col: int
    """Display column within its band. Bands snake, so even bands run
    left-to-right (``rank % band_size``) and odd bands right-to-left."""
    row: int
    """Order among the other nodes sharing this rank — determines vertical position."""
    dummy: bool
    """True for a bend-point inserted to route an edge through intermediate ranks."""


@dataclass
class RoutedEdge:
    nodes: list[int]
    """Node ids from source to target, inclusive, one hop per adjacent rank —
    intermediate entries (if any) are dummy node ids."""
    fk_at_start: bool = True
    """Whether ``nodes[0]`` (rather than ``nodes[-1]``) owns the FK column."""
    one_to_one: bool = False
    """Whether the FK column is itself constrained unique."""
    fk_column: str = ""
    """Name of the FK column on whichever endpoint owns it."""


@dataclass
class Layout:
    positions: dict[int, LayoutNode]
    """By node id — covers every real ``GraphNode.id`` plus any dummy ids."""
    routed_edges: list[RoutedEdge]
    band_size: int


def compute_layout(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    band_size: int = _DEFAULT_BAND_SIZE,
) -> Layout:
    real_ids = {n.id for n in nodes}
    rank = {n.id: n.rank for n in nodes}
    _bump_same_rank_edges(rank, edges)

    next_id = max(rank) + 1
    routed_edges: list[RoutedEdge] = []
    for edge in edges:
        chain, next_id = _decompose(edge, rank, next_id, band_size)
        routed_edges.append(
            RoutedEdge(
                nodes=chain,
                fk_at_start=edge.fk_side == "source",
                one_to_one=edge.one_to_one,
                fk_column=edge.fk_column,
            )
        )

    nodes_by_rank: dict[int, list[int]] = defaultdict(list)
    for node_id in sorted(rank, key=lambda i: (rank[i], i)):
        nodes_by_rank[rank[node_id]].append(node_id)

    _order_ranks(nodes_by_rank, routed_edges, rank)
    _order_wrap_nodes(nodes_by_rank, routed_edges, rank, band_size)

    positions = _assign_positions(nodes_by_rank, real_ids, band_size)
    return Layout(positions=positions, routed_edges=routed_edges, band_size=band_size)


def _bump_same_rank_edges(rank: dict[int, int], edges: list[GraphEdge]) -> None:
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if rank[edge.source] == rank[edge.target]:
                rank[edge.target] += 1
                changed = True


def _decompose(
    edge: GraphEdge, rank: dict[int, int], next_id: int, band_size: int
) -> tuple[list[int], int]:
    source_rank, target_rank = rank[edge.source], rank[edge.target]
    if abs(source_rank // band_size - target_rank // band_size) == 1:
        # Adjacent-band edge: routed as one hop along the wrap lane between
        # the bands (entering/leaving via top/bottom borders), so it needs no
        # per-rank bend points.
        return [edge.source, edge.target], next_id
    step = 1 if target_rank > source_rank else -1
    chain = [edge.source]
    for r in range(source_rank + step, target_rank, step):
        chain.append(next_id)
        rank[next_id] = r
        next_id += 1
    chain.append(edge.target)
    return chain, next_id


def _order_ranks(
    nodes_by_rank: dict[int, list[int]],
    routed_edges: list[RoutedEdge],
    rank: dict[int, int],
) -> None:
    neighbors: dict[int, list[int]] = defaultdict(list)
    for redge in routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            neighbors[u].append(v)
            neighbors[v].append(u)

    position: dict[int, int] = {}
    for ids in nodes_by_rank.values():
        for i, node_id in enumerate(ids):
            position[node_id] = i

    def barycenter(node_id: int, reference_rank: int) -> float:
        refs = [
            position[n] for n in neighbors[node_id] if rank.get(n) == reference_rank
        ]
        return sum(refs) / len(refs) if refs else position[node_id]

    if not nodes_by_rank:
        return
    min_rank, max_rank = min(nodes_by_rank), max(nodes_by_rank)
    for _ in range(2):
        for r in range(min_rank + 1, max_rank + 1):
            ids = nodes_by_rank.get(r)
            if not ids:
                continue
            ids.sort(key=lambda n: barycenter(n, r - 1))
            for i, node_id in enumerate(ids):
                position[node_id] = i
        for r in range(max_rank - 1, min_rank - 1, -1):
            ids = nodes_by_rank.get(r)
            if not ids:
                continue
            ids.sort(key=lambda n: barycenter(n, r + 1))
            for i, node_id in enumerate(ids):
                position[node_id] = i


def _order_wrap_nodes(
    nodes_by_rank: dict[int, list[int]],
    routed_edges: list[RoutedEdge],
    rank: dict[int, int],
    band_size: int,
) -> None:
    """A wrap hop leaves its upper node through the bottom border and enters
    its lower node through the top border, dropping straight through the node's
    column. Sink nodes with downward wrap hops to the bottom of their rank's
    stack (and float upward-wrapped ones to the top) so no rank-mate sits in
    the drop's path. Runs after the barycenter ordering and overrides it."""
    down: set[int] = set()
    up: set[int] = set()
    for redge in routed_edges:
        for u, v in zip(redge.nodes, redge.nodes[1:]):
            band_u, band_v = rank[u] // band_size, rank[v] // band_size
            if band_u == band_v:
                continue
            upper, lower = (u, v) if band_u < band_v else (v, u)
            down.add(upper)
            up.add(lower)

    for ids in nodes_by_rank.values():
        ids.sort(key=lambda n: (n in down) - (n in up))


def _assign_positions(
    nodes_by_rank: dict[int, list[int]], real_ids: set[int], band_size: int
) -> dict[int, LayoutNode]:
    positions: dict[int, LayoutNode] = {}
    for r, ids in nodes_by_rank.items():
        band, offset = divmod(r, band_size)
        col = band_size - 1 - offset if band % 2 else offset
        for row, node_id in enumerate(ids):
            positions[node_id] = LayoutNode(
                rank=r,
                band=band,
                col=col,
                row=row,
                dummy=node_id not in real_ids,
            )
    return positions
