"""Graph discovery: BFS from the source table, building a deduplicated graph of
nodes and edges instead of a tree. Every reachable table gets exactly one node,
even if it can't be expanded further (depth cap) or resolved (describe failure).
"""

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ..protocol import ColumnInfo, DescribeResult, TableDescription, TableReference

FkSide = Literal["source", "target"]
"""Which endpoint of a ``GraphEdge`` owns the FK column — the "many" side of
the relationship; the other endpoint is the referenced ("one") side."""

Describe = Callable[[list[str]], Awaitable[DescribeResult]]
"""Fetches the description for a path, as ``Dispatcher._handle_explore_diagram`` does
via ``conn.driver.explore_describe`` (with reconnect-and-retry)."""

MAX_DEPTH = 20
"""BFS levels expanded before a table is drawn as a leaf box without recursing
further into its own references. Bounds worst-case graph size."""


class DiagramError(Exception):
    """Raised when the given path does not resolve to a table."""


@dataclass
class GraphNode:
    id: int
    name: str
    """Display name, e.g. ``dbo.orders`` or ``orders``."""
    path: list[str]
    """Path identifying this table."""
    rank: int
    """BFS distance from the source table; determines the node's layout column."""
    columns: list[ColumnInfo] = field(default_factory=list)
    fk_columns: set[str] = field(default_factory=set)
    """Names of columns covered by an outgoing foreign key."""
    ref_columns: set[str] = field(default_factory=set)
    """Names of columns covered by an incoming foreign key (referenced by another table)."""
    unavailable: bool = False
    """True if ``describe`` failed to resolve this table; drawn as a placeholder box."""


@dataclass
class GraphEdge:
    source: int
    target: int
    fk_side: FkSide = "source"
    """Which of ``source``/``target`` owns the FK column."""
    one_to_one: bool = False
    """Whether the FK column is itself constrained unique, making this a
    one-to-one relationship rather than many-to-one."""
    fk_column: str = ""
    """Name of the FK column on whichever endpoint owns it (``source`` or
    ``target``, per ``fk_side``) — the column segment of its
    ``["relationships", fk_column]`` describe path."""


async def discover(
    path: list[str], describe: Describe
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Fetch the table at ``path`` and everything connected to it, as a
    deduplicated graph — each table gets exactly one node no matter how many
    relationships point to it.

    Raises:
        DiagramError: If path does not resolve to a table.
    """
    desc = await describe(path)
    if not isinstance(desc, TableDescription):
        raise DiagramError(f"Path {path!r} does not resolve to a table")

    nodes = [_table_node(0, path, desc, rank=0)]
    edges: list[GraphEdge] = []
    seen_pairs: set[frozenset[int]] = set()
    visited: dict[tuple[str, ...], int] = {tuple(path): 0}
    queue: deque[tuple[list[str], TableDescription, int, int]] = deque(
        [(path, desc, 0, 0)]
    )

    while queue:
        cur_path, cur_desc, cur_id, depth = queue.popleft()
        for ref, fk_side in _iter_refs(cur_desc):
            ref_path = _ref_path(cur_desc, ref)
            if tuple(ref_path) == tuple(cur_path):
                continue  # self-reference — already fully described by this box

            target_id = visited.get(tuple(ref_path))
            if target_id is None:
                target_id = len(nodes)
                visited[tuple(ref_path)] = target_id
                child_desc = await describe(ref_path)
                rank = depth + 1
                if isinstance(child_desc, TableDescription):
                    nodes.append(_table_node(target_id, ref_path, child_desc, rank))
                    if rank < MAX_DEPTH:
                        queue.append((ref_path, child_desc, target_id, rank))
                else:
                    nodes.append(_placeholder_node(target_id, ref_path, ref, rank))

            pair = frozenset((cur_id, target_id))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                fk_column = ref.column if fk_side == "source" else ref.ref_column
                edges.append(
                    GraphEdge(
                        cur_id,
                        target_id,
                        fk_side=fk_side,
                        one_to_one=ref.unique,
                        fk_column=fk_column,
                    )
                )

    return nodes, edges


def _iter_refs(desc: TableDescription):
    """Yields every reference alongside which side ``desc``'s own table sits
    on: it owns the FK column for its ``outgoing_references``, and is the
    referenced ("one") side for its ``incoming_references``."""
    for ref in desc.outgoing_references:
        yield ref, "source"
    for ref in desc.incoming_references:
        yield ref, "target"


def _ref_path(desc: TableDescription, ref: TableReference) -> list[str]:
    if desc.schema is None:
        return [ref.table]
    return [ref.schema or desc.schema, ref.table]


def _table_node(
    id_: int, path: list[str], desc: TableDescription, rank: int
) -> GraphNode:
    name = f"{desc.schema}.{desc.table}" if desc.schema else desc.table
    return GraphNode(
        id=id_,
        name=name,
        path=path,
        rank=rank,
        columns=desc.columns,
        fk_columns={r.column for r in desc.outgoing_references},
        ref_columns={r.column for r in desc.incoming_references},
    )


def _placeholder_node(
    id_: int, path: list[str], ref: TableReference, rank: int
) -> GraphNode:
    name = f"{ref.schema}.{ref.table}" if ref.schema else ref.table
    return GraphNode(id=id_, name=name, path=path, rank=rank, unavailable=True)
