#!/usr/bin/env bash
# Start an Elasticsearch container, run integration tests, then tear down.
# Requires: pip install elasticsearch
set -euo pipefail

CONTAINER="elasticsearch-dev"
HOST="localhost"
PORT=9200
IMAGE="docker.elastic.co/elasticsearch/elasticsearch:8.17.0"
TIMEOUT=60

cleanup() {
    echo "Stopping container..."
    docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting Elasticsearch container..."
docker run \
    -e "discovery.type=single-node" \
    -e "xpack.security.enabled=false" \
    -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
    -p "$PORT:9200" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for Elasticsearch to accept connections (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until curl -sf "http://$HOST:$PORT/_cluster/health" &>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "Elasticsearch did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "Elasticsearch is ready."

ELASTICSEARCH_HOST="$HOST" \
ELASTICSEARCH_PORT="$PORT" \
ELASTICSEARCH_PROTOCOL="http" \
    python -m pytest tests/external/test_elasticsearch.py -v "$@"
