#!/usr/bin/env bash
# Start an Oracle Free container, run integration tests, then tear down.
# Requires: pip install oracledb
# Image requires Oracle Container Registry login:
#   docker login container-registry.oracle.com
set -euo pipefail

CONTAINER="oracle-dev"
SYS_PASSWORD="TestPassword1"
APP_USER="testuser"
APP_PASSWORD="testuser1"
APP_USER2="testuser2"
APP_PASSWORD2="testuser2pw"
PORT=1521
IMAGE="container-registry.oracle.com/database/free:latest"
SERVICE="FREEPDB1"
TIMEOUT=300  # Oracle typically needs 2-4 minutes to initialise

cleanup() {
    echo "Stopping container..."
    # docker rm -f "$CONTAINER" &>/dev/null || true
}
trap cleanup EXIT

echo "Starting Oracle container..."
docker run \
    -e "ORACLE_PWD=$SYS_PASSWORD" \
    -p "$PORT:1521" \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --replace \
    -d "$IMAGE"

echo "Waiting for Oracle to become healthy (timeout: ${TIMEOUT}s)..."
deadline=$(( $(date +%s) + TIMEOUT ))
until [ "$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER")" = "healthy" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "Oracle did not become ready within ${TIMEOUT}s." >&2
        exit 1
    fi
    sleep 5
done
echo "Oracle is ready."

echo "Creating test users..."
docker exec "$CONTAINER" bash -c "
sqlplus -S sys/$SYS_PASSWORD@//localhost:1521/$SERVICE as sysdba << 'EOF'
CREATE USER $APP_USER IDENTIFIED BY $APP_PASSWORD;
GRANT CONNECT, RESOURCE, UNLIMITED TABLESPACE, SELECT_CATALOG_ROLE TO $APP_USER;
GRANT CREATE SYNONYM, CREATE PUBLIC SYNONYM, DROP PUBLIC SYNONYM TO $APP_USER;
CREATE USER $APP_USER2 IDENTIFIED BY $APP_PASSWORD2;
GRANT CONNECT, CREATE ANY INDEX TO $APP_USER2;
EXIT;
EOF
"

ORACLE_HOST="localhost" \
ORACLE_PORT="$PORT" \
ORACLE_USER="$APP_USER" \
ORACLE_PASSWORD="$APP_PASSWORD" \
ORACLE_SERVICE="$SERVICE" \
ORACLE_USER2="$APP_USER2" \
ORACLE_PASSWORD2="$APP_PASSWORD2" \
    python -m pytest tests/external/test_oracle.py -v "$@"
