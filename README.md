# grannos-py

JSON-over-stdio server backend to query databases.

This server can be used by compatible clients (e.g. [grannos.nvim](https://github.com/theogilbert/grannos.nvim)) to explore and query databases in an IDE.

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv) (recommended)

## Installation

```bash
pip install grannos-py
# with all driver dependencies:
pip install "grannos-py[all]"
# or with a specific driver only, e.g. SQL Server:
pip install "grannos-py[mssql]"
```

## Usage

The server is started by the Neovim plugin automatically. To run it manually:

```bash
grannos [--log] [-v] [--max-concurrency N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log` | off | Log all requests and responses to `~/.local/state/grannos/server.log` |
| `-v` | off | Log at DEBUG level (requires `--log`) |
| `--max-concurrency N` | 5 | Max concurrent requests per connection |

## Supported drivers

| Driver | Dependency | Install |
|--------|------------|---------|
| `sqlite` | stdlib | — |
| `duckdb` | `duckdb` | `pip install "grannos-py[duckdb]"` |
| `sqlserver` | `mssql-python` | `pip install "grannos-py[mssql]"` |
| `neo4j` | `neo4j` | `pip install neo4j` |
| `oracle` | `oracledb` | `pip install oracledb` |
| `mongodb` | `pymongo` | `pip install pymongo` |
| `elasticsearch` | `elasticsearch` | `pip install elasticsearch` |

Only drivers whose package is installed are advertised via `capabilities`. See [docs/drivers.md](docs/drivers.md) for connection parameters, query syntax, and explore tree structure for each driver.

## Protocol

Communication uses newline-delimited JSON (one message per line). See [docs/protocol.md](https://github.com/theogilbert/grannos.nvim/blob/main/docs/protocol.md) for the full specification.

### Methods

- **`capabilities`** — list available drivers and their connection parameters
- **`driver.help`** — return Markdown help text for a named driver
- **`connect`** — open a database connection, returns a `connection_id`
- **`disconnect`** — close a connection
- **`execute`** — run a query against the connected database
  - Query with results → `{"columns": [...], "rows": [...], "rows_total": N}` (`rows_total` is the total number of matching rows; may exceed `len(rows)` when the driver applies a default fetch limit)
  - Write operation → `{"rows_affected": N}`

  Query syntax depends on the driver. Examples:

  ```sql
  -- SQLite / SQL Server / Oracle
  SELECT name, age FROM users WHERE active = 1
  ```

  ```cypher
  // Neo4j (Cypher)
  MATCH (u:User)-[:BOUGHT]->(p:Product) RETURN u.name, p.title
  ```

  ```
  // Elasticsearch (Lucene)
  orders | status:open AND total:>50
  ```

  ```json
  // MongoDB
  {"find": "orders", "filter": {"status": "open"}, "limit": 100}
  ```

- **`explore.list`** — list child nodes in the database object tree (schemas, tables, columns, …)
- **`explore.describe`** — return column metadata for a table

Long-running operations emit progress notifications before the final response.

## Server behaviour

**Idle timeout:** a connection is auto-closed after `idle_timeout` seconds of inactivity. Pass `"idle_timeout": <seconds>` in `connect.params` (default: `600`).

**Explore cache:** `explore.list` and `explore.describe` results are cached per connection and persisted to `~/.cache/grannos/`. Pass `"reset_cache": true` in any explore request to invalidate the cache for that connection. Passwords are never written to the cache.

## Development

```bash
uv sync --group dev
uv run pytest
```
