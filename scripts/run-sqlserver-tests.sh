#!/usr/bin/env bash
# Start a SQL Server container, run integration tests, then tear down.
set -euo pipefail

CONTAINER="sqlserver-dev"
PASSWORD="test-Password"
PORT=1433
IMAGE="mcr.microsoft.com/mssql/server:2022-latest"
SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
TIMEOUT=60

cleanup() {
    echo "Stopping container..."
    docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting SQL Server container..."
docker run \
    -e "ACCEPT_EULA=Y" \
    -e "MSSQL_SA_PASSWORD=$PASSWORD" \
    -p "$PORT:1433" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for SQL Server to accept connections (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until docker exec "$CONTAINER" "$SQLCMD" -S "localhost,$PORT" -U sa -P "$PASSWORD" \
        -No -Q "SELECT 1" &>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "SQL Server did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "SQL Server is ready."

MSSQL_PASSWORD="$PASSWORD" \
MSSQL_USER="sa" \
MSSQL_HOST="localhost" \
MSSQL_PORT="$PORT" \
    python -m pytest tests/integration/test_sqlserver.py -v "$@"
