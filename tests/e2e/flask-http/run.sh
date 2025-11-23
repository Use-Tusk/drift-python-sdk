#!/bin/bash

# Exit on error
set -e

# Accept optional port parameter (default: 5000)
APP_PORT=${1:-5000}
export APP_PORT

# Generate unique docker compose project name
PROJECT_NAME="flask-http-${APP_PORT}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting Flask HTTP E2E test run on port ${APP_PORT}..."

# Step 0: Clean up traces and logs
echo "Step 0: Cleaning up traces and logs..."
rm -rf "$SCRIPT_DIR/.tusk/traces" "$SCRIPT_DIR/.tusk/logs" 2>/dev/null || true

# Step 1: Start docker container
echo "Step 1: Starting docker container..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" build --no-cache
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" up -d --quiet-pull

# Wait for container to be ready
echo "Waiting for container to be ready..."
sleep 3

# Step 2: Start server in RECORD mode
echo "Step 2: Starting server in RECORD mode..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -d -T -e TUSK_DRIFT_MODE=RECORD app python app.py

# Wait for server to start
echo "Waiting for server to start..."
sleep 5

# Step 3: Hit all endpoints
echo "Step 3: Hitting all Flask HTTP endpoints..."

echo "  - GET /health"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/health > /dev/null

echo "  - GET /test-http-get"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/test-http-get > /dev/null

echo "  - POST /test-http-post"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s -X POST -H "Content-Type: application/json" -d '{"title":"test","body":"test body"}' http://localhost:5000/test-http-post > /dev/null

echo "  - PUT /test-http-put"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s -X PUT -H "Content-Type: application/json" -d '{"id":1,"title":"updated"}' http://localhost:5000/test-http-put > /dev/null

echo "  - DELETE /test-http-delete"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s -X DELETE http://localhost:5000/test-http-delete > /dev/null

echo "  - GET /test-http-headers"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/test-http-headers > /dev/null

echo "  - GET /test-http-query-params"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/test-http-query-params > /dev/null

echo "  - GET /test-http-error"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/test-http-error > /dev/null || true

echo "  - GET /test-chained-requests"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s http://localhost:5000/test-chained-requests > /dev/null

echo "  - GET /greet/World"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s "http://localhost:5000/greet/World?greeting=Hi" > /dev/null

echo "  - POST /echo"
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app curl -s -X POST -H "Content-Type: application/json" -d '{"message":"hello"}' http://localhost:5000/echo > /dev/null

echo "All endpoints hit successfully."

# Step 4: Wait before stopping server
echo "Step 4: Waiting 3 seconds before stopping server..."
sleep 3

# Stop the server process
echo "Stopping server..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app pkill -f "python app.py" || true
sleep 2

# Step 5: Run tests using tusk CLI
echo "Step 5: Running tests using tusk CLI..."
EXIT_CODE=0
TEST_RESULTS=$(docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" exec -T app tusk run --print --output-format "json" --enable-service-logs 2>&1) || EXIT_CODE=$?

echo ""
echo "Test Results:"
echo "$TEST_RESULTS"

# Step 6: Clean up
echo ""
echo "Step 6: Cleaning up docker containers..."
docker compose -p $PROJECT_NAME -f "$SCRIPT_DIR/docker-compose.yml" down

# Step 7: Clean up traces and logs
echo "Step 7: Cleaning up traces and logs..."
rm -rf "$SCRIPT_DIR/.tusk/traces" "$SCRIPT_DIR/.tusk/logs" 2>/dev/null || true

echo "Flask HTTP E2E test run complete."

exit $EXIT_CODE
