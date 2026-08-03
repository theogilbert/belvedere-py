import pytest

from grannos.drivers.base import DriverError
from grannos.explore_find import ListFn, walk_find
from grannos.protocol import ExploreItem, NodeType, SearchScope

# A PostgreSQL-shaped tree: two schemas, an "id" column in three different
# tables, and a "users" table present in both schemas — so the same symbol
# resolves ambiguously unless scopes narrow it.
PG_PATHS = {
    NodeType.SCHEMA: [["*"]],
    NodeType.TABLE: [["*", "*"]],
    NodeType.VIEW: [["*", "*"]],
    NodeType.COLUMN: [["*", "*", "columns", "*"]],
    NodeType.INDEX: [["*", "*", "indexes", "*"]],
}

PG_TREE: dict[tuple[str, ...], list[tuple[str, str]]] = {
    (): [("public", "schema"), ("app", "schema")],
    ("public",): [("users", "table"), ("orders", "table"), ("user_stats", "view")],
    ("app",): [("users", "table")],
    ("public", "users"): [("columns", "group"), ("indexes", "group")],
    ("public", "orders"): [("columns", "group"), ("indexes", "group")],
    ("app", "users"): [("columns", "group"), ("indexes", "group")],
    ("public", "users", "columns"): [("id", "int4"), ("name", "text")],
    ("public", "orders", "columns"): [("id", "int4"), ("user_id", "int4")],
    ("app", "users", "columns"): [("id", "int4"), ("token", "text")],
    ("public", "users", "indexes"): [("users_pkey", "btree")],
}

# A Neo4j-shaped tree: literal group segments at the root, and a property that
# lives under two different templates.
NEO4J_PATHS = {
    NodeType.LABEL: [["entities", "*"]],
    NodeType.RELATIONSHIP_TYPE: [["relationships", "*"]],
    NodeType.PROPERTY: [
        ["entities", "*", "properties", "*"],
        ["relationships", "*", "properties", "*"],
    ],
}

NEO4J_TREE: dict[tuple[str, ...], list[tuple[str, str]]] = {
    (): [("entities", "group"), ("relationships", "group"), ("indexes", "group")],
    ("entities",): [("Person", "label"), ("Movie", "label")],
    ("relationships",): [("ACTED_IN", "relationship_type")],
    ("entities", "Person"): [("properties", "group")],
    ("entities", "Person", "properties"): [("name", "property")],
    ("entities", "Movie"): [("properties", "group")],
    ("entities", "Movie", "properties"): [("title", "property")],
    ("relationships", "ACTED_IN"): [("properties", "group")],
    ("relationships", "ACTED_IN", "properties"): [("roles", "property")],
}


def _lister(
    tree: dict[tuple[str, ...], list[tuple[str, str]]],
    calls: list[list[str]] | None = None,
) -> ListFn:
    """Build a list function serving *tree*, optionally recording every call."""

    async def list_fn(path: list[str]) -> list[ExploreItem]:
        if calls is not None:
            calls.append(list(path))
        return [
            ExploreItem(name=name, type=type_, expandable=True)
            for name, type_ in tree.get(tuple(path), [])
        ]

    return list_fn


def scope(name: str, type_: NodeType) -> SearchScope:
    return SearchScope(name=name, type=type_)


async def find(
    node_type: NodeType,
    name: str,
    scopes: list[SearchScope] | None = None,
    tree: dict[tuple[str, ...], list[tuple[str, str]]] = PG_TREE,
    paths: dict[NodeType, list[list[str]]] = PG_PATHS,
    list_fn: ListFn | None = None,
) -> list[list[str]]:
    return await walk_find(
        list_fn or _lister(tree), paths, node_type, name, scopes or []
    )


async def test_finds_column_under_full_scope():
    result = await find(
        NodeType.COLUMN,
        "name",
        [scope("public", NodeType.SCHEMA), scope("users", NodeType.TABLE)],
    )
    assert result == [["public", "users", "columns", "name"]]


async def test_unscoped_column_returns_every_candidate():
    """No scope means the client could not infer one — every match is reported
    so it can warn that the symbol is ambiguous."""
    result = await find(NodeType.COLUMN, "id")
    assert result == [
        ["public", "users", "columns", "id"],
        ["public", "orders", "columns", "id"],
        ["app", "users", "columns", "id"],
    ]


async def test_scopes_of_one_type_are_alternatives():
    """Two table scopes — an unqualified column over a join — widen one level."""
    result = await find(
        NodeType.COLUMN,
        "id",
        [
            scope("users", NodeType.TABLE),
            scope("orders", NodeType.TABLE),
            scope("public", NodeType.SCHEMA),
        ],
    )
    assert result == [
        ["public", "users", "columns", "id"],
        ["public", "orders", "columns", "id"],
    ]


async def test_scopes_of_different_types_compound():
    """The schema scope rules out app.users, which the table scope alone allows."""
    assert await find(NodeType.COLUMN, "id", [scope("users", NodeType.TABLE)]) == [
        ["public", "users", "columns", "id"],
        ["app", "users", "columns", "id"],
    ]
    assert await find(
        NodeType.COLUMN,
        "id",
        [scope("users", NodeType.TABLE), scope("app", NodeType.SCHEMA)],
    ) == [["app", "users", "columns", "id"]]


async def test_scope_naming_an_unknown_type_is_ignored():
    """A client infers scopes from source text and cannot know each driver's
    vocabulary — an unusable scope must not silently rule out every match."""
    result = await find(
        NodeType.COLUMN,
        "name",
        [scope("public", NodeType.SCHEMA), scope("Person", NodeType.LABEL)],
    )
    assert result == [["public", "users", "columns", "name"]]


async def test_unknown_node_type_finds_nothing():
    assert await walk_find(_lister(PG_TREE), PG_PATHS, "banana", "id", []) == []


async def test_node_type_absent_from_driver_finds_nothing():
    """Elasticsearch has no schemas; asking for one is not an error."""
    assert await find(NodeType.DATABASE, "public") == []


async def test_finds_table_and_view_at_the_same_level():
    """Nothing in the tree distinguishes a table from a view at that level, and
    a client reading a FROM clause cannot either."""
    assert await find(NodeType.TABLE, "user_stats") == [["public", "user_stats"]]
    assert await find(NodeType.VIEW, "user_stats") == [["public", "user_stats"]]


async def test_matches_name_case_insensitively():
    """Oracle folds identifiers to upper case, PostgreSQL to lower — a symbol as
    written in a query rarely matches the catalog's own casing."""
    result = await find(NodeType.COLUMN, "NAME", [scope("PUBLIC", NodeType.SCHEMA)])
    assert result == [["public", "users", "columns", "name"]]


async def test_exact_match_wins_over_case_insensitive_one():
    tree = {
        (): [("public", "schema")],
        ("public",): [("Users", "table"), ("users", "table")],
        ("public", "Users"): [("columns", "group")],
        ("public", "users"): [("columns", "group")],
        ("public", "Users", "columns"): [("id", "int4")],
        ("public", "users", "columns"): [("id", "int4")],
    }
    assert await find(NodeType.TABLE, "users", tree=tree) == [["public", "users"]]


async def test_group_nodes_are_never_matched_or_descended():
    """A group is an organisational node, not a database object — reachable only
    via a literal template segment naming it."""
    assert await find(NodeType.TABLE, "columns") == []
    assert await find(NodeType.INDEX, "users_pkey") == [
        ["public", "users", "indexes", "users_pkey"]
    ]


async def test_literal_segments_and_multiple_templates():
    assert await find(
        NodeType.PROPERTY, "roles", tree=NEO4J_TREE, paths=NEO4J_PATHS
    ) == [["relationships", "ACTED_IN", "properties", "roles"]]
    assert await find(NodeType.LABEL, "Person", tree=NEO4J_TREE, paths=NEO4J_PATHS) == [
        ["entities", "Person"]
    ]


async def test_scope_prunes_a_literal_segment_template():
    result = await find(
        NodeType.PROPERTY,
        "name",
        [scope("Person", NodeType.LABEL)],
        tree=NEO4J_TREE,
        paths=NEO4J_PATHS,
    )
    assert result == [["entities", "Person", "properties", "name"]]


async def test_duplicate_paths_are_collapsed():
    """One node reachable from two of a type's templates is still one node."""
    paths = {NodeType.LABEL: [["entities", "*"], ["entities", "*"]]}
    result = await find(NodeType.LABEL, "Person", tree=NEO4J_TREE, paths=paths)
    assert result == [["entities", "Person"]]


async def test_scope_prunes_the_walk_rather_than_filtering_afterwards():
    """A scoped level must not be listed and then discarded — pruning is what
    keeps a hover from fanning out across the whole database."""
    calls: list[list[str]] = []
    await find(
        NodeType.COLUMN,
        "name",
        [scope("public", NodeType.SCHEMA), scope("users", NodeType.TABLE)],
        list_fn=_lister(PG_TREE, calls),
    )
    assert calls == [[], ["public"], ["public", "users", "columns"]]
    assert ["app"] not in calls


async def test_too_broad_a_search_errors_rather_than_fanning_out(monkeypatch):
    monkeypatch.setattr("grannos.explore_find.MAX_LIST_CALLS", 2)
    with pytest.raises(DriverError, match="narrow the search"):
        await find(NodeType.COLUMN, "id")


async def test_budget_is_shared_across_a_types_templates(monkeypatch):
    """Both of a type's templates draw on one budget, so a two-template search
    cannot quietly cost twice the cap."""
    # Walking the entities template alone costs exactly 3 calls: the label
    # level, then each label's properties.
    monkeypatch.setattr("grannos.explore_find.MAX_LIST_CALLS", 3)
    with pytest.raises(DriverError, match="narrow the search"):
        await find(NodeType.PROPERTY, "roles", tree=NEO4J_TREE, paths=NEO4J_PATHS)
