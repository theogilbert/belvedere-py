"""Layered graph layout (Sugiyama-style): assigns every node a rank (from
BFS-derived depth), decomposes long edges into per-rank hops through dummy
nodes, orders nodes within each rank to reduce line crossings (best-effort,
not optimal), and chunks ranks into vertically-stacked bands once a row would
grow past ``band_size`` columns wide.

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
    """Column within its band (``rank % band_size``)."""
    row: int
    """Order among the other nodes sharing this rank — determines vertical position."""
    dummy: bool
    """True for a bend-point inserted to route an edge through intermediate ranks."""


@dataclass
class RoutedEdge:
    nodes: list[int]
    """Node ids from source to target, inclusive, one hop per adjacent rank —
    intermediate entries (if any) are dummy node ids."""


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
        chain, next_id = _decompose(edge, rank, next_id)
        routed_edges.append(RoutedEdge(nodes=chain))

    nodes_by_rank: dict[int, list[int]] = defaultdict(list)
    for node_id in sorted(rank, key=lambda i: (rank[i], i)):
        nodes_by_rank[rank[node_id]].append(node_id)

    _order_ranks(nodes_by_rank, routed_edges, rank)

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
    edge: GraphEdge, rank: dict[int, int], next_id: int
) -> tuple[list[int], int]:
    source_rank, target_rank = rank[edge.source], rank[edge.target]
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


def _assign_positions(
    nodes_by_rank: dict[int, list[int]], real_ids: set[int], band_size: int
) -> dict[int, LayoutNode]:
    positions: dict[int, LayoutNode] = {}
    for r, ids in nodes_by_rank.items():
        for row, node_id in enumerate(ids):
            positions[node_id] = LayoutNode(
                rank=r,
                band=r // band_size,
                col=r % band_size,
                row=row,
                dummy=node_id not in real_ids,
            )
    return positions
