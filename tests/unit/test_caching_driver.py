import pathlib
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from grannos.drivers.base import FindNotSupported
from grannos.explore_cache import CachingDriver, ConnectionCache
from grannos.protocol import (
    EntityDescription,
    ExploreItem,
    FieldDescription,
    IndexDescription,
    IndexKeyField,
    NodeType,
    RawDocument,
    SearchScope,
    TableReference,
)

PARAMS = {"driver": "sqlite", "database": ":memory:"}


def _entity_desc() -> EntityDescription:
    idx = IndexDescription(
        name="i1", fields=[IndexKeyField(name="ID", direction="asc")], unique=True
    )
    return EntityDescription(
        name="t",
        kind="table",
        properties=[
            FieldDescription(
                name="ID", types=["NUMBER"], pk=True, exclusive_indices=[idx]
            ),
            FieldDescription(
                name="VAL",
                types=["VARCHAR2"],
                sample=["a", "b"],
                outgoing_references=[
                    TableReference(
                        table="t", column="VAL", ref_table="other", ref_column="ID"
                    )
                ],
            ),
        ],
    )


@pytest.fixture
def cache(tmp_path: pathlib.Path) -> ConnectionCache:
    return ConnectionCache(PARAMS, tmp_path / "c.json")


@pytest.fixture
def inner() -> AsyncMock:
    d = AsyncMock()
    d.FIND_PATHS = {}
    d.explore_list.return_value = [ExploreItem(name="t", type="table", expandable=True)]
    d.explore_describe.return_value = EntityDescription(
        name="t", kind="table", properties=[]
    )
    return d


@pytest.fixture
def driver(inner: AsyncMock, cache: ConnectionCache) -> CachingDriver:
    return CachingDriver(inner, cache)


class TestExploreListCaching:
    async def test_miss_calls_inner_and_caches(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        items = await driver.explore_list([])
        assert items == inner.explore_list.return_value
        inner.explore_list.assert_awaited_once_with([])

    async def test_hit_returns_cached_without_calling_inner(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_list([])
        await driver.explore_list([])
        inner.explore_list.assert_awaited_once()

    async def test_different_paths_cached_independently(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="a", type="schema", expandable=True)],
            [ExploreItem(name="b", type="table", expandable=False)],
        ]
        result_a = await driver.explore_list(["a"])
        result_b = await driver.explore_list(["b"])
        assert result_a[0].name == "a"
        assert result_b[0].name == "b"
        assert inner.explore_list.await_count == 2
        await driver.explore_list(["a"])
        await driver.explore_list(["b"])
        assert inner.explore_list.await_count == 2


class TestExploreDescribeCaching:
    async def test_miss_calls_inner_and_caches(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        desc = await driver.explore_describe(["s", "t"])
        assert desc == inner.explore_describe.return_value
        inner.explore_describe.assert_awaited_once_with(["s", "t"])

    async def test_hit_returns_cached_without_calling_inner(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_describe(["s", "t"])
        await driver.explore_describe(["s", "t"])
        inner.explore_describe.assert_awaited_once()

    async def test_none_result_not_cached(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = None
        await driver.explore_describe(["s", "t"])
        await driver.explore_describe(["s", "t"])
        assert inner.explore_describe.await_count == 2

    async def test_index_description_cached(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = IndexDescription(
            name="idx", fields=[], unique=False
        )
        await driver.explore_describe(["s", "t", "indices", "idx"])
        result = await driver.explore_describe(["s", "t", "indices", "idx"])
        assert isinstance(result, IndexDescription)
        inner.explore_describe.assert_awaited_once()


class TestEntityFanOut:
    async def test_entity_result_populates_per_field_entries(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _entity_desc()
        await driver.explore_describe(["s", "t"])
        field = await driver.explore_describe(["s", "t", "columns", "ID"])
        assert field == _entity_desc().properties[0]
        inner.explore_describe.assert_awaited_once()

    async def test_all_fields_served_from_fan_out(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _entity_desc()
        await driver.explore_describe(["s", "t"])
        val = await driver.explore_describe(["s", "t", "columns", "VAL"])
        assert val == _entity_desc().properties[1]
        inner.explore_describe.assert_awaited_once()

    async def test_relationship_also_served_from_fan_out(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _entity_desc()
        await driver.explore_describe(["s", "t"])
        ref = await driver.explore_describe(["s", "t", "relationships", "VAL"])
        assert ref == _entity_desc().properties[1].outgoing_references[0]
        inner.explore_describe.assert_awaited_once()

    async def test_reset_clears_fanned_out_entries(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_describe.return_value = _entity_desc()
        await driver.explore_describe(["s", "t"])
        driver.reset_cache(["s", "t"])
        await driver.explore_describe(["s", "t", "columns", "ID"])
        assert inner.explore_describe.await_count == 2

    async def test_resetting_a_field_also_evicts_the_parent_entity(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        # The parent entity's cached copy embeds this field's data (including
        # sample values) — resetting just the field must not leave the parent
        # holding a stale copy of it.
        inner.explore_describe.return_value = _entity_desc()
        await driver.explore_describe(["s", "t"])
        driver.reset_cache(["s", "t", "columns", "ID"])
        await driver.explore_describe(["s", "t"])
        assert inner.explore_describe.await_count == 2


class TestDescribeDiskRoundTrip:
    def test_entity_description_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        ConnectionCache(PARAMS, path).set_describe(["s", "t"], _entity_desc())
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t"]) == _entity_desc()

    def test_field_description_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        field = _entity_desc().properties[0]
        ConnectionCache(PARAMS, path).set_describe(["s", "t", "columns", "ID"], field)
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t", "columns", "ID"]) == field

    def test_field_entry_does_not_poison_other_entries(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = tmp_path / "c.json"
        cache = ConnectionCache(PARAMS, path)
        other = EntityDescription(name="other", kind="table", properties=[])
        cache.set_describe(["s", "other"], other)
        cache.set_describe(["s", "t"], _entity_desc())
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "other"]) == other

    def test_non_json_sample_values_persist(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        field = FieldDescription(
            name="TS",
            types=["DATE"],
            sample=[datetime(2024, 1, 1), Decimal("1.5")],
        )
        ConnectionCache(PARAMS, path).set_describe(["s", "t", "columns", "TS"], field)
        reloaded = ConnectionCache(PARAMS, path).get_describe(
            ["s", "t", "columns", "TS"]
        )
        assert isinstance(reloaded, FieldDescription)
        assert reloaded.sample == ["2024-01-01T00:00:00", 1.5]

    def test_table_comment_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        desc = EntityDescription(
            name="t", kind="table", properties=[], comment="a comment"
        )
        ConnectionCache(PARAMS, path).set_describe(["s", "t"], desc)
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["s", "t"]) == desc

    def test_raw_document_round_trips(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "c.json"
        doc = RawDocument(filetype="yaml", content="global:\n  scrape_interval: 15s\n")
        ConnectionCache(PARAMS, path).set_describe(["configuration"], doc)
        reloaded = ConnectionCache(PARAMS, path)
        assert reloaded.get_describe(["configuration"]) == doc


class TestResetCache:
    async def test_reset_clears_list_cache(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_list([])
        driver.reset_cache([])
        await driver.explore_list([])
        assert inner.explore_list.await_count == 2

    async def test_reset_clears_describe_cache(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.explore_describe(["s", "t"])
        driver.reset_cache([])
        await driver.explore_describe(["s", "t"])
        assert inner.explore_describe.await_count == 2

    async def test_reset_with_path_clears_subtree(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="a", type="schema", expandable=True)],
            [ExploreItem(name="b", type="table", expandable=False)],
            [ExploreItem(name="a2", type="schema", expandable=True)],
            [ExploreItem(name="b2", type="table", expandable=False)],
        ]
        await driver.explore_list(["s"])
        await driver.explore_list(["s", "t"])
        driver.reset_cache(["s"])
        # both ["s"] and ["s", "t"] should be re-fetched
        await driver.explore_list(["s"])
        await driver.explore_list(["s", "t"])
        assert inner.explore_list.await_count == 4

    async def test_reset_with_path_leaves_siblings_intact(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_list.side_effect = [
            [ExploreItem(name="s", type="schema", expandable=True)],
            [ExploreItem(name="other", type="schema", expandable=True)],
        ]
        await driver.explore_list(["s"])
        await driver.explore_list(["other"])
        driver.reset_cache(["s"])
        # ["other"] was not under ["s"], so it should still be cached
        await driver.explore_list(["other"])
        assert inner.explore_list.await_count == 2


class TestDelegation:
    async def test_execute_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.execute("SELECT 1", [])
        inner.execute.assert_awaited_once_with("SELECT 1", [])

    async def test_reconnect_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.reconnect()
        inner.reconnect.assert_awaited_once()

    async def test_disconnect_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.disconnect()
        inner.disconnect.assert_awaited_once()

    async def test_set_session_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        await driver.set_session({"query_mode": "range"})
        inner.set_session.assert_awaited_once_with({"query_mode": "range"})

    def test_get_session_delegates(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.get_session = MagicMock(return_value={"query_mode": "range"})
        assert driver.get_session() == {"query_mode": "range"}


class TestExploreFind:
    """The walker has to run on the CachingDriver rather than on the driver it
    wraps, so its wildcard levels expand through the cached explore_list."""

    @pytest.fixture
    def searchable(self, inner: AsyncMock) -> AsyncMock:
        inner.FIND_PATHS = {NodeType.TABLE: [["*"]]}
        inner.explore_find.side_effect = FindNotSupported
        return inner

    async def test_falls_back_to_the_walker(
        self, driver: CachingDriver, searchable: AsyncMock
    ) -> None:
        assert await driver.explore_find(NodeType.TABLE, "t", []) == [["t"]]

    async def test_walk_is_served_from_cache_on_a_repeated_search(
        self, driver: CachingDriver, searchable: AsyncMock
    ) -> None:
        await driver.explore_find(NodeType.TABLE, "t", [])
        await driver.explore_find(NodeType.TABLE, "t", [])
        searchable.explore_list.assert_awaited_once()

    async def test_a_tree_already_browsed_costs_no_round_trip(
        self, driver: CachingDriver, searchable: AsyncMock
    ) -> None:
        await driver.explore_list([])
        searchable.explore_list.reset_mock()
        assert await driver.explore_find(NodeType.TABLE, "t", []) == [["t"]]
        searchable.explore_list.assert_not_awaited()

    async def test_driver_implementation_takes_precedence(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        inner.explore_find.return_value = [["public", "users"]]
        scopes = [SearchScope(name="public", type=NodeType.SCHEMA)]
        assert await driver.explore_find(NodeType.TABLE, "users", scopes) == [
            ["public", "users"]
        ]
        inner.explore_find.assert_awaited_once_with(NodeType.TABLE, "users", scopes)
        inner.explore_list.assert_not_awaited()

    async def test_cache_is_preferred_over_the_driver_implementation(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        """A symbol resolvable from the cache must not cost a catalog query,
        however cheap that query is — this is the whole point of pass 1."""
        inner.FIND_PATHS = {NodeType.TABLE: [["*"]]}
        inner.explore_find.return_value = [["from-the-database"]]
        await driver.explore_list([])

        assert await driver.explore_find(NodeType.TABLE, "t", []) == [["t"]]
        inner.explore_find.assert_not_awaited()

    async def test_cache_miss_falls_through_to_the_driver_implementation(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        """The cached root does not settle a search for a *column*: the level
        below it was never listed, so the walk cannot conclude "no match"."""
        inner.FIND_PATHS = {
            NodeType.TABLE: [["*"]],
            NodeType.COLUMN: [["*", "columns", "*"]],
        }
        inner.explore_find.return_value = [["t", "columns", "id"]]
        await driver.explore_list([])

        assert await driver.explore_find(NodeType.COLUMN, "id", []) == [
            ["t", "columns", "id"]
        ]
        inner.explore_find.assert_awaited_once()

    async def test_a_complete_cache_only_walk_trusts_an_empty_result(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        """No match, with every level the walk touched cached, is an answer —
        not a reason to go to the database."""
        inner.FIND_PATHS = {NodeType.TABLE: [["*"]]}
        await driver.explore_list([])

        assert await driver.explore_find(NodeType.TABLE, "absent", []) == []
        inner.explore_find.assert_not_awaited()

    async def test_undeclared_node_type_skips_the_cache_only_walk(
        self, driver: CachingDriver, inner: AsyncMock
    ) -> None:
        """A type with no template makes the walk return empty without reading
        the cache at all — which must not be mistaken for "no such symbol" when
        the driver's own lookup covers that type."""
        inner.FIND_PATHS = {NodeType.TABLE: [["*"]]}
        inner.explore_find.return_value = [["entities", "Person"]]
        await driver.explore_list([])

        assert await driver.explore_find(NodeType.LABEL, "Person", []) == [
            ["entities", "Person"]
        ]
        inner.explore_find.assert_awaited_once()

    async def test_cache_only_walk_survives_a_restart(
        self, inner: AsyncMock, cache: ConnectionCache, tmp_path: pathlib.Path
    ) -> None:
        """Pass 1 reads the on-disk cache, so a symbol resolved in one session
        still costs no round trip in the next."""
        inner.FIND_PATHS = {NodeType.TABLE: [["*"]]}
        await CachingDriver(inner, cache).explore_list([])

        reopened = CachingDriver(inner, ConnectionCache(PARAMS, tmp_path / "c.json"))
        inner.explore_list.reset_mock()
        assert await reopened.explore_find(NodeType.TABLE, "t", []) == [["t"]]
        inner.explore_list.assert_not_awaited()
        inner.explore_find.assert_not_awaited()
