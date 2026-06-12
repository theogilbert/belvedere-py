# dbelveder-py

JSON-over-stdio server backend for [dbelveder.nvim](../dbelveder.nvim). The Neovim plugin spawns this process and communicates through its stdin/stdout pipes using newline-delimited JSON.

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv) (recommended)

## Installation

```bash
pip install dbelveder-py
# or with SQL Server support:
pip install "dbelveder-py[mssql]"
```

## Usage

The server is started by the Neovim plugin automatically. To run it manually:

```bash
dbelveder [--log] [--max-concurrency N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log` | off | Log all requests and responses to `~/.local/state/dbelveder/server.log` |
| `--max-concurrency N` | 5 | Max concurrent requests per connection |

## Supported drivers

| Driver | Dependency | Install |
|--------|------------|---------|
| `sqlite` | stdlib | — |
| `sqlserver` | `mssql-python` | `pip install "dbelveder-py[mssql]"` |
| `neo4j` | `neo4j` | `pip install neo4j` |
| `oracle` | `oracledb` | `pip install oracledb` |
| `mongodb` | `pymongo` | `pip install pymongo` |

Only drivers whose package is installed are advertised via `capabilities`. See [docs/drivers.md](docs/drivers.md) for connection parameters, query syntax, and explore tree structure for each driver.

## Protocol

Communication uses newline-delimited JSON (one message per line). See [docs/protocol.md](../dbelveder.nvim/docs/protocol.md) for the full specification.

### Methods

- **`connect`** — open a database connection, returns a `connection_id`
- **`disconnect`** — close a connection
- **`execute`** — run a SQL statement
  - SELECT → `{"columns": [...], "rows": [...]}`
  - INSERT/UPDATE/DELETE → `{"rows_affected": N}`
- **`explore.list`** — list child nodes in the database object tree (schemas, tables, columns, …)
- **`explore.describe`** — return column metadata for a table

Long-running operations emit progress notifications before the final response.

## Server behaviour

**Idle timeout:** a connection is auto-closed after `idle_timeout` seconds of inactivity. Pass `"idle_timeout": <seconds>` in `connect.params` (default: `600`).

**Explore cache:** `explore.list` and `explore.describe` results are cached per connection and persisted to `~/.cache/dbelveder/`. Pass `"reset_cache": true` in any explore request to invalidate the cache for that connection. Passwords are never written to the cache.

## Development

```bash
uv sync --group dev
uv run pytest
```
