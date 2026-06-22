"""MongoDB driver — requires: pip install pymongo"""

import json
from enum import StrEnum
from typing import Any

import pymongo
import pymongo.errors

from ..protocol import (
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    ParamType,
    ReadResult,
    WriteResult,
)
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError, DriverError

_DEFAULT_FIND_LIMIT = 1000


class _Op(StrEnum):
    FIND = "find"
    AGGREGATE = "aggregate"
    INSERT_ONE = "insertOne"
    INSERT_MANY = "insertMany"
    UPDATE_ONE = "updateOne"
    UPDATE_MANY = "updateMany"
    DELETE_ONE = "deleteOne"
    DELETE_MANY = "deleteMany"


class MongoDriver(BaseDriver):
    """MongoDB driver backed by the pymongo async API.

    Args:
        params: Connect request fields (``uri``).
        client: Open AsyncMongoClient. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "MongoDB"

    PARAMS: list[DriverParam] = [
        DriverParam(
            key="uri",
            type=ParamType.STRING,
            label="Connection URI",
            default="mongodb://localhost:27017",
        ),
        DriverParam(
            key="username", type=ParamType.STRING, label="Username", required=False
        ),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            required=False,
            secret=True,
        ),
    ]

    HELP: str = """\
## MongoDB

**Install:** `pip install pymongo`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `uri` | yes | Connection URI (embed credentials and `authSource` here if needed) |
| `username` | no | Username (can also be embedded in the URI) |
| `password` | no | Password (masked; can also be embedded in the URI) |

**Queries:** JSON command objects. `"db"` is required and names the target
database. The top-level operation key names the collection.

```json
{"find": "users", "db": "auth"}
```

**Read:**

```json
{"find": "orders", "db": "mydb", "filter": {"status": "open"}, "sort": {"createdAt": -1}, "limit": 100}
```

`filter`, `sort`, `projection`, and `limit` are all optional. `find` defaults
to a limit of 1000 rows when `"limit"` is omitted.

```json
{"aggregate": "orders", "db": "mydb", "pipeline": [
  {"$group": {"_id": "$status", "total": {"$sum": "$amount"}}},
  {"$sort": {"total": -1}}
]}
```

**Write:**

```json
{"insertOne": "users", "db": "mydb", "document": {"name": "Alice", "age": 30}}
{"updateOne": "users", "db": "mydb", "filter": {"name": "Alice"}, "update": {"$set": {"age": 31}}}
{"deleteOne": "orders", "db": "mydb", "filter": {"status": "cancelled"}}
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

`explore.describe` is supported on `[database, collection, "indexes", index_name]` paths
and returns the index key fields with their sort direction (`asc` / `desc`).
"""

    def __init__(
        self, params: dict[str, Any], client: pymongo.AsyncMongoClient
    ) -> None:
        super().__init__(params)
        self._client = client

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "MongoDriver":
        return cls(params, await _make_mongo_client(params))

    async def reconnect(self) -> None:
        await self._client.close()
        self._client = await _make_mongo_client(self.params)

    async def disconnect(self) -> None:
        await self._client.close()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
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

                ``"db"`` is required and names the target database.
            binds: Unused for MongoDB.
        """
        try:
            cmd: dict[str, Any] = json.loads(query)
        except json.JSONDecodeError as exc:
            raise DriverError(f"MongoDB command must be valid JSON: {exc}") from exc

        if "db" not in cmd:
            raise DriverError(
                'MongoDB command must include a "db" key specifying the target database'
            )
        db = self._client[cmd.pop("db")]
        try:
            if _Op.FIND in cmd:
                return await self._find(db, cmd)
            if _Op.AGGREGATE in cmd:
                return await self._aggregate(db, cmd)
            if _Op.INSERT_ONE in cmd:
                return await self._insert_one(db, cmd)
            if _Op.INSERT_MANY in cmd:
                return await self._insert_many(db, cmd)
            if _Op.UPDATE_ONE in cmd:
                return await self._update_one(db, cmd)
            if _Op.UPDATE_MANY in cmd:
                return await self._update_many(db, cmd)
            if _Op.DELETE_ONE in cmd:
                return await self._delete_one(db, cmd)
            if _Op.DELETE_MANY in cmd:
                return await self._delete_many(db, cmd)
            raise DriverError(f"Unsupported command keys: {list(cmd.keys())}")
        except Exception as exc:
            _maybe_raise_connection_lost(exc)
            if isinstance(exc, DriverError):
                raise
            raise DriverError(str(exc)) from exc

    async def _find(self, db: Any, cmd: dict[str, Any]) -> ReadResult:
        col = db[cmd.pop(_Op.FIND)]
        filter_ = cmd.pop("filter", {})
        projection = cmd.pop("projection", None)
        sort = cmd.pop("sort", None)
        limit = cmd.pop("limit", _DEFAULT_FIND_LIMIT)
        cursor = col.find(filter_, projection).limit(limit)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        return _docs_to_result(await cursor.to_list())

    async def _aggregate(self, db: Any, cmd: dict[str, Any]) -> ReadResult:
        col = db[cmd.pop(_Op.AGGREGATE)]
        cursor = await col.aggregate(cmd.pop("pipeline", []))
        return _docs_to_result(await cursor.to_list())

    async def _insert_one(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.INSERT_ONE)]
        await col.insert_one(cmd.pop("document", {}))
        return WriteResult(rows_affected=1)

    async def _insert_many(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.INSERT_MANY)]
        docs = cmd.pop("documents", [])
        if not docs:
            return WriteResult(rows_affected=0)
        result = await col.insert_many(docs)
        return WriteResult(rows_affected=len(result.inserted_ids))

    async def _update_one(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.UPDATE_ONE)]
        result = await col.update_one(cmd.pop("filter", {}), cmd.pop("update", {}))
        return WriteResult(rows_affected=result.modified_count)

    async def _update_many(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.UPDATE_MANY)]
        result = await col.update_many(cmd.pop("filter", {}), cmd.pop("update", {}))
        return WriteResult(rows_affected=result.modified_count)

    async def _delete_one(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.DELETE_ONE)]
        result = await col.delete_one(cmd.pop("filter", {}))
        return WriteResult(rows_affected=result.deleted_count)

    async def _delete_many(self, db: Any, cmd: dict[str, Any]) -> WriteResult:
        col = db[cmd.pop(_Op.DELETE_MANY)]
        result = await col.delete_many(cmd.pop("filter", {}))
        return WriteResult(rows_affected=result.deleted_count)

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

    async def explore_describe(self, path: list[str]) -> IndexDescription | None:
        match path:
            case [db_name, collection_name, "indexes", index_name]:
                info = await self._client[db_name][collection_name].index_information()
                spec = info.get(index_name)
                if spec is None:
                    return None
                partial = spec.get("partialFilterExpression")
                return IndexDescription(
                    index=index_name,
                    fields=[
                        IndexKeyField(name=field, direction=_index_direction(direction))
                        for field, direction in spec.get("key", [])
                    ],
                    unique=bool(spec.get("unique", False)),
                    condition=json.dumps(partial, separators=(",", ":"))
                    if partial is not None
                    else None,
                )
            case _:
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


def _index_direction(direction: Any) -> str:
    if direction == 1:
        return "asc"
    if direction == -1:
        return "desc"
    return str(direction)


def _docs_to_result(docs: list[dict[str, Any]]) -> ReadResult:
    if not docs:
        return ReadResult(columns=[], rows=[], rows_total=0)
    serialized = [{k: _serialize(v) for k, v in doc.items()} for doc in docs]
    # dict.fromkeys deduplicates while preserving first-seen order (set would not)
    columns: list[str] = list(dict.fromkeys(k for doc in serialized for k in doc))
    rows = [[doc.get(col) for col in columns] for doc in serialized]
    return flatten_docs(columns, rows, rows_total=len(docs))


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


async def _make_mongo_client(params: dict[str, Any]) -> pymongo.AsyncMongoClient:
    kwargs: dict[str, Any] = {}
    if params.get("username"):
        kwargs["username"] = params["username"]
    if params.get("password"):
        kwargs["password"] = params["password"]
    client = pymongo.AsyncMongoClient(params["uri"], **kwargs)
    try:
        # pymongo is lazy — force a connection to verify credentials
        await client.admin.command("ping")
    except Exception as exc:
        await client.close()
        raise DriverError(str(exc)) from exc
    return client


def _maybe_raise_connection_lost(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            pymongo.errors.AutoReconnect,
            pymongo.errors.ConnectionFailure,
            pymongo.errors.NetworkTimeout,
        ),
    ):
        raise ConnectionLostError(str(exc)) from exc
