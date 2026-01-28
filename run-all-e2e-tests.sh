#!/bin/bash

# Script to run all Python E2E and stack tests for all instrumentation libraries
# This script discovers and runs all run.sh scripts with controlled concurrency
#
# Test types:
#   - E2E tests: Single instrumentation tests (e.g., django, flask, psycopg2)
#   - Stack tests: Full-stack tests combining multiple instrumentations (e.g., django-postgres, fastapi-postgres)

set -e

# Default values
MAX_CONCURRENT=1
RUN_E2E=true
RUN_STACK=true

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

usage() {
  echo "Usage: $0 [OPTIONS]"
  echo ""
  echo "Run all Python SDK E2E and stack tests."
  echo ""
  echo "Options:"
  echo "  -c, --concurrency N    Number of tests to run concurrently (default: 1)"
  echo "                         Use 0 for unlimited parallelism"
  echo "  --instrumentation-only Run only single-instrumentation e2e tests"
  echo "  --stack-only           Run only stack tests"
  echo "  -h, --help             Show this help message"
  echo ""
  echo "Examples:"
  echo "  $0                           # Run all tests sequentially"
  echo "  $0 -c 2                      # Run 2 tests concurrently"
  echo "  $0 -c 0                      # Run all tests in parallel"
  echo "  $0 --instrumentation-only    # Run only e2e tests"
  echo "  $0 --stack-only              # Run only stack tests"
  echo "  $0 --stack-only -c 3         # Run stack tests, 3 at a time"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -c|--concurrency)
      if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
        echo "Error: --concurrency requires a number argument"
        exit 1
      fi
      MAX_CONCURRENT="$2"
      shift 2
      ;;
    --instrumentation-only)
      RUN_E2E=true
      RUN_STACK=false
      shift
      ;;
    --stack-only)
      RUN_E2E=false
      RUN_STACK=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown option $1"
      usage
      exit 1
      ;;
  esac
done

# Validate MAX_CONCURRENT is a number
if ! [[ "$MAX_CONCURRENT" =~ ^[0-9]+$ ]]; then
  echo "Error: --concurrency must be a non-negative integer"
  exit 1
fi

# Get the directory where this script is located (SDK root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find test scripts based on flags
E2E_SCRIPTS=()
STACK_SCRIPTS=()

if [ "$RUN_E2E" = true ]; then
  E2E_SCRIPTS=($(find "$SCRIPT_DIR/drift/instrumentation" -path "*/e2e-tests/run.sh" -type f | sort))
fi

if [ "$RUN_STACK" = true ]; then
  STACK_SCRIPTS=($(find "$SCRIPT_DIR/drift/stack-tests" -mindepth 2 -maxdepth 2 -name "run.sh" -type f 2>/dev/null | sort))
fi

# Combine both arrays
RUN_SCRIPTS=("${E2E_SCRIPTS[@]}" "${STACK_SCRIPTS[@]}")
NUM_TESTS=${#RUN_SCRIPTS[@]}

if [ $NUM_TESTS -eq 0 ]; then
  echo -e "${RED}No test scripts found!${NC}"
  exit 1
fi

# Extract test names from paths
TEST_NAMES=()
for script in "${RUN_SCRIPTS[@]}"; do
  if [[ "$script" == *"/stack-tests/"* ]]; then
    # Extract from: drift/stack-tests/{name}/run.sh
    TEST_NAME=$(echo "$script" | sed -E 's|.*/stack-tests/([^/]+)/run.sh|stack:\1|')
  else
    # Extract from: drift/instrumentation/{name}/e2e-tests/run.sh
    TEST_NAME=$(echo "$script" | sed -E 's|.*/instrumentation/([^/]+)/e2e-tests/run.sh|\1|')
  fi
  TEST_NAMES+=("$TEST_NAME")
done

# Determine what we're running for display
TEST_TYPE_DESC=""
if [ "$RUN_E2E" = true ] && [ "$RUN_STACK" = true ]; then
  TEST_TYPE_DESC="E2E & Stack Tests"
elif [ "$RUN_E2E" = true ]; then
  TEST_TYPE_DESC="E2E Tests (instrumentation only)"
else
  TEST_TYPE_DESC="Stack Tests"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Running Python SDK $TEST_TYPE_DESC${NC}"
echo -e "${BLUE}========================================${NC}"
echo "Found ${#E2E_SCRIPTS[@]} e2e test(s), ${#STACK_SCRIPTS[@]} stack test(s)"
echo "Tests: ${TEST_NAMES[*]}"
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
