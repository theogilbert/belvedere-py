# Drivers

Each driver is loaded on demand. Only the packages required for the drivers you
actually use need to be installed.

---

## SQLite

**Install:** none (stdlib)

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `database` | yes | — | File path or `:memory:` |

**Queries:** Standard SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM users WHERE age > ?
```

**Explore tree:**

```
(root)
└── <table|view>
    ├── columns       → name, type
    ├── indices       → index name
    └── foreign_keys  → "col → ref_table.ref_col"
```

`explore.describe` is supported on:
- `[table|view]` — returns full column metadata (name, type, nullability, primary key flag)
- `[table, "indices", index_name]` — returns an `IndexDescription` with key fields (name + direction), `unique`, and `condition` (the SQL WHERE clause for partial indexes)

---

## DuckDB

**Install:** `pip install 'belvedere-py[duckdb]'`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `database` | no | `:memory:` | File path or `:memory:` |

**Queries:** Standard SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM read_parquet('/path/to/file.parquet')
SELECT * FROM read_csv('/path/to/file.csv', header = true)
SELECT * FROM 'glob/**/*.parquet'
```

**Explore tree:**

```
(root)
└── <schema>
    └── <table|view>
        ├── columns       → name, type
        ├── indices       → index name
        └── foreign_keys  → "col → ref_table.ref_col"
```

`explore.describe` is supported on:
- `[schema, table]` — returns full column metadata (name, type, nullability, primary key flag)
- `[schema, table, "indices", index_name]` — returns an `IndexDescription` with key fields (name + direction), `unique`, `entity` (table name), and `condition` (the SQL WHERE clause for partial indexes)

---

## SQL Server

**Install:** `pip install mssql-python`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | yes | — | Server hostname or IP |
| `port` | yes | `1433` | TCP port |
| `database` | yes | — | Database name |
| `user` | yes | — | Login name |
| `password` | yes | — | Password (masked) |
| `applicationIntent` | yes | — | `READ_WRITE` or `READ_ONLY` |

**Queries:** Standard T-SQL. Positional bind parameters use `?` placeholders.

```sql
SELECT * FROM dbo.orders WHERE status = ?
```

**Explore tree:**

```
(root)
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indices      → name, type (e.g. CLUSTERED)
        └── constraints  → name, type (e.g. primary_key, foreign_key)
```

System schemas (`sys`, `INFORMATION_SCHEMA`, `guest`, `db_*`) are hidden.

`explore.describe` is supported on `[schema, table]` paths and returns full
column metadata (name, type, nullability, default).

---

## Oracle

**Install:** `pip install oracledb` — thin mode, no Oracle Instant Client required.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | yes | — | Server hostname or IP |
| `port` | yes | `1521` | Listener port |
| `service_name` | yes | — | Database service name |
| `user` | yes | — | Username |
| `password` | yes | — | Password (masked) |

**Queries:** Standard SQL. Positional bind parameters use `:1`, `:2`, … placeholders.

```sql
SELECT * FROM employees WHERE department_id = :1 AND hire_date > :2
```

**Explore tree:**

```
(root)  ← non-system schemas (ALL_USERS where ORACLE_MAINTAINED = 'N')
└── <schema>
    └── <table|view>
        ├── columns      → name, data type
        ├── indexes      → name, index type
        └── constraints  → name, type (primary_key, unique, check, foreign_key)
```

`explore.describe` is supported on `[schema, table]` paths and returns full
column metadata (name, type, nullability, primary key flag, default).

---

## Neo4j

**Install:** `pip install neo4j`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `uri` | yes | `bolt://localhost:7687` | Bolt URI |
| `user` | yes | `neo4j` | Username |
| `password` | yes | — | Password (masked) |
| `database` | no | `neo4j` | Database name |

**Queries:** Cypher. Positional bind parameters are referenced as `$0`, `$1`, …

```cypher
MATCH (u:User {name: $0})-[:BOUGHT]->(p:Product) RETURN u, p
```

Results are serialized and flattened: nodes expand to `col._labels`, `col.prop`,
…; relationships expand to `col._type`, `col.prop`, …

**Explore tree:**

```
(root)
├── entities       → <label>  → property names (sampled from existing nodes)
├── relationships  → <type>   → property names (sampled from existing relationships)
└── indexes        → index name
```

`explore.describe` is supported on `["indexes", index_name]` paths and returns an
`IndexDescription` with the indexed properties, `unique`, and `entity` (the node label or
relationship type the index operates on). The `direction` field on each `IndexKeyField`
holds the Neo4j index type (`RANGE`, `TEXT`, `POINT`, …).

---

## Elasticsearch

**Install:** `pip install elasticsearch aiohttp`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | yes | — | Server hostname or IP |
| `port` | yes | `9200` | HTTP port |
| `username` | no | — | Username |
| `password` | no | — | Password (masked) |
| `query_mode` | yes | `lucene` | Query language: `lucene` or `dev_tools` |

**Queries:** Prefix with the target index name (pattern or alias) and ` | `.

*Lucene mode:*

```
orders | status:open AND total:>50
```

```
orders | *
```

*Dev Tools mode (Kibana Dev Tools syntax):*

```
GET /orders/_search
{"query": {"match": {"status": "open"}}}
```

```
GET /orders,products/_search
{"query": {"match_all": {}}, "sort": [{"total": "desc"}]}
```

Any Elasticsearch REST endpoint is accepted — the response is returned as a
flat table. Search responses unpack `hits.hits`; all other responses are
flattened as a single row.

**Explore tree:**

```
(root)
└── <index>
    ├── mappings  → field name and type
    └── aliases   → alias names
```

System indices (names starting with `.`) are hidden.

`explore.describe` is supported on `[index]` paths and returns field metadata
from the index mapping (name, type).

---

## MongoDB

**Install:** `pip install pymongo`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `uri` | yes | Connection URI (embed credentials and `authSource` here if needed) |
| `username` | no | Username (can also be embedded in the URI) |
| `password` | no | Password (masked; can also be embedded in the URI) |

**Queries:** MongoDB Extended JSON command objects. `"db"` is required and
names the target database. The top-level operation key names the collection.

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

**Insert:**

```json
{"insertOne": "users", "db": "mydb", "document": {"name": "Alice", "age": 30}}
```

```json
{"insertMany": "users", "db": "mydb", "documents": [{"name": "Alice"}, {"name": "Bob"}]}
```

**Update:**

```json
{"updateOne": "users", "db": "mydb", "filter": {"name": "Alice"}, "update": {"$set": {"age": 31}}}
```

```json
{"updateMany": "users", "db": "mydb", "filter": {"role": "guest"}, "update": {"$set": {"active": false}}}
```

**Delete:**

```json
{"deleteOne": "orders", "db": "mydb", "filter": {"status": "cancelled"}}
```

```json
{"deleteMany": "orders", "db": "mydb", "filter": {"status": "cancelled"}}
```

Document values support Extended JSON, so BSON types that plain JSON can't
express — dates, ObjectIds, decimals — can be written directly:

```json
{"updateOne": "events", "db": "mydb",
 "filter": {"_id": {"$oid": "5f8d0d55b54764421b7156c0"}},
 "update": {"$set": {"occurredAt": {"$date": "2024-01-01T00:00:00Z"}}}}
```

**Collections and indexes:**

```json
{"createCollection": "events", "db": "mydb"}
{"dropCollection": "old_events", "db": "mydb"}
{"createIndex": "users", "db": "mydb", "keys": {"email": 1}, "options": {"unique": true}}
{"dropIndex": "users", "db": "mydb", "name": "email_1"}
```

`options` is optional for both `createCollection` and `createIndex` and is
passed through to the underlying pymongo call.

Results are flattened with dot-notation column names (`address.city`,
`address.zip`).

**Explore tree:**

```
(root)
└── <database>
    └── <collection>
        ├── fields   → top-level field names (sampled from up to 10 documents)
        └── indexes  → index names
```

`explore.describe` is supported on `[database, collection, "indexes", index_name]` paths
and returns an `IndexDescription` with key fields (name + direction), `unique`, and `condition`
(the `partialFilterExpression` serialized as JSON, if set).
