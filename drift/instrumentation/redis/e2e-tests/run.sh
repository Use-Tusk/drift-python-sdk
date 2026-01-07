#!/bin/bash

# Exit on error
set -e

# Accept optional port parameter (default: 8000)
APP_PORT=${1:-8000}
export APP_PORT

# Generate unique docker compose project name
# Get the instrumentation name (parent directory of e2e-tests)
TEST_NAME="$(basename "$(dirname "$(pwd)")")"
PROJECT_NAME="python-${TEST_NAME}-${APP_PORT}"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Running Python E2E Test: ${TEST_NAME}${NC}"
echo -e "${BLUE}Port: ${APP_PORT}${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo -e "${YELLOW}Cleaning up containers...${NC}"
    docker compose -p "$PROJECT_NAME" down -v 2>/dev/null || true
}

# Register cleanup on exit
trap cleanup EXIT

# Build containers
echo -e "${BLUE}Building containers...${NC}"
docker compose -p "$PROJECT_NAME" build --no-cache

# Run the test container
echo -e "${BLUE}Starting test...${NC}"
echo ""

# Run container and capture exit code (always use port 8000 inside container)
docker compose -p "$PROJECT_NAME" run --rm app

# Capture exit code from docker compose
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}✓ Test passed!${NC}"
    echo -e "${GREEN}========================================${NC}"
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}✗ Test failed with exit code ${EXIT_CODE}${NC}"
    echo -e "${RED}========================================${NC}"
fi

exit $EXIT_CODE
