"""Hub-centered layered layout: picks the most-connected table as a structural
hub, partitions the rest of the graph into a left/right side per connected
component, and assigns every node a signed column (BFS distance from the hub,
signed by side). Row order within each column comes from barycenter sweeps.

This is deliberately abstract — no box sizes, no channels, no edge routing.
``place.py`` turns these grid positions into concrete rectangles; ``route.py``
turns rectangles into orthogonal paths. Pure functions over plain graph data,
so the algorithm is testable against small synthetic graphs directly.
"""

from collections import defaultdict, deque
from dataclasses import dataclass

from .graph import GraphEdge, GraphNode

_KL_MAX_ROUNDS = 20
"""Bound on Kernighan-Lin-style swap rounds when bisecting a single connected
component (degenerate case: the hub is not a cut vertex)."""


@dataclass
class Layout:
    column: dict[int, int]
    """Every node's display column, 0-indexed left to right. The hub sits at
    the column matching the side with more/heavier left-side content; there
    is no dedicated "hub column" field — callers find it via ``hub``."""
    row_order: dict[int, list[int]]
    """Node ids per column, ordered top to bottom."""
    hub: int
    """Id of the node chosen as the layout's structural hub."""


def compute_layout(nodes: list[GraphNode], edges: list[GraphEdge]) -> Layout:
    hub = _select_hub(nodes, edges)
    side = _partition_sides(nodes, edges, hub)
    column = _signed_columns(nodes, edges, hub, side)
    row_order = _order_columns(nodes, edges, column)
    return Layout(column=column, row_order=row_order, hub=hub)


def _select_hub(nodes: list[GraphNode], edges: list[GraphEdge]) -> int:
    degree: dict[int, int] = defaultdict(int)
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1
    return min(nodes, key=lambda n: (-degree[n.id], n.id)).id


def _adjacency(edges: list[GraphEdge]) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        adj[edge.source].append(edge.target)
        adj[edge.target].append(edge.source)
    return adj


def _partition_sides(
    nodes: list[GraphNode], edges: list[GraphEdge], hub: int
) -> dict[int, int]:
    """Assigns every non-hub node a side, ``+1`` or ``-1``."""
    other_ids = {n.id for n in nodes if n.id != hub}
    rest_edges = [e for e in edges if hub not in (e.source, e.target)]
    components = _connected_components(other_ids, rest_edges)
    if len(components) > 1:
        return _lpt_assign(components)
    return _bisect_component(other_ids, rest_edges, hub, edges)


def _connected_components(
    node_ids: set[int], edges: list[GraphEdge]
) -> list[list[int]]:
    adj = _adjacency(edges)
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in sorted(node_ids):
        if start in seen:
            continue
        component = []
        queue = deque([start])
        seen.add(start)
        while queue:
            nid = queue.popleft()
            component.append(nid)
            for neighbor in adj[nid]:
                if neighbor in node_ids and neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(sorted(component))
    return components


def _lpt_assign(components: list[list[int]]) -> dict[int, int]:
    """Greedy longest-processing-time: largest component first, each to the
    currently lighter side; ties broken by side +1 first for determinism."""
    ordered = sorted(components, key=lambda c: (-len(c), c[0]))
    side: dict[int, int] = {}
    weight = {1: 0, -1: 0}
    for component in ordered:
        chosen = 1 if weight[1] <= weight[-1] else -1
        weight[chosen] += len(component)
        for nid in component:
            side[nid] = chosen
    return side


def _bisect_component(
    other_ids: set[int], rest_edges: list[GraphEdge], hub: int, edges: list[GraphEdge]
) -> dict[int, int]:
    """Hub is not a cut vertex — ``other_ids`` is one connected component.
    Grows two sides from the hub's neighbors via alternating BFS (whichever
    side is smaller claims the next frontier node), then runs a bounded
    number of Kernighan-Lin-style pairwise swaps to reduce cross-side edges.
    Imperfection is acceptable: the router turns any remaining cross-side
    edges into perimeter detours, not cascading crossings."""
    adj = _adjacency(rest_edges)
    hub_neighbors = sorted(
        {
            e.target if e.source == hub else e.source
            for e in edges
            if hub in (e.source, e.target)
        }
        & other_ids
    )
    side = _grow_balanced(other_ids, adj, hub_neighbors)
    return _kl_swap(other_ids, rest_edges, side)


def _grow_balanced(
    other_ids: set[int], adj: dict[int, list[int]], seeds: list[int]
) -> dict[int, int]:
    side: dict[int, int] = {}
    frontiers: dict[int, deque[int]] = {1: deque(), -1: deque()}
    for i, seed in enumerate(seeds):
        s = 1 if i % 2 == 0 else -1
        if seed not in side:
            side[seed] = s
            frontiers[s].append(seed)

    while side.keys() < other_ids:
        s = 1 if sum(1 for v in side.values() if v == 1) <= len(side) / 2 else -1
        grown = False
        while frontiers[s]:
            nid = frontiers[s].popleft()
            for neighbor in adj[nid]:
                if neighbor in other_ids and neighbor not in side:
                    side[neighbor] = s
                    frontiers[s].append(neighbor)
                    grown = True
            if grown:
                break
        if not grown:
            # frontier exhausted without reaching a new node (disconnected
            # remainder within the "single component" — shouldn't happen,
            # but stay total and deterministic if it ever does)
            if remaining := sorted(other_ids - set(side)):
                nid = remaining[0]
                side[nid] = s
                frontiers[s].append(nid)
    return side


def _kl_swap(
    other_ids: set[int], rest_edges: list[GraphEdge], side: dict[int, int]
) -> dict[int, int]:
    edge_set: set[frozenset[int]] = {
        frozenset((e.source, e.target)) for e in rest_edges
    }
    adj = _adjacency(rest_edges)

    def external_internal(nid: int) -> tuple[int, int]:
        ext = sum(1 for n in adj[nid] if side[n] != side[nid])
        inter = sum(1 for n in adj[nid] if side[n] == side[nid])
        return ext, inter

    for _ in range(_KL_MAX_ROUNDS):
        d = {nid: (e - i) for nid in other_ids for e, i in [external_internal(nid)]}
        side_a = sorted(nid for nid in other_ids if side[nid] == 1)
        side_b = sorted(nid for nid in other_ids if side[nid] == -1)
        best_gain = 0
        best_pair: tuple[int, int] | None = None
        for u in side_a:
            for v in side_b:
                connected = frozenset((u, v)) in edge_set
                gain = d[u] + d[v] - (2 if connected else 0)
                if gain > best_gain:
                    best_gain = gain
                    best_pair = (u, v)
        if best_pair is None:
            break
        u, v = best_pair
        side[u], side[v] = side[v], side[u]
    return side


def _signed_columns(
    nodes: list[GraphNode], edges: list[GraphEdge], hub: int, side: dict[int, int]
) -> dict[int, int]:
    distance = _bfs_distance(nodes, edges, hub)
    column = {nid: (0 if nid == hub else side[nid] * distance[nid]) for nid in distance}
    _bump_same_column_edges(column, hub, edges)
    shift = -min(column.values())
    return {nid: c + shift for nid, c in column.items()}


def _bfs_distance(
    nodes: list[GraphNode], edges: list[GraphEdge], hub: int
) -> dict[int, int]:
    adj = _adjacency(edges)
    distance = {hub: 0}
    queue = deque([hub])
    while queue:
        nid = queue.popleft()
        for neighbor in adj[nid]:
            if neighbor not in distance:
                distance[neighbor] = distance[nid] + 1
                queue.append(neighbor)
    for node in nodes:
        distance.setdefault(node.id, 0)  # unreachable (shouldn't happen; stay total)
    return distance


def _bump_same_column_edges(
    column: dict[int, int], hub: int, edges: list[GraphEdge]
) -> None:
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if column[edge.source] != column[edge.target]:
                continue
            bump_id = edge.target if edge.target != hub else edge.source
            if bump_id == hub:
                continue  # both endpoints are the hub — impossible
            column[bump_id] += 1 if column[bump_id] >= 0 else -1
            changed = True


def _order_columns(
    nodes: list[GraphNode], edges: list[GraphEdge], column: dict[int, int]
) -> dict[int, list[int]]:
    by_column: dict[int, list[int]] = defaultdict(list)
    for node in sorted(nodes, key=lambda n: n.id):
        by_column[column[node.id]].append(node.id)

    neighbors: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        neighbors[edge.source].append(edge.target)
        neighbors[edge.target].append(edge.source)

    position: dict[int, int] = {}
    for ids in by_column.values():
        for i, nid in enumerate(ids):
            position[nid] = i

    def barycenter(node_id: int, direction: int) -> float:
        refs = [
            position[n]
            for n in neighbors[node_id]
            if (column[n] - column[node_id]) * direction > 0
        ]
        return sum(refs) / len(refs) if refs else position[node_id]

    if not by_column:
        return {}
    columns = sorted(by_column)
    for _ in range(2):
        for c in columns:
            by_column[c].sort(key=lambda n: (barycenter(n, -1), n))
            for i, nid in enumerate(by_column[c]):
                position[nid] = i
        for c in reversed(columns):
            by_column[c].sort(key=lambda n: (barycenter(n, 1), n))
            for i, nid in enumerate(by_column[c]):
                position[nid] = i

    return dict(by_column)
