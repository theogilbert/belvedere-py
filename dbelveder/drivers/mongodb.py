"""MongoDB driver — requires: pip install pymongo"""

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..protocol import DMLResult, DriverParam, ExploreItem, SelectResult, TableDescription
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError

if TYPE_CHECKING:
    import pymongo

T = TypeVar("T")

_DEFAULT_FIND_LIMIT = 1000


def _serialize(value: Any) -> Any:
    """Recursively convert BSON types to plain Python values."""
    try:
        from bson import Decimal128, ObjectId
        if isinstance(value, ObjectId):
            return str(value)
        if isinstance(value, Decimal128):
            return str(value)
    except ImportError:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


def _docs_to_result(docs: list[dict[str, Any]]) -> SelectResult:
    if not docs:
        return SelectResult(columns=[], rows=[])
    serialized = [{k: _serialize(v) for k, v in doc.items()} for doc in docs]
    columns: list[str] = list(dict.fromkeys(k for doc in serialized for k in doc))
    rows = [[doc.get(col) for col in columns] for doc in serialized]
    return flatten_docs(columns, rows)


class MongoDriver(BaseDriver):
    """MongoDB driver backed by pymongo, run in a thread executor.

    Args:
        params: Connect request fields (``uri``, ``database``).
        client: Open MongoClient. Use :meth:`create` instead of constructing directly.
    """

    PARAMS: list[DriverParam] = [
        DriverParam(key="uri", type="string", label="Connection URI", default="mongodb://localhost:27017"),
        DriverParam(key="database", type="string", label="Default database", required=True),
        DriverParam(key="username", type="string", label="Username"),
        DriverParam(key="password", type="string", label="Password", secret=True),
    ]

    def __init__(self, params: dict[str, Any], client: "pymongo.MongoClient") -> None:
        super().__init__(params)
        self._client = client

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "MongoDriver":
        try:
            from pymongo import MongoClient
        except ImportError:
            raise RuntimeError("pymongo not installed — run: pip install pymongo")

        def _connect() -> "pymongo.MongoClient":
            kwargs: dict[str, Any] = {}
            if params.get("username"):
                kwargs["username"] = params["username"]
            if params.get("password"):
                kwargs["password"] = params["password"]
            client = MongoClient(params.get("uri", "mongodb://localhost:27017"), **kwargs)
            client.admin.command("ping")
            return client

        client = await asyncio.get_running_loop().run_in_executor(None, _connect)
        return cls(params, client)

    async def reconnect(self) -> None:
        await self._run(self._reconnect_sync)

    def _reconnect_sync(self) -> None:
        self._client.close()
        from pymongo import MongoClient
        kwargs: dict[str, Any] = {}
        if self.params.get("username"):
            kwargs["username"] = self.params["username"]
        if self.params.get("password"):
            kwargs["password"] = self.params["password"]
        self._client = MongoClient(self.params.get("uri", "mongodb://localhost:27017"), **kwargs)
        self._client.admin.command("ping")

    async def disconnect(self) -> None:
        await self._run(self._client.close)

    async def execute(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:
        """Run a MongoDB command expressed as a JSON string.

        Args:
            sql: JSON object following MongoDB command syntax. The top-level key
                selects the operation; its value is the collection name.
                Supported operations:

                - ``find``: ``{"find": "col", "filter": {}, "projection": {},
                  "sort": {}, "limit": N}``
                - ``aggregate``: ``{"aggregate": "col", "pipeline": [...]}``
                - ``insertOne``: ``{"insertOne": "col", "document": {...}}``
                - ``insertMany``: ``{"insertMany": "col", "documents": [...]}``
                - ``updateOne``: ``{"updateOne": "col", "filter": {}, "update": {}}``
                - ``updateMany``: ``{"updateMany": "col", "filter": {}, "update": {}}``
                - ``deleteOne``: ``{"deleteOne": "col", "filter": {}}``
                - ``deleteMany``: ``{"deleteMany": "col", "filter": {}}``

                Add ``"db": "name"`` to target a database other than the default.
            binds: Unused for MongoDB.
        """
        try:
            cmd: dict[str, Any] = json.loads(sql)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MongoDB command must be valid JSON: {exc}") from exc
        return await self._run(self._execute_sync, cmd)

    def _execute_sync(self, cmd: dict[str, Any]) -> SelectResult | DMLResult:
        db = self._client[cmd.pop("db", self.params.get("database", "test"))]
        try:
            if "find" in cmd:
                return self._find(db, cmd)
            if "aggregate" in cmd:
                return self._aggregate(db, cmd)
            if "insertOne" in cmd:
                return self._insert_one(db, cmd)
            if "insertMany" in cmd:
                return self._insert_many(db, cmd)
            if "updateOne" in cmd:
                return self._update_one(db, cmd)
            if "updateMany" in cmd:
                return self._update_many(db, cmd)
            if "deleteOne" in cmd:
                return self._delete_one(db, cmd)
            if "deleteMany" in cmd:
                return self._delete_many(db, cmd)
            raise ValueError(f"Unsupported command keys: {list(cmd.keys())}")
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            raise

    def _find(self, db: Any, cmd: dict[str, Any]) -> SelectResult:
        col = db[cmd.pop("find")]
        filter_ = cmd.pop("filter", {})
        projection = cmd.pop("projection", None)
        sort = cmd.pop("sort", None)
        limit = cmd.pop("limit", _DEFAULT_FIND_LIMIT)
        cursor = col.find(filter_, projection)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        return _docs_to_result(list(cursor.limit(limit)))

    def _aggregate(self, db: Any, cmd: dict[str, Any]) -> SelectResult:
        col = db[cmd.pop("aggregate")]
        return _docs_to_result(list(col.aggregate(cmd.pop("pipeline", []))))

    def _insert_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("insertOne")]
        doc = cmd.pop("document", {})
        col.insert_one(doc)
        return DMLResult(rows_affected=1)

    def _insert_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("insertMany")]
        docs = cmd.pop("documents", [])
        if not docs:
            return DMLResult(rows_affected=0)
        return DMLResult(rows_affected=len(col.insert_many(docs).inserted_ids))

    def _update_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("updateOne")]
        affected = col.update_one(cmd.pop("filter", {}), cmd.pop("update", {})).modified_count
        return DMLResult(rows_affected=affected)

    def _update_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("updateMany")]
        affected = col.update_many(cmd.pop("filter", {}), cmd.pop("update", {})).modified_count
        return DMLResult(rows_affected=affected)

    def _delete_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("deleteOne")]
        affected = col.delete_one(cmd.pop("filter", {})).deleted_count
        return DMLResult(rows_affected=affected)

    def _delete_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("deleteMany")]
        affected = col.delete_many(cmd.pop("filter", {})).deleted_count
        return DMLResult(rows_affected=affected)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                return [
                    ExploreItem(name=n, type="database", expandable=True)
                    for n in sorted(self._client.list_database_names())
                ]
            case [db_name]:
                return [
                    ExploreItem(name=n, type="collection", expandable=True)
                    for n in sorted(self._client[db_name].list_collection_names())
                ]
            case [_, _]:
                return [
                    ExploreItem(name="fields", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                ]
            case [db_name, collection_name, "fields"]:
                return [
                    ExploreItem(name=f, type="field", expandable=False)
                    for f in self._sample_fields(db_name, collection_name)
                ]
            case [db_name, collection_name, "indexes"]:
                return [
                    ExploreItem(name=i, type="index", expandable=False)
                    for i in self._list_indexes(db_name, collection_name)
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return None

    def _sample_fields(self, db_name: str, collection_name: str) -> list[str]:
        docs = list(self._client[db_name][collection_name].aggregate([{"$sample": {"size": 10}}]))
        seen: dict[str, None] = {}
        for doc in docs:
            for key in doc:
                seen[key] = None
        return list(seen)

    def _list_indexes(self, db_name: str, collection_name: str) -> list[str]:
        return sorted(self._client[db_name][collection_name].index_information())

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args))


def _maybe_raise_connection_lost(exc: Exception) -> None:
    try:
        from pymongo.errors import AutoReconnect, ConnectionFailure, NetworkTimeout
        if isinstance(exc, (AutoReconnect, ConnectionFailure, NetworkTimeout)):
            raise ConnectionLostError(str(exc)) from exc
    except ImportError:
        pass
