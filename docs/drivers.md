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

`explore.describe` is supported on tables and views and returns full column
metadata (name, type, nullability, primary key flag).

---

## SQL Server

**Install:** `pip install mssql-python`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | no | `localhost` | Server hostname or IP |
| `port` | no | `1433` | TCP port |
| `database` | no | — | Database name |
| `user` | no | — | Login name |
| `password` | no | — | Password (masked) |
| `applicationIntent` | no | — | `READ_WRITE` or `READ_ONLY` |

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
| `host` | no | `localhost` | Server hostname or IP |
| `port` | no | `1521` | Listener port |
| `service_name` | no | `FREEPDB1` | Database service name |
| `user` | no | — | Username |
| `password` | no | — | Password (masked) |

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
| `uri` | no | `bolt://localhost:7687` | Bolt URI |
| `user` | no | `neo4j` | Username |
| `password` | no | — | Password (masked) |
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

`explore.describe` always returns `None` (no fixed schema).

---

## MongoDB

**Install:** `pip install pymongo`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `uri` | no | `mongodb://localhost:27017` | Connection URI |
| `database` | yes | — | Default database |
| `username` | no | — | Username (can also be embedded in the URI) |
| `password` | no | — | Password (masked; can also be embedded in the URI) |

**Queries:** JSON command objects. The top-level key selects the operation and
its value names the collection. Add `"db": "<name>"` to target a database other
than the default.

```json
{"find": "orders", "filter": {"status": "open"}, "limit": 100}
```

```json
{"aggregate": "orders", "pipeline": [
  {"$group": {"_id": "$status", "total": {"$sum": "$amount"}}}
]}
```

```json
{"insert": "users", "documents": [{"name": "Alice", "age": 30}]}
```

```json
{"update": "users", "updates": [{"q": {"name": "Alice"}, "u": {"$set": {"age": 31}}}]}
```

```json
{"delete": "orders", "deletes": [{"q": {"status": "cancelled"}, "limit": 0}]}
```

`find` defaults to a limit of 1000 rows if `"limit"` is not specified. Results
are flattened with dot-notation column names (`address.city`, `address.zip`).

**Explore tree:**

```
(root)
└── <database>
    └── <collection>
        ├── fields   → top-level field names (sampled from up to 10 documents)
        └── indexes  → index names
```

`explore.describe` always returns `None` (no fixed schema).
