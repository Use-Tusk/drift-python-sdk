#!/bin/bash

set -e

APP_PORT=${1:-5000}
export APP_PORT
PROJECT_NAME="flask-http-${APP_PORT}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting Flask HTTP E2E test run on port ${APP_PORT}..."

echo "Step 1: Cleaning up traces and logs..."
rm -rf "$SCRIPT_DIR/.tusk/traces" "$SCRIPT_DIR/.tusk/logs" 2>/dev/null || true

echo "Step 2: Building and starting docker container..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" build --no-cache
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" up -d --wait

echo ""
echo "Container logs (RECORD mode):"
echo "---"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" logs app
echo "---"
echo ""

echo "Step 3: Running tests using tusk CLI..."
EXIT_CODE=0

if docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app which tusk >/dev/null 2>&1; then
    TEST_RESULTS=$(docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app tusk run --print --output-format "json" --enable-service-logs 2>&1) || EXIT_CODE=$?
    echo ""
    echo "Test Results:"
    echo "$TEST_RESULTS"
else
    echo "  !!!tusk CLI not found!!!"
fi

echo ""
echo "Step 4: Cleaning up docker containers..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" down

echo "Step 5: Cleaning up traces and logs..."
rm -rf "$SCRIPT_DIR/.tusk/traces" "$SCRIPT_DIR/.tusk/logs" 2>/dev/null || true

echo "Flask HTTP E2E test run complete."

exit $EXIT_CODE
