#!/bin/bash

# Script to run benchmarks for all Python SDK E2E instrumentation and stack tests serially.
# Each test runs with BENCHMARKS=1, producing a comparison of
# SDK disabled vs enabled (RECORD mode) with Go-style ns/op stats.
#
# Test types:
#   - Instrumentation: Single instrumentation benchmarks (e.g., flask, fastapi, django)
#   - Stack: Multi-instrumentation integration benchmarks (e.g., django-postgres, fastapi-postgres)
#
# Usage:
#   ./run-all-benchmarks.sh                     # Run all, 10s per endpoint
#   ./run-all-benchmarks.sh -d 20               # Run all, 20s per endpoint
#   ./run-all-benchmarks.sh -f flask,fastapi     # Run only flask and fastapi
#   ./run-all-benchmarks.sh --stack-only         # Run only stack-test benchmarks
#   ./run-all-benchmarks.sh -f stack:django-postgres  # Run a single stack benchmark
#   ./run-all-benchmarks.sh -h                  # Show help

set -e

# Default values
BENCHMARK_DURATION=${BENCHMARK_DURATION:-10}
BENCHMARK_WARMUP=${BENCHMARK_WARMUP:-3}
FILTER=""
RUN_INSTRUMENTATION=true
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
  echo "Run SDK benchmarks for all (or selected) instrumentations and stack tests serially."
  echo ""
  echo "Options:"
  echo "  -d, --duration N       Seconds per endpoint for timed loop (default: 10)"
  echo "  -w, --warmup N         Seconds of warmup per endpoint before timing (default: 3)"
  echo "  -f, --filter LIST      Comma-separated list of benchmarks to run"
  echo "                         e.g. flask,fastapi,stack:django-postgres"
  echo "  --instrumentation-only Run only single-instrumentation benchmarks"
  echo "  --stack-only           Run only stack-test benchmarks"
  echo "  -h, --help             Show this help message"
  echo ""
  echo "Environment variables:"
  echo "  BENCHMARK_DURATION     Same as --duration"
  echo "  BENCHMARK_WARMUP       Same as --warmup"
  echo "  TUSK_CLI_VERSION       CLI version to use in Docker builds"
  echo ""
  echo "Examples:"
  echo "  $0                           # Benchmark all instrumentations and stack tests"
  echo "  $0 -d 20                     # 20s per endpoint"
  echo "  $0 -d 30 -w 5               # 30s timed, 5s warmup"
  echo "  $0 -f flask,fastapi          # Only benchmark flask and fastapi"
  echo "  $0 --stack-only              # Only benchmark stack tests"
  echo "  $0 -f stack:django-postgres  # Only benchmark django-postgres stack test"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -d|--duration)
      if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
        echo "Error: --duration requires a number argument"
        exit 1
      fi
      BENCHMARK_DURATION="$2"
      shift 2
      ;;
    -w|--warmup)
      if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
        echo "Error: --warmup requires a number argument"
        exit 1
      fi
      BENCHMARK_WARMUP="$2"
      shift 2
      ;;
    -f|--filter)
      if [[ -z "$2" ]] || [[ "$2" == -* ]]; then
        echo "Error: --filter requires a comma-separated list"
        exit 1
      fi
      FILTER="$2"
      shift 2
      ;;
    --instrumentation-only)
      RUN_INSTRUMENTATION=true
      RUN_STACK=false
      shift
      ;;
    --stack-only)
      RUN_INSTRUMENTATION=false
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

# Validate numbers
if ! [[ "$BENCHMARK_DURATION" =~ ^[0-9]+$ ]]; then
  echo "Error: --duration must be a positive integer"
  exit 1
fi
if ! [[ "$BENCHMARK_WARMUP" =~ ^[0-9]+$ ]]; then
  echo "Error: --warmup must be a positive integer"
  exit 1
fi

# Get the directory where this script is located (SDK root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Discover test scripts based on flags
E2E_SCRIPTS=()
STACK_SCRIPTS=()

if [ "$RUN_INSTRUMENTATION" = true ]; then
  E2E_SCRIPTS=($(find "$SCRIPT_DIR/drift/instrumentation" -path "*/e2e-tests/run.sh" -type f | sort))
fi

if [ "$RUN_STACK" = true ]; then
  STACK_SCRIPTS=($(find "$SCRIPT_DIR/drift/stack-tests" -mindepth 2 -maxdepth 2 -name "run.sh" -type f 2>/dev/null | sort))
fi

ALL_SCRIPTS=("${E2E_SCRIPTS[@]}" "${STACK_SCRIPTS[@]}")

if [ ${#ALL_SCRIPTS[@]} -eq 0 ]; then
  echo -e "${RED}No benchmark scripts found!${NC}"
  exit 1
fi

# Extract names and apply filter
RUN_SCRIPTS=()
RUN_NAMES=()
for script in "${ALL_SCRIPTS[@]}"; do
  if [[ "$script" == *"/stack-tests/"* ]]; then
    NAME=$(echo "$script" | sed -E 's|.*/stack-tests/([^/]+)/run.sh|stack:\1|')
  else
    NAME=$(echo "$script" | sed -E 's|.*/instrumentation/([^/]+)/e2e-tests/run.sh|\1|')
  fi
  if [ -n "$FILTER" ]; then
    # Check if NAME is in the comma-separated filter list
    if echo ",$FILTER," | grep -q ",$NAME,"; then
      RUN_SCRIPTS+=("$script")
      RUN_NAMES+=("$NAME")
    fi
  else
    RUN_SCRIPTS+=("$script")
    RUN_NAMES+=("$NAME")
  fi
done

NUM_TESTS=${#RUN_SCRIPTS[@]}

if [ $NUM_TESTS -eq 0 ]; then
  echo -e "${RED}No matching benchmarks found for filter: $FILTER${NC}"
  # Show all available names
  ALL_NAMES=()
  for script in "${ALL_SCRIPTS[@]}"; do
    if [[ "$script" == *"/stack-tests/"* ]]; then
      ALL_NAMES+=($(echo "$script" | sed -E 's|.*/stack-tests/([^/]+)/run.sh|stack:\1|'))
    else
      ALL_NAMES+=($(echo "$script" | sed -E 's|.*/instrumentation/([^/]+)/e2e-tests/run.sh|\1|'))
    fi
  done
  echo "Available: ${ALL_NAMES[*]}"
  exit 1
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Python SDK Benchmarks${NC}"
echo -e "${BLUE}========================================${NC}"
echo "Benchmarks: ${RUN_NAMES[*]}"
echo "Warmup per endpoint: ${BENCHMARK_WARMUP}s"
echo "Duration per endpoint: ${BENCHMARK_DURATION}s"
echo "Total benchmarks: $NUM_TESTS"
echo -e "${BLUE}========================================${NC}"
echo ""

# Export benchmark env vars so they pass through docker-compose
export BENCHMARKS=1
export BENCHMARK_DURATION
export BENCHMARK_WARMUP

# Track results
OVERALL_EXIT_CODE=0
declare -a EXIT_CODES

for i in "${!RUN_SCRIPTS[@]}"; do
  SCRIPT="${RUN_SCRIPTS[$i]}"
  NAME="${RUN_NAMES[$i]}"
  TEST_DIR=$(dirname "$SCRIPT")

  echo ""
  echo -e "${BLUE}============================================================${NC}"
  echo -e "${BLUE}[$((i + 1))/$NUM_TESTS] Benchmarking: $NAME${NC}"
  echo -e "${BLUE}============================================================${NC}"
  echo ""

  chmod +x "$SCRIPT"

  set +e
  (cd "$TEST_DIR" && ./run.sh)
  EXIT_CODE=$?
  set -e

  EXIT_CODES+=("$EXIT_CODE")

  if [ $EXIT_CODE -ne 0 ]; then
    echo -e "${RED}Benchmark for $NAME failed with exit code $EXIT_CODE${NC}"
    OVERALL_EXIT_CODE=1
  fi

  echo ""
done

# Final summary
echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Benchmark Summary${NC}"
echo -e "${BLUE}========================================${NC}"

for i in "${!RUN_NAMES[@]}"; do
  NAME="${RUN_NAMES[$i]}"
  EXIT_CODE="${EXIT_CODES[$i]}"

  if [ "$EXIT_CODE" -eq 0 ]; then
    echo -e "${GREEN}  ✓${NC} $NAME"
  else
    echo -e "${RED}  ✗${NC} $NAME (exit code $EXIT_CODE)"
  fi
done

echo -e "${BLUE}========================================${NC}"

if [ $OVERALL_EXIT_CODE -eq 0 ]; then
  echo -e "${GREEN}All $NUM_TESTS benchmark(s) completed successfully.${NC}"
else
  echo -e "${RED}Some benchmarks failed.${NC}"
fi

echo ""
exit $OVERALL_EXIT_CODE
