"""Generic symbol → path resolution backing the ``explore.find`` method.

A client hovering a symbol in a query buffer knows the symbol's name, its kind,
and — from the surrounding syntax — which schema/table/label it must live under.
It does not know where any given driver keeps that kind of node in its object
tree. Each driver closes that gap by declaring, in ``BaseDriver.FIND_PATHS``,
the path templates at which each kind of node lives:

    FIND_PATHS = {NodeType.COLUMN: [["*", "*", "columns", "*"]]}

A ``"*"`` segment stands for one level of children (listed via ``explore_list``);
every other segment is a literal group name. This module walks those templates,
pruning each wildcard level by the caller's scopes, and returns the concrete
paths that can be handed straight back to ``explore.describe``.

Walking through ``explore_list`` — rather than querying each backend's catalog —
means results come from the connection's existing explore cache, so a repeated
hover costs no round trip and needs no cache of its own. A driver that can
resolve names in a single catalog query overrides ``explore_find`` instead.
"""

import logging
from collections.abc import Awaitable, Callable

from .drivers.base import DriverError
from .protocol import ExploreItem, NodeType, SearchScope

logger = logging.getLogger(__name__)

WILDCARD = "*"
"""Template segment standing for "any one child at this level"."""

MAX_LIST_CALLS = 200
"""Cap on explore_list calls per find. A search whose scopes leave an
intermediate level unconstrained fans out across every node at that level; the
cap turns a pathological case (e.g. an unqualified column over a database of
hundreds of tables, on a cold cache) into an error telling the client to narrow
its scope, rather than thousands of round trips."""

ListFn = Callable[[list[str]], Awaitable[list[ExploreItem]]]
"""Signature of the ``explore_list`` used to expand wildcard levels."""


async def walk_find(
    list_fn: ListFn,
    templates: dict[NodeType, list[list[str]]],
    node_type: str,
    name: str,
    scopes: list[SearchScope],
) -> list[list[str]]:
    """Resolve a symbol to the paths of every node matching it.

    Args:
        list_fn: Lists a path's children — normally the *caching* explore_list,
            so repeated searches are served from cache.
        templates: The driver's ``FIND_PATHS``.
        node_type: Kind of node to find, as a key of *templates*. A kind the
            driver does not declare yields no matches.
        name: Symbol name, matched case-insensitively (see :func:`_prefer_exact`).
        scopes: Ancestor restrictions; see :class:`~grannos.protocol.SearchScope`.

    Returns:
        Describe-paths of the matching nodes, in template then tree order,
        deduplicated. Empty when the symbol resolves to nothing; more than one
        entry when the symbol is ambiguous.

    Raises:
        DriverError: If the search would exceed :data:`MAX_LIST_CALLS`.
    """
    kind = _as_node_type(node_type)
    if kind is None:
        return []
    matches: list[list[str]] = []
    budget = _Budget(list_fn, MAX_LIST_CALLS)
    for template in templates.get(kind, []):
        await _walk(
            budget,
            template,
            0,
            [],
            _constraints(template, templates, scopes),
            name,
            matches,
        )
    return _dedupe(_prefer_exact(matches, name))


def _constraints(
    template: list[str],
    templates: dict[NodeType, list[list[str]]],
    scopes: list[SearchScope],
) -> dict[int, set[str]]:
    """Map each of *template*'s ancestor wildcard positions to the names allowed
    there, folded to lowercase.

    A scope constrains position ``i`` when the driver declares the scope's own
    type at exactly the template prefix ending at ``i`` — i.e. when a node of
    that type is what lives at that level. Several scopes of one type widen the
    same position (alternatives); scopes of different types land on different
    positions, so they compound. A scope matching no position is dropped: it
    names a level this template does not pass through.
    """
    allowed: dict[int, set[str]] = {}
    for i in range(len(template) - 1):  # the final position is the node itself
        if template[i] != WILDCARD:
            continue
        prefix = template[: i + 1]
        for scope in scopes:
            kind = _as_node_type(scope.type)
            if kind is not None and prefix in templates.get(kind, []):
                allowed.setdefault(i, set()).add(scope.name.casefold())
    return allowed


def _as_node_type(value: str) -> NodeType | None:
    """Coerce a client-supplied node kind to a :class:`NodeType`.

    Returns:
        None if *value* names no known kind — reported as "no match" (for a
        searched type) or ignored (for a scope) rather than raising, since a
        client naming a kind this server does not know is asking about
        something that, by definition, isn't in the tree.
    """
    try:
        return NodeType(value)
    except ValueError:
        logger.debug(f"explore.find: unknown node type {value!r}")
        return None


async def _walk(
    budget: "_Budget",
    template: list[str],
    i: int,
    prefix: list[str],
    allowed: dict[int, set[str]],
    name: str,
    matches: list[list[str]],
) -> None:
    """Expand *template* from position *i* onward, appending every match found
    below *prefix* to *matches*."""
    if i == len(template) - 1:
        matches += [
            [*prefix, item.name]
            for item in await budget.children(prefix)
            if item.type != NodeType.GROUP and item.name.casefold() == name.casefold()
        ]
        return

    if template[i] != WILDCARD:
        await _walk(
            budget, template, i + 1, [*prefix, template[i]], allowed, name, matches
        )
        return

    names = allowed.get(i)
    for item in await budget.children(prefix):
        if item.type == NodeType.GROUP:
            continue
        if names is not None and item.name.casefold() not in names:
            continue
        await _walk(
            budget, template, i + 1, [*prefix, item.name], allowed, name, matches
        )


class _Budget:
    """Wraps a list function, capping how many times a single find may call it."""

    def __init__(self, list_fn: ListFn, limit: int) -> None:
        self._list_fn = list_fn
        self._left = limit

    async def children(self, path: list[str]) -> list[ExploreItem]:
        if self._left <= 0:
            raise DriverError(
                "Too many nodes to search — narrow the search with a more specific scope"
            )
        self._left -= 1
        return await self._list_fn(path)


def _prefer_exact(matches: list[list[str]], name: str) -> list[list[str]]:
    """Drop case-insensitive matches when exact ones exist.

    Names are matched case-insensitively because a symbol as written in a query
    rarely matches the catalog's own casing (Oracle folds to upper, PostgreSQL
    to lower). Where both an exact and an inexact match exist, though, the exact
    one is what the author meant — reporting both would make an unambiguous
    symbol look ambiguous.
    """
    exact = [path for path in matches if path[-1] == name]
    return exact or matches


def _dedupe(paths: list[list[str]]) -> list[list[str]]:
    """Remove duplicate paths, preserving order — a node reachable from two of a
    type's templates is still one node."""
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for path in paths:
        if tuple(path) not in seen:
            seen.add(tuple(path))
            unique.append(path)
    return unique
