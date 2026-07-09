"""Unit tests for MongoDriver — no live database required."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pymongo.errors
import pytest

from belvedere.drivers.base import ConnectionLostError, DriverError, DriverSettings
from belvedere.drivers.mongodb import MongoDriver, _make_mongo_client, _serialize
from belvedere.protocol import LobPlaceholder, WriteResult

_CLOSED_EXC = pymongo.errors.InvalidOperation("Cannot use AsyncMongoClient after close")


def _closed_client() -> MagicMock:
    """A client mock raising InvalidOperation, as pymongo does once closed by the idle timer."""
    cursor = MagicMock()
    cursor.to_list = AsyncMock(side_effect=_CLOSED_EXC)
    col = MagicMock()
    col.find.return_value.limit.return_value = cursor
    col.insert_one = AsyncMock(side_effect=_CLOSED_EXC)
    col.index_information = AsyncMock(side_effect=_CLOSED_EXC)
    db = MagicMock()
    db.__getitem__.return_value = col
    client = MagicMock()
    client.__getitem__.return_value = db
    client.list_database_names = AsyncMock(side_effect=_CLOSED_EXC)
    return client


def _open_client() -> tuple[MagicMock, MagicMock, MagicMock]:
    """A client mock with a stubbed db/collection chain for happy-path execute() calls."""
    col = MagicMock()
    col.create_index = AsyncMock()
    col.drop_index = AsyncMock()
    col.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    db = MagicMock()
    db.__getitem__.return_value = col
    db.create_collection = AsyncMock()
    db.drop_collection = AsyncMock()
    client = MagicMock()
    client.__getitem__.return_value = db
    return client, db, col


def _make_driver(client: MagicMock) -> MongoDriver:
    return MongoDriver({}, client, DriverSettings())


class TestConnectionLostAfterIdleClose:
    async def test_execute_raises_connection_lost(self) -> None:
        driver = _make_driver(_closed_client())
        with pytest.raises(ConnectionLostError):
            await driver.execute(
                '{"insertOne": "users", "db": "test", "document": {}}', []
            )

    async def test_explore_list_raises_connection_lost(self) -> None:
        driver = _make_driver(_closed_client())
        with pytest.raises(ConnectionLostError):
            await driver.explore_list([])

    async def test_explore_describe_raises_connection_lost(self) -> None:
        driver = _make_driver(_closed_client())
        with pytest.raises(ConnectionLostError):
            await driver.explore_describe(["db", "col", "indexes"])

    async def test_explore_preview_raises_connection_lost(self) -> None:
        driver = _make_driver(_closed_client())
        with pytest.raises(ConnectionLostError):
            await driver.explore_preview(["db", "col"])


class TestMakeMongoClientInvalidUri:
    async def test_invalid_uri_raises_driver_error(self) -> None:
        with pytest.raises(DriverError):
            await _make_mongo_client({"uri": "mongodb://"})

    async def test_invalid_uri_message_is_preserved(self) -> None:
        with pytest.raises(DriverError, match="at least one hostname"):
            await _make_mongo_client({"uri": "mongodb://"})

    async def test_malformed_host_raises_driver_error(self) -> None:
        # pymongo raises a plain ValueError (not InvalidURI) for unescaped
        # reserved characters in the host portion of the URI.
        with pytest.raises(DriverError, match="Reserved characters"):
            await _make_mongo_client({"uri": "mongodb://localhost:270:1213:"})


class TestExecuteCollectionAndIndexOps:
    async def test_create_collection(self) -> None:
        client, db, _ = _open_client()
        driver = _make_driver(client)
        result = await driver.execute(
            '{"createCollection": "events", "db": "mydb", "options": {"capped": true}}',
            [],
        )
        db.create_collection.assert_awaited_once_with("events", capped=True)
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_drop_collection(self) -> None:
        client, db, _ = _open_client()
        driver = _make_driver(client)
        result = await driver.execute(
            '{"dropCollection": "old_events", "db": "mydb"}', []
        )
        db.drop_collection.assert_awaited_once_with("old_events")
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_create_index(self) -> None:
        client, _, col = _open_client()
        driver = _make_driver(client)
        result = await driver.execute(
            '{"createIndex": "users", "db": "mydb", "keys": {"email": 1}, '
            '"options": {"unique": true}}',
            [],
        )
        col.create_index.assert_awaited_once_with([("email", 1)], unique=True)
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1

    async def test_drop_index(self) -> None:
        client, _, col = _open_client()
        driver = _make_driver(client)
        result = await driver.execute(
            '{"dropIndex": "users", "db": "mydb", "name": "email_1"}', []
        )
        col.drop_index.assert_awaited_once_with("email_1")
        assert isinstance(result, WriteResult)
        assert result.rows_affected == 1


class TestExecuteExtendedJson:
    async def test_update_one_parses_date_literal(self) -> None:
        client, _, col = _open_client()
        driver = _make_driver(client)
        await driver.execute(
            '{"updateOne": "events", "db": "mydb", "filter": {}, '
            '"update": {"$set": {"occurredAt": {"$date": "2024-01-01T00:00:00Z"}}}}',
            [],
        )
        _, update = col.update_one.call_args.args
        occurred_at = update["$set"]["occurredAt"]
        assert isinstance(occurred_at, datetime)
        assert occurred_at.year == 2024


class TestSerialize:
    def test_passes_through_plain_values(self) -> None:
        assert _serialize("hello") == "hello"
        assert _serialize(42) == 42
        assert _serialize(None) is None

    def test_renders_binary_as_byte_count(self) -> None:
        assert _serialize(b"\x01\x02\x03") == LobPlaceholder(
            text="BSON Binary (3 bytes)"
        )

    def test_renders_binary_nested_in_dict(self) -> None:
        result = _serialize({"blob": b"\x00\x01"})
        assert result == {"blob": LobPlaceholder(text="BSON Binary (2 bytes)")}


class TestExecuteMalformedCommand:
    async def test_invalid_json_raises_driver_error(self) -> None:
        driver = _make_driver(_open_client()[0])
        with pytest.raises(DriverError, match="must be valid JSON"):
            await driver.execute("{not json", [])

    async def test_invalid_oid_raises_driver_error(self) -> None:
        driver = _make_driver(_open_client()[0])
        with pytest.raises(DriverError):
            await driver.execute(
                '{"find": "users", "db": "mydb", "filter": {"_id": {"$oid": "bad"}}}',
                [],
            )
