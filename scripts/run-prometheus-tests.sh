#!/usr/bin/env bash
# Start a Prometheus container, run integration tests, then tear down.
# Requires: pip install aiohttp
set -euo pipefail

CONTAINER="prometheus-dev"
PORT=9090
IMAGE="docker.io/prom/prometheus:latest"
TIMEOUT=60

cleanup() {
    echo "Stopping container..."
    # docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting Prometheus container..."
# The image's default prometheus.yml scrapes itself (job "prometheus"), which
# is enough real data for the driver's queries and explore tree.
docker run \
    -p "$PORT:9090" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for Prometheus to accept connections (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until curl -sf "http://localhost:$PORT/-/ready" &>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "Prometheus did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "Prometheus is ready."

echo "Waiting for the self-scrape to produce data (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until [ "$(curl -sf "http://localhost:$PORT/api/v1/query?query=up" | jq -e '.data.result | length > 0' 2>/dev/null)" = "true" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "Prometheus did not produce scrape data within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 2
done
echo "Prometheus has data."

PROMETHEUS_URL="http://localhost:$PORT" \
    python -m pytest tests/external/test_prometheus.py -v "$@"
