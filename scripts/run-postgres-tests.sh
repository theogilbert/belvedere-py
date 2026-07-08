#!/usr/bin/env bash
# Start a PostgreSQL container, run integration tests, then tear down.
set -euo pipefail

CONTAINER="postgres-dev"
APP_USER="testuser"
APP_PASSWORD="testuser1"
APP_DATABASE="testdb"
PORT=5432
IMAGE="postgres:17"
TIMEOUT=60

cleanup() {
    echo "Stopping container..."
    docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting PostgreSQL container..."
docker run \
    -e "POSTGRES_USER=$APP_USER" \
    -e "POSTGRES_PASSWORD=$APP_PASSWORD" \
    -e "POSTGRES_DB=$APP_DATABASE" \
    -p "$PORT:5432" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for PostgreSQL to accept connections (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until docker exec "$CONTAINER" pg_isready -U "$APP_USER" -d "$APP_DATABASE" &>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "PostgreSQL did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "PostgreSQL is ready."

POSTGRES_HOST="localhost" \
POSTGRES_PORT="$PORT" \
POSTGRES_USER="$APP_USER" \
POSTGRES_PASSWORD="$APP_PASSWORD" \
POSTGRES_DATABASE="$APP_DATABASE" \
    python -m pytest tests/external/test_postgres.py -v "$@"
