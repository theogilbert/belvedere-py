#!/usr/bin/env bash
# Start a Neo4j container, run integration tests, then tear down.
set -euo pipefail

CONTAINER="neo4j-dev"
PASSWORD="test-Password"
BOLT_PORT=7687
HTTP_PORT=7474
IMAGE="neo4j:5"
TIMEOUT=60

cleanup() {
    echo "Stopping container..."
    docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting Neo4j container..."
docker run \
    -e "NEO4J_AUTH=neo4j/$PASSWORD" \
    -p "$BOLT_PORT:7687" \
    -p "$HTTP_PORT:7474" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for Neo4j to accept connections (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until docker exec "$CONTAINER" \
        cypher-shell -u neo4j -p "$PASSWORD" "RETURN 1" &>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "Neo4j did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "Neo4j is ready."

NEO4J_PASSWORD="$PASSWORD" \
NEO4J_USER="neo4j" \
NEO4J_URI="bolt://localhost:$BOLT_PORT" \
    python -m pytest tests/external/test_neo4j.py -v "$@"
