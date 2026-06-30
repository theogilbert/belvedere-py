"""Unit tests for MongoDriver — no live database required."""

from unittest.mock import AsyncMock, MagicMock

import pymongo.errors
import pytest

from belvedere.drivers.base import ConnectionLostError, DriverSettings
from belvedere.drivers.mongodb import MongoDriver

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
