# E2E Testing Guide for Tusk Drift Python SDK

## Overview

The Tusk Drift Python SDK is a Python library that enables recording and replaying of both outbound and inbound network calls. This allows you to capture real API interactions during development and replay them during testing, ensuring consistent and reliable test execution without external dependencies.

The SDK instruments various Python libraries (requests, httpx, psycopg, redis, etc.) and web frameworks (Flask, FastAPI, Django) to intercept and record network traffic. During replay mode, the SDK matches incoming requests against recorded traces and returns the previously captured responses.

## Purpose of This Guide

This guide provides step-by-step instructions for iterating on SDK instrumentations when debugging E2E tests. Use this when:

- An E2E test endpoint is failing
- You need to debug or fix instrumentation code
- You want to verify that SDK changes work correctly

## E2E Test Structure

E2E tests are located in `drift/instrumentation/{instrumentation}/e2e-tests/`:

Each test directory contains:

- `src/` - Test application source code
  - `app.py` - The test application
  - `test_requests.py` - HTTP requests to execute during recording
- `Dockerfile` - Container configuration (builds on `python-e2e-base`)
- `docker-compose.yml` - Container orchestration
- `.tusk/` - Traces and logs directory
  - `config.yaml` - Tusk CLI configuration
  - `traces/` - Recorded network traces
  - `logs/` - Service execution logs
- `entrypoint.py` - Test orchestrator (runs inside container)
- `run.sh` - External test runner (starts containers)
- `requirements.txt` - Python dependencies

## Prerequisites

### Build the Base Image

Before running any E2E test, you must build the shared Python e2e base image:

```bash
cd drift-python-sdk
docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .
```

This image contains:
- Python 3.12
- Tusk CLI (for running replay tests)
- System utilities (curl, postgresql-client)

**Important:** You only need to rebuild the base image when:
- The Tusk CLI version needs to be updated
- System dependencies change
- Python version needs to be updated

## Quick Iteration Workflow

### Step 1: Navigate to the E2E Test Directory

```bash
cd drift/instrumentation/{instrumentation}/e2e-tests
```

Example:

```bash
cd drift/instrumentation/flask/e2e-tests
```

### Step 2: Clean Up Previous Test Data

Before running a new test iteration, delete existing traces and logs to ensure only current test data is present:

```bash
rm -rf .tusk/traces/*
rm -rf .tusk/logs/*
```

This prevents confusion from old test runs and makes it easier to identify current issues.

### Step 3: Build and Start Docker Container

Build the test container (first time only, or when requirements.txt changes):

```bash
docker compose build
```

Start the container in interactive mode for debugging:

```bash
docker compose run --rm app /bin/bash
```

This drops you into a shell inside the container where you can run commands manually.

### Step 4: Start Server in RECORD Mode

Inside the container, start the application server in RECORD mode to capture network traffic:

```bash
TUSK_DRIFT_MODE=RECORD python src/app.py
```

The server will start and wait for requests. You should see output indicating the SDK initialized and the app is running.

### Step 5: Hit the Endpoint(s) You Want to Record

Open a new terminal, exec into the running container, and use `curl` to make requests to the endpoints you want to test:

```bash
# Find the container name
docker compose ps

# Exec into the container
docker compose exec app /bin/bash

# Make requests
curl -s http://localhost:8000/api/weather-activity
```

**Tip:** Check the test's `src/app.py` file to see all available endpoints

### Step 6: Wait Before Stopping the Server

Wait a few seconds to ensure all traces are written to local storage:

```bash
sleep 3
```

### Step 7: Stop the Server Process

Stop the Python server by pressing `Ctrl+C` in the terminal where it's running, or:

```bash
pkill -f "python src/app.py"
```

### Step 8: Run the Tusk CLI to Execute Tests

Run the Tusk CLI to replay the recorded traces:

```bash
TUSK_ANALYTICS_DISABLED=1 tusk run --print --output-format "json" --enable-service-logs
```

**Flags explained:**

- `--print` - Print test results to stdout
- `--output-format "json"` - Output results in JSON format
- `--enable-service-logs` - Write detailed service logs to `.tusk/logs/` for debugging

To see all available flags, run:

```bash
tusk run --help
```

**Interpreting Results:**

The output will be JSON with test results:

```json
{
  "test_id": "test-1",
  "passed": true,
  "duration": 150
}
{
  "test_id": "test-2",
  "passed": false,
  "duration": 200
}
```

- `"passed": true` - Test passed successfully
- `"passed": false` - Test failed (mismatch between recording and replay)
- Check `.tusk/logs/` for detailed error messages and debugging information

### Step 9: Review Logs for Issues

If tests fail, check the service logs for detailed error information:

```bash
ls .tusk/logs/
cat .tusk/logs/<log-file>
```

You can also view the traces recorded in the `.tusk/traces/` directory:

```bash
cat .tusk/traces/*.jsonl | python -m json.tool
```

### Step 10: Iterate on SDK Code

When you need to fix instrumentation code:

1. **Make changes to the SDK source code** in your editor
2. **NO need to rebuild Docker containers** - the SDK is mounted as a volume, so changes propagate automatically
3. **Clean up traces and logs** (Step 2)
4. **Restart the server in RECORD mode** (Step 4)
5. **Hit the endpoints again** (Steps 5-7)
6. **Run the CLI tests** (Step 8)
7. **Repeat until tests pass**

### Step 11: Clean Up Docker Containers

When you're done testing, clean up the Docker containers:

```bash
docker compose down -v
```

## Automated Testing

Each E2E test directory has a `run.sh` script that automates the entire workflow:

```bash
./run.sh
```

This script:

1. Builds containers
2. Runs the entrypoint (which handles setup, recording, testing, and cleanup)
3. Displays results with colored output
4. Exits with code 0 (success) or 1 (failure)

The actual test orchestration happens inside the container via `entrypoint.py`, which:

1. Installs Python dependencies
2. Starts app in RECORD mode
3. Executes test requests
4. Stops app, verifies traces
5. Runs `tusk run` CLI
6. Checks for socket instrumentation warnings
7. Returns exit code

Use `run.sh` for full test runs, and use the manual steps above for iterative debugging.

## Important Notes

### SDK Volume Mounting

The Docker Compose configuration mounts the SDK source code as a read-only volume:

```yaml
volumes:
  - ../../../..:/sdk  # SDK source mounted at /sdk
```

This means:

- **SDK changes propagate automatically** - no need to rebuild containers
- **Fast iteration** - just edit the SDK code and restart the app
- **Must rebuild only when** - requirements.txt changes or base image needs updating

### Traces and Logs

- **Traces** (`.tusk/traces/`) - Recorded network interactions in JSONL format
- **Logs** (`.tusk/logs/`) - Detailed service logs when `--enable-service-logs` is used
- **Always clean these before re-running tests** to avoid confusion

### Debugging Tips

1. **Check service logs first** - Most issues are explained in `.tusk/logs/`
2. **Verify traces were created** - Check `.tusk/traces/` has files after recording
3. **Test one endpoint at a time** - Easier to isolate issues
4. **Check for socket warnings** - Indicates missing instrumentation for a library

### Socket Instrumentation Warnings

The SDK monitors for unpatched dependencies - libraries that make network calls without proper instrumentation. If you see this warning in logs:

```
[SocketInstrumentation] TCP connect() called from inbound request context, likely unpatched dependency
```

This indicates a library is making TCP calls that aren't being instrumented. You should either:
- Add instrumentation for that library
- Investigate which library is making the unpatched calls

## Running All Tests

To run all E2E tests across all instrumentations:

```bash
# From SDK root directory

# Sequential (default)
./run-all-e2e-tests.sh

# 2 tests in parallel
./run-all-e2e-tests.sh 2

# All tests in parallel (unlimited)
./run-all-e2e-tests.sh 0
```

## Quick Reference Commands

```bash
# Build base image (first time only, or when updating CLI/Python version)
docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .

# Navigate to test directory
cd drift/instrumentation/flask/e2e-tests

# Clean traces and logs
rm -rf .tusk/traces/* .tusk/logs/*

# Build test container (first time only, or when requirements change)
docker compose build

# Run automated test
./run.sh

# Start container interactively for debugging
docker compose run --rm app /bin/bash

# Inside container: Start server in RECORD mode
TUSK_DRIFT_MODE=RECORD python src/app.py

# Inside container: Run test requests
python src/test_requests.py

# Inside container: Run Tusk CLI tests
TUSK_ANALYTICS_DISABLED=1 tusk run --print --output-format "json" --enable-service-logs

# View traces
cat .tusk/traces/*.jsonl | python -m json.tool

# View logs
cat .tusk/logs/*

# Clean up containers
docker compose down -v

# Run all E2E tests
./run-all-e2e-tests.sh
```
