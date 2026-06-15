"""MongoDB driver — requires: pip install pymongo"""

import json
from typing import TYPE_CHECKING, Any

from ..protocol import (
    DMLResult,
    DriverParam,
    ExploreItem,
    ReadResult,
    TableDescription,
)
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError, DriverError

if TYPE_CHECKING:
    import pymongo

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


def _docs_to_result(docs: list[dict[str, Any]]) -> ReadResult:
    if not docs:
        return ReadResult(columns=[], rows=[], rows_total=0)
    serialized = [{k: _serialize(v) for k, v in doc.items()} for doc in docs]
    columns: list[str] = list(dict.fromkeys(k for doc in serialized for k in doc))
    rows = [[doc.get(col) for col in columns] for doc in serialized]
    return flatten_docs(columns, rows, rows_total=len(docs))


class MongoDriver(BaseDriver):
    """MongoDB driver backed by the pymongo async API.

    Args:
        params: Connect request fields (``uri``, ``database``).
        client: Open AsyncMongoClient. Use :meth:`create` instead of constructing directly.
    """

    PARAMS: list[DriverParam] = [
        DriverParam(key="uri", type="string", label="Connection URI", required=True),
        DriverParam(key="database", type="string", label="Database", required=True),
        DriverParam(key="username", type="string", label="Username", required=True),
        DriverParam(
            key="password", type="string", label="Password", required=True, secret=True
        ),
    ]

    HELP: str = """\
## MongoDB

**Install:** `pip install pymongo`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `uri` | yes | Connection URI |
| `database` | yes | Database |
| `username` | yes | Username (can also be embedded in the URI) |
| `password` | yes | Password (masked; can also be embedded in the URI) |

**Queries:** JSON command objects. The top-level key selects the operation and
its value names the collection. Add `"db": "<name>"` to target a database other
than the default.

```json
{"find": "users", "db": "auth"}
```

**Read:**

```json
{"find": "orders", "filter": {"status": "open"}, "sort": {"createdAt": -1}, "limit": 100}
```

`filter`, `sort`, `projection`, and `limit` are all optional. `find` defaults
to a limit of 1000 rows when `"limit"` is omitted.

```json
{"aggregate": "orders", "pipeline": [
  {"$group": {"_id": "$status", "total": {"$sum": "$amount"}}},
  {"$sort": {"total": -1}}
]}
```

**Write:**

```json
{"insertOne": "users", "document": {"name": "Alice", "age": 30}}
{"updateOne": "users", "filter": {"name": "Alice"}, "update": {"$set": {"age": 31}}}
{"deleteOne": "orders", "filter": {"status": "cancelled"}}
```

Results are flattened with dot-notation column names (`address.city`, `address.zip`).

**Explore tree:**

```
(root)
└── <database>
    └── <collection>
        ├── fields   → top-level field names (sampled from up to 10 documents)
        └── indexes  → index names
```

`explore.describe` always returns `None` (no fixed schema).
"""

    def __init__(
        self, params: dict[str, Any], client: "pymongo.AsyncMongoClient"
    ) -> None:
        super().__init__(params)
        self._client = client

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "MongoDriver":
        try:
            from pymongo import AsyncMongoClient
        except ImportError:
            raise RuntimeError("pymongo not installed — run: pip install pymongo")

        kwargs: dict[str, Any] = {}
        if params.get("username"):
            kwargs["username"] = params["username"]
        if params.get("password"):
            kwargs["password"] = params["password"]
        client = AsyncMongoClient(
            params.get("uri", "mongodb://localhost:27017"), **kwargs
        )
        try:
            # pymongo is lazy - force a connection to the db
            await client.admin.command("ping")
        except Exception as exc:
            await client.close()
            raise DriverError(str(exc)) from exc
        return cls(params, client)

    async def reconnect(self) -> None:
        await self._client.close()
        from pymongo import AsyncMongoClient

        kwargs: dict[str, Any] = {}
        if self.params.get("username"):
            kwargs["username"] = self.params["username"]
        if self.params.get("password"):
            kwargs["password"] = self.params["password"]
        self._client = AsyncMongoClient(
            self.params.get("uri", "mongodb://localhost:27017"), **kwargs
        )
        try:
            # pymongo is lazy - force a connection to the db
            await self._client.admin.command("ping")
        except Exception as exc:
            await self._client.close()
            raise DriverError(str(exc)) from exc

    async def disconnect(self) -> None:
        await self._client.close()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | DMLResult:
        """Run a MongoDB command expressed as a JSON string.

        Args:
            query: JSON object following MongoDB command syntax. The top-level key
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
            cmd: dict[str, Any] = json.loads(query)
        except json.JSONDecodeError as exc:
            raise DriverError(f"MongoDB command must be valid JSON: {exc}") from exc

        db = self._client[cmd.pop("db", self.params.get("database", "test"))]
        try:
            if "find" in cmd:
                return await self._find(db, cmd)
            if "aggregate" in cmd:
                return await self._aggregate(db, cmd)
            if "insertOne" in cmd:
                return await self._insert_one(db, cmd)
            if "insertMany" in cmd:
                return await self._insert_many(db, cmd)
            if "updateOne" in cmd:
                return await self._update_one(db, cmd)
            if "updateMany" in cmd:
                return await self._update_many(db, cmd)
            if "deleteOne" in cmd:
                return await self._delete_one(db, cmd)
            if "deleteMany" in cmd:
                return await self._delete_many(db, cmd)
            raise DriverError(f"Unsupported command keys: {list(cmd.keys())}")
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            if isinstance(exc, DriverError):
                raise
            raise DriverError(str(exc)) from exc

    async def _find(self, db: Any, cmd: dict[str, Any]) -> ReadResult:
        col = db[cmd.pop("find")]
        filter_ = cmd.pop("filter", {})
        projection = cmd.pop("projection", None)
        sort = cmd.pop("sort", None)
        limit = cmd.pop("limit", _DEFAULT_FIND_LIMIT)
        cursor = col.find(filter_, projection).limit(limit)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        return _docs_to_result(await cursor.to_list())

    async def _aggregate(self, db: Any, cmd: dict[str, Any]) -> ReadResult:
        col = db[cmd.pop("aggregate")]
        cursor = await col.aggregate(cmd.pop("pipeline", []))
        return _docs_to_result(await cursor.to_list())

    async def _insert_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("insertOne")]
        await col.insert_one(cmd.pop("document", {}))
        return DMLResult(rows_affected=1)

    async def _insert_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("insertMany")]
        docs = cmd.pop("documents", [])
        if not docs:
            return DMLResult(rows_affected=0)
        result = await col.insert_many(docs)
        return DMLResult(rows_affected=len(result.inserted_ids))

    async def _update_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("updateOne")]
        result = await col.update_one(cmd.pop("filter", {}), cmd.pop("update", {}))
        return DMLResult(rows_affected=result.modified_count)

    async def _update_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("updateMany")]
        result = await col.update_many(cmd.pop("filter", {}), cmd.pop("update", {}))
        return DMLResult(rows_affected=result.modified_count)

    async def _delete_one(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("deleteOne")]
        result = await col.delete_one(cmd.pop("filter", {}))
        return DMLResult(rows_affected=result.deleted_count)

    async def _delete_many(self, db: Any, cmd: dict[str, Any]) -> DMLResult:
        col = db[cmd.pop("deleteMany")]
        result = await col.delete_many(cmd.pop("filter", {}))
        return DMLResult(rows_affected=result.deleted_count)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                return [
                    ExploreItem(name=n, type="database", expandable=True)
                    for n in sorted(await self._client.list_database_names())
                ]
            case [db_name]:
                return [
                    ExploreItem(name=n, type="collection", expandable=True)
                    for n in sorted(await self._client[db_name].list_collection_names())
                ]
            case [_, _]:
                return [
                    ExploreItem(name="fields", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                ]
            case [db_name, collection_name, "fields"]:
                return [
                    ExploreItem(name=f, type="field", expandable=False)
                    for f in await self._sample_fields(db_name, collection_name)
                ]
            case [db_name, collection_name, "indexes"]:
                return [
                    ExploreItem(name=i, type="index", expandable=False)
                    for i in await self._list_indexes(db_name, collection_name)
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return None

    async def _sample_fields(self, db_name: str, collection_name: str) -> list[str]:
        cursor = await self._client[db_name][collection_name].aggregate(
            [{"$sample": {"size": 10}}]
        )
        docs = await cursor.to_list()
        seen: dict[str, None] = {}
        for doc in docs:
            for key in doc:
                seen[key] = None
        return list(seen)

    async def _list_indexes(self, db_name: str, collection_name: str) -> list[str]:
        return sorted(await self._client[db_name][collection_name].index_information())


def _maybe_raise_connection_lost(exc: Exception) -> None:
    try:
        from pymongo.errors import AutoReconnect, ConnectionFailure, NetworkTimeout

        if isinstance(exc, (AutoReconnect, ConnectionFailure, NetworkTimeout)):
            raise ConnectionLostError(str(exc)) from exc
    except ImportError:
        pass
