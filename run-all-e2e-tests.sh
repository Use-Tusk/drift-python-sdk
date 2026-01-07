#!/bin/bash

# Script to run all Python E2E tests for all instrumentation libraries
# This script discovers and runs all run.sh scripts with controlled concurrency
#
# Usage: ./run-all-e2e-tests.sh [MAX_CONCURRENT]
#   MAX_CONCURRENT: Number of tests to run concurrently (default: 1 = sequential)
#
# Examples:
#   ./run-all-e2e-tests.sh     # Run all tests sequentially
#   ./run-all-e2e-tests.sh 2   # Run 2 tests concurrently
#   ./run-all-e2e-tests.sh 0   # Run all tests in parallel (unlimited)

set -e

# Parse arguments
MAX_CONCURRENT=${1:-1}  # Default to sequential (1 at a time)

# Validate MAX_CONCURRENT is a number
if ! [[ "$MAX_CONCURRENT" =~ ^[0-9]+$ ]]; then
  echo "Error: MAX_CONCURRENT must be a non-negative integer"
  echo "Usage: $0 [MAX_CONCURRENT]"
  exit 1
fi

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get the directory where this script is located (SDK root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find all run.sh scripts in e2e-tests directories
RUN_SCRIPTS=($(find "$SCRIPT_DIR/drift/instrumentation" -path "*/e2e-tests/run.sh" -type f | sort))
NUM_TESTS=${#RUN_SCRIPTS[@]}

if [ $NUM_TESTS -eq 0 ]; then
  echo -e "${RED}No e2e test run.sh scripts found!${NC}"
  exit 1
fi

# Extract test names from paths
TEST_NAMES=()
for script in "${RUN_SCRIPTS[@]}"; do
  # Extract instrumentation name from path: drift/instrumentation/{name}/e2e-tests/run.sh
  TEST_NAME=$(echo "$script" | sed -E 's|.*/instrumentation/([^/]+)/e2e-tests/run.sh|\1|')
  TEST_NAMES+=("$TEST_NAME")
done

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Running Python SDK E2E Tests${NC}"
echo -e "${BLUE}========================================${NC}"
echo "Found $NUM_TESTS test(s): ${TEST_NAMES[*]}"
if [ $MAX_CONCURRENT -eq 0 ]; then
  echo "Concurrency: Unlimited (all in parallel)"
elif [ $MAX_CONCURRENT -eq 1 ]; then
  echo "Concurrency: Sequential"
else
  echo "Concurrency: $MAX_CONCURRENT at a time"
fi
echo ""
echo "Port allocation:"
for i in "${!TEST_NAMES[@]}"; do
  BASE_PORT=$((8000 + i))
  echo "  ${TEST_NAMES[$i]}: $BASE_PORT"
done
echo -e "${BLUE}========================================${NC}"
echo ""

# Create temporary directory for outputs
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Track results
declare -a TEST_PIDS
declare -a TEST_PORTS
declare -a TEST_EXIT_CODES
declare -a COMPLETED_PIDS

# Function to wait for any background job to complete
wait_for_any_job() {
  local pid
  while true; do
    for pid in "${TEST_PIDS[@]}"; do
      # Skip if already waited on this PID
      if [ "${COMPLETED_PIDS[$pid]}" = "1" ]; then
        continue
      fi

      if ! kill -0 "$pid" 2>/dev/null; then
        # Found a completed PID, now actually wait for it to fully clean up
        wait "$pid" 2>/dev/null || true
        # Mark this PID as completed so we don't wait on it again
        COMPLETED_PIDS[$pid]=1
        # Give the exit code file time to be written and flushed to disk
        sleep 1
        return 0
      fi
    done
    sleep 0.1
  done
}

# Launch all tests with controlled concurrency
RUNNING_COUNT=0
for i in "${!RUN_SCRIPTS[@]}"; do
  SCRIPT="${RUN_SCRIPTS[$i]}"
  TEST_NAME="${TEST_NAMES[$i]}"
  BASE_PORT=$((8000 + i))
  TEST_DIR=$(dirname "$SCRIPT")

  # If we have a concurrency limit and reached it, wait for a job to complete
  if [ $MAX_CONCURRENT -gt 0 ] && [ $RUNNING_COUNT -ge $MAX_CONCURRENT ]; then
    echo "Concurrency limit ($MAX_CONCURRENT) reached. Waiting for a test to complete..."
    wait_for_any_job
    RUNNING_COUNT=$((RUNNING_COUNT - 1))
  fi

  echo -e "${BLUE}=========================================${NC}"
  echo -e "${BLUE}[$((i + 1))/$NUM_TESTS] Starting $TEST_NAME on port $BASE_PORT...${NC}"
  echo -e "${BLUE}=========================================${NC}"

  # Make script executable
  chmod +x "$SCRIPT"

  # Run in background - expand all variables immediately to avoid scope issues
  # Disable 'set -e' in subshell to ensure exit code is always written
  (set +e; cd "$TEST_DIR" && ./run.sh "$BASE_PORT" > "$TEMP_DIR/${TEST_NAME}.log" 2>&1; EXIT=$?; echo "$EXIT" > "$TEMP_DIR/${TEST_NAME}.exit"; exit $EXIT) &
  PID=$!

  TEST_PIDS+=("$PID")
  TEST_PORTS+=("$BASE_PORT")
  RUNNING_COUNT=$((RUNNING_COUNT + 1))

  echo "Started in background (PID: $PID)"
  echo ""
done

echo -e "${BLUE}========================================${NC}"
echo "Waiting for all tests to complete..."
echo -e "${BLUE}========================================${NC}"
echo ""

# Wait for all background jobs and collect exit codes
OVERALL_EXIT_CODE=0
for i in "${!TEST_PIDS[@]}"; do
  PID="${TEST_PIDS[$i]}"
  TEST_NAME="${TEST_NAMES[$i]}"
  BASE_PORT="${TEST_PORTS[$i]}"

  # Wait for specific PID only if we haven't already waited on it
  if [ "${COMPLETED_PIDS[$PID]}" != "1" ]; then
    wait "$PID" 2>/dev/null || true
    COMPLETED_PIDS[$PID]=1
  fi

  # Wait for the exit file to exist (with timeout)
  TIMEOUT=10
  ELAPSED=0
  while [ ! -f "$TEMP_DIR/${TEST_NAME}.exit" ] && [ $ELAPSED -lt $TIMEOUT ]; do
    sleep 0.1
    ELAPSED=$((ELAPSED + 1))
  done

  # Read the actual exit code from the file
  ACTUAL_EXIT_CODE=1
  if [ -f "$TEMP_DIR/${TEST_NAME}.exit" ]; then
    ACTUAL_EXIT_CODE=$(cat "$TEMP_DIR/${TEST_NAME}.exit")
  fi

  TEST_EXIT_CODES+=($ACTUAL_EXIT_CODE)

  if [ $ACTUAL_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✓${NC} $TEST_NAME (port $BASE_PORT) completed successfully"
  else
    echo -e "${RED}✗${NC} $TEST_NAME (port $BASE_PORT) failed with exit code $ACTUAL_EXIT_CODE"
    OVERALL_EXIT_CODE=1
  fi

  # Show output from the test
  echo ""
  echo "--- Output from $TEST_NAME ---"
  if [ -f "$TEMP_DIR/${TEST_NAME}.log" ]; then
    cat "$TEMP_DIR/${TEST_NAME}.log"
  else
    echo "(No log file found)"
  fi
  echo "--- End of output from $TEST_NAME ---"
  echo ""
done

# Display final summary
echo ""
echo ""
echo -e "${BLUE}========================================${NC}"
echo "Final Summary"
echo -e "${BLUE}========================================${NC}"

for i in "${!TEST_NAMES[@]}"; do
  TEST_NAME="${TEST_NAMES[$i]}"
  BASE_PORT="${TEST_PORTS[$i]}"
  EXIT_CODE="${TEST_EXIT_CODES[$i]}"

  if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✓${NC} $TEST_NAME (port $BASE_PORT)"
  else
    echo -e "${RED}✗${NC} $TEST_NAME (port $BASE_PORT)"
  fi
done

echo -e "${BLUE}========================================${NC}"

if [ $OVERALL_EXIT_CODE -eq 0 ]; then
  echo -e "${GREEN}✓ All $NUM_TESTS test(s) passed!${NC}"
else
  echo -e "${RED}✗ Some tests failed!${NC}"
fi

echo -e "${BLUE}========================================${NC}"
echo ""

exit $OVERALL_EXIT_CODE

