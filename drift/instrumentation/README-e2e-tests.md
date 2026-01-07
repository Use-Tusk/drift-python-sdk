# Python E2E Tests

End-to-end tests for the Drift Python SDK instrumentations. Each instrumentation has its own e2e-tests directory that validates the instrumentation by running a real application, recording traces, and verifying behavior via the Tusk CLI.

## Directory Structure

Following the Node.js SDK pattern, e2e tests are co-located with their instrumentations:

```text
drift/instrumentation/
├── e2e_common/             # Shared base classes and Dockerfile
├── flask/
│   └── e2e-tests/          # Flask HTTP instrumentation tests
├── fastapi/
│   └── e2e-tests/          # FastAPI ASGI instrumentation tests
├── django/
│   └── e2e-tests/          # Django middleware instrumentation tests
├── redis/
│   └── e2e-tests/          # Redis instrumentation tests
├── psycopg/
│   └── e2e-tests/          # Psycopg (v3) instrumentation tests
└── psycopg2/
    └── e2e-tests/          # Psycopg2 (legacy) instrumentation tests
```

Each e2e-tests directory contains:

```text
e2e-tests/
├── Dockerfile              # Builds on python-e2e-base
├── docker-compose.yml      # Service orchestration
├── run.sh                  # External runner (starts containers)
├── entrypoint.py           # Test orchestrator (setup → record → test → cleanup)
├── requirements.txt        # Python dependencies
├── .tusk/config.yaml       # Tusk CLI configuration
└── src/
    ├── app.py              # Test application
    └── test_requests.py    # HTTP request script
```

## Available Tests

### 1. flask

**Purpose:** Test Flask HTTP instrumentation

**Features:**

- External API calls (weather, user data, posts)
- Parallel request execution with OpenTelemetry context propagation
- Multiple HTTP methods (GET, POST, DELETE)

**Services:** None (external APIs only)

**Run:**

```bash
cd drift/instrumentation/flask/e2e-tests
./run.sh
```

---

### 2. fastapi

**Purpose:** Test FastAPI ASGI instrumentation

**Features:**

- External API calls using both `requests` and `httpx`
- Async HTTP handling
- Parallel request execution with context propagation
- Multiple HTTP methods (GET, POST, DELETE)

**Services:** None (external APIs only)

**Run:**

```bash
cd drift/instrumentation/fastapi/e2e-tests
./run.sh
```

---

### 3. django

**Purpose:** Test Django middleware instrumentation

**Features:**

- Django middleware HTTP capture
- External API calls
- Parallel request execution with context propagation
- Multiple HTTP methods (GET, POST, DELETE)

**Services:** None (external APIs only)

**Run:**

```bash
cd drift/instrumentation/django/e2e-tests
./run.sh
```

---

### 4. redis

**Purpose:** Test Redis instrumentation

**Features:**

- Redis operations (SET, GET, INCR, DELETE, KEYS)
- Pattern matching
- Expiration

**Services:** Redis 7

**Run:**

```bash
cd drift/instrumentation/redis/e2e-tests
./run.sh
```

---

### 5. psycopg

**Purpose:** Test Psycopg (v3) PostgreSQL instrumentation

**Features:**

- Basic CRUD operations (SELECT, INSERT, UPDATE, DELETE)
- Batch operations (executemany)
- Transactions and rollback
- Connection pooling

**Services:** PostgreSQL 13

**Run:**

```bash
cd drift/instrumentation/psycopg/e2e-tests
./run.sh
```

---

### 6. psycopg2

**Purpose:** Test Psycopg2 (legacy) PostgreSQL instrumentation

**Features:**

- Same as psycopg but using psycopg2 driver
- Tests backward compatibility

**Services:** PostgreSQL 13

**Run:**

```bash
cd drift/instrumentation/psycopg2/e2e-tests
./run.sh
```

---

## How E2E Tests Work

### Architecture

The e2e tests follow a **Docker entrypoint-driven architecture** where the Python `entrypoint.py` script inside the container handles the full test lifecycle:

```text
┌─────────────────────────────────────────┐
│ run.sh (external orchestrator)          │
│  - Builds containers                    │
│  - Starts test via docker compose       │
│  - Captures exit code                   │
└─────────────┬───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│ entrypoint.py (inside container)        │
│                                         │
│  Phase 1: Setup                         │
│   - Install Python dependencies         │
│   - Initialize services (DB schema)     │
│                                         │
│  Phase 2: Record Traces                 │
│   - Start app in RECORD mode            │
│   - Execute test requests               │
│   - Stop app, verify traces created     │
│                                         │
│  Phase 3: Run Tests                     │
│   - Execute `tusk run` CLI              │
│   - Parse JSON test results             │
│                                         │
│  Phase 4: Check Instrumentation Warnings│
│   - Check logs for unpatched deps       │
│   - Verify trace files exist            │
│                                         │
│  Phase 5: Cleanup                       │
│   - Stop processes                      │
│   - Return exit code                    │
└─────────────────────────────────────────┘
```

### Why This Architecture?

**Benefits:**

- ✅ **Better error handling**: Python > Bash for complex orchestration
- ✅ **Proper exit codes**: Docker propagates exit codes from entrypoint
- ✅ **Simpler external script**: `run.sh` just starts containers
- ✅ **Reusable pattern**: Same structure across all tests
- ✅ **CI-friendly**: Exit codes enable CI pass/fail detection

**vs Node.js E2E Approach:**

- Node.js uses external bash scripts for orchestration
- Python uses Docker entrypoint for orchestration
- Both approaches work, but Python approach is more maintainable

---

## Prerequisites

### 1. Build Base Image

Before running any test, build the shared Python e2e base image:

```bash
docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .
```

This image contains:

- Python 3.12
- Tusk Drift CLI
- System utilities (curl, postgresql-client)

### 2. Install Docker

All tests require Docker and Docker Compose.

---

## Running Tests

### Single Test

```bash
cd drift/instrumentation/<instrumentation-name>/e2e-tests
./run.sh
```

Example:

```bash
cd drift/instrumentation/flask/e2e-tests
./run.sh
```

### All Tests (Using run-all script)

```bash
# Sequential (default)
./run-all-e2e-tests.sh

# 2 tests in parallel
./run-all-e2e-tests.sh 2

# All tests in parallel (unlimited)
./run-all-e2e-tests.sh 0
```

### All Tests (Manual Parallel)

```bash
cd drift/instrumentation/flask/e2e-tests && ./run.sh 8000 &
cd drift/instrumentation/fastapi/e2e-tests && ./run.sh 8001 &
cd drift/instrumentation/django/e2e-tests && ./run.sh 8002 &
wait
```

Each test uses a unique Docker Compose project name based on the port, so they don't conflict.

---

## Understanding Test Output

### Successful Test

```text
========================================
Phase 1: Setup
========================================
Installing Python dependencies...
Setup complete

========================================
Phase 2: Recording Traces
========================================
Starting application in RECORD mode...
Waiting for application to be ready...
Application is ready
Executing test requests...
→ GET /health
  Status: 200
...
Recorded 5 trace files

========================================
Phase 3: Running Tusk Tests
========================================
✓ Test ID: test-1 (Duration: 45ms)
✓ Test ID: test-2 (Duration: 32ms)
All tests passed!

========================================
Phase 4: Cleanup
========================================
Cleanup complete

========================================
✓ Test passed!
========================================
```

### Failed Test

If the test fails, you'll see:

- Red ✗ marks for failed tests
- Error messages
- Exit code 1

Traces are preserved in `.tusk/traces/` for inspection.

---

## Debugging Tests

### View Traces

Traces are saved to `.tusk/traces/*.jsonl` inside each test directory:

```bash
cd drift/instrumentation/flask/e2e-tests
cat .tusk/traces/*.jsonl | jq
```

### View Logs

Logs are saved to `.tusk/logs/`:

```bash
cd drift/instrumentation/flask/e2e-tests
cat .tusk/logs/*
```

### Run Test App Locally

You can run the test app outside Docker for debugging:

```bash
cd drift/instrumentation/flask/e2e-tests
pip install -r requirements.txt
TUSK_DRIFT_MODE=RECORD python src/app.py
```

Then in another terminal:

```bash
python src/test_requests.py
```

### Inspect Docker Container

To debug inside the container:

```bash
cd drift/instrumentation/flask/e2e-tests
docker compose build
docker compose run --rm app /bin/bash
```

---

## CI Integration

### GitHub Actions Example

```yaml
name: Python E2E Tests

on: [push, pull_request]

jobs:
  e2e-tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        test:
          - flask
          - fastapi
          - django
          - redis
          - psycopg
          - psycopg2

    steps:
      - uses: actions/checkout@v4

      - name: Build base image
        run: |
          docker build \
            -t python-e2e-base:latest \
            -f drift/instrumentation/e2e_common/Dockerfile.base \
            .

      - name: Run ${{ matrix.test }} test
        run: |
          cd drift/instrumentation/${{ matrix.test }}/e2e-tests
          ./run.sh

      - name: Upload traces on failure
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: traces-${{ matrix.test }}
          path: drift/instrumentation/${{ matrix.test }}/e2e-tests/.tusk/traces/
```

---

## Adding New Tests

To add a new e2e test:

1. **Create directory**: `drift/instrumentation/<instrumentation-name>/e2e-tests/`

2. **Copy template from existing test**: Use `flask/e2e-tests` as a template

3. **Customize files**:
   - `src/app.py` - Your test application
   - `src/test_requests.py` - HTTP requests to test
   - `requirements.txt` - Add instrumentation-specific dependencies
   - `docker-compose.yml` - Add required services (update context paths)
   - `Dockerfile` - Update COPY path for test files
   - `entrypoint.py` - Add service-specific setup (if needed)
   - `.tusk/config.yaml` - Update service ID/name

4. **Test locally**:

   ```bash
   cd drift/instrumentation/<instrumentation-name>/e2e-tests
   ./run.sh
   ```

5. **Add to CI**: Update GitHub Actions workflow

---

## Troubleshooting

### "python-e2e-base:latest not found"

Build the base image first:

```bash
docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .
```

### "Port already in use"

Each test uses port 8000 by default. Run tests on different ports:

```bash
./run.sh 8001
```

### "Service not ready"

Increase healthcheck timeout in `docker-compose.yml`:

```yaml
healthcheck:
  timeout: 10s  # Increase from 5s
  retries: 10   # Increase from 5
```

### "No traces recorded"

Check that:

1. `TUSK_DRIFT_MODE=RECORD` is set
2. SDK is initialized correctly in app
3. Requests are actually hitting the app
4. SDK has time to flush traces (3 second wait)

### Database Tests (psycopg/psycopg2) - Known Issues

The psycopg and psycopg2 e2e tests have partial pass rates due to the complexity of mocking database operations. Key findings:

**What works:**

- Basic `execute()` queries (SELECT, INSERT, UPDATE, DELETE)
- Mock data extraction from CLI (rowcount, description, rows)
- Health checks and simple endpoints

**Known issues:**

1. **`cursor.description` is read-only**: Psycopg3's cursor has `description` as a C-level read-only property. The SDK works around this by:
   - Adding `_tusk_description` attribute to `InstrumentedCursor`
   - Overriding the `description` property getter to return mock data when available

2. **`executemany()` mock matching**: Batch operations may fail to find mocks during replay. This appears to be a schema/hash mismatch between record and replay phases. The queries are recorded correctly but the CLI's mock matcher may not find them due to:
   - Parameter serialization differences
   - Hash calculation differences for batch parameters

3. **Trace blocking**: If a request returns HTTP 500 during recording, the entire trace is blocked and won't be available for replay. This can cause cascading failures if early requests fail due to database state issues.

**Workarounds:**

- Ensure clean database state before each test run (`docker compose down -v`)
- For `executemany` tests, consider using multiple `execute()` calls instead
- Check trace files to verify spans are being recorded: `cat .tusk/traces/*.jsonl | jq`

**Current test status:**

- Flask, FastAPI, Django, Redis: ✅ All tests pass
- psycopg: ⚠️ ~50% pass rate (execute works, executemany partial)
- psycopg2: ⚠️ Similar to psycopg

### Unpatched Dependency Detection

The e2e tests automatically check for **unpatched dependencies** - libraries that make network calls without proper instrumentation. This helps catch cases where:

1. A new dependency is added that makes HTTP/database calls but isn't instrumented
2. An instrumented library bypasses the patched code path
3. TCP connections are made from within a server span without going through instrumented code

**How it works:**

The Python SDK's socket instrumentation (`drift/instrumentation/socket/`) monitors low-level TCP operations during REPLAY mode. When a TCP call is made from within a SERVER span context (i.e., while handling an incoming request) without going through instrumented code, it logs a warning:

```text
[SocketInstrumentation] TCP connect() called from inbound request context, likely unpatched dependency
```

The e2e test runner checks for these warnings after running tests. If found, the test fails with an error indicating which unpatched dependency was detected.

**If you see this error:**

1. Check the log output for the full warning message and stack trace
2. Identify which library is making the unpatched TCP call
3. Either:
   - Add instrumentation for that library
   - Exclude it from monitoring if it's expected behavior (e.g., health checks)

This is equivalent to the Node.js SDK's `check_tcp_instrumentation_warning` check.

---

## Comparison with Node.js E2E Tests

| Aspect | Node.js | Python |
|--------|---------|--------|
| **Module variants** | CJS + ESM | Single version (no module variants needed) |
| **Orchestration** | Bash scripts | Python entrypoint |
| **External script** | Complex (9-step pipeline) | Simple (just starts containers) |
| **Exit codes** | Complex pipe handling | Native Docker propagation |
| **Setup location** | External script | Inside container (entrypoint) |
| **Shared utilities** | e2e-helpers.sh | base_runner.py |
| **Unpatched dep check** | `check_tcp_instrumentation_warning()` | `check_socket_instrumentation_warnings()` |

Both approaches achieve the same goal, but Python's entrypoint-driven design is simpler and more maintainable.

---

## Related Documentation

- [Base Dockerfile](./e2e_common/Dockerfile.base) - Python e2e base Docker image
- [Base Runner](./e2e_common/base_runner.py) - Shared e2e test runner class
- [Python SDK README](../../README.md) - Main Python SDK documentation
- [CONTRIBUTING.md](../../CONTRIBUTING.md) - Contribution guidelines with e2e test instructions

---

## Maintaining These Tests

### Updating Tusk CLI Version

Update in base image:

```dockerfile
# drift/instrumentation/e2e_common/Dockerfile.base
ARG TUSK_CLI_VERSION=v2.0.0
```

Then rebuild the base image and all tests will use the new CLI.

### Updating Python Version

Update in base image:

```dockerfile
# drift/instrumentation/e2e_common/Dockerfile.base
FROM python:3.13-slim  # Update from 3.12
```

### Adding Dependencies

Update `requirements.txt` in specific test directory:

```text
# drift/instrumentation/flask/e2e-tests/requirements.txt
Flask>=3.2.0  # Update version
```

---

## Success Criteria

All tests should:

- ✅ Exit with code 0 on success
- ✅ Record traces to `.tusk/traces/*.jsonl`
- ✅ Pass all Tusk CLI tests
- ✅ No unpatched dependency warnings in logs
- ✅ Complete in < 2 minutes
- ✅ Clean up containers on exit
- ✅ Work in both local and CI environments
