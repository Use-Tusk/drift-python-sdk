# Python E2E Tests

End-to-end tests for the Drift Python SDK instrumentations. Each instrumentation has its own e2e-tests directory that validates the instrumentation by running a real application, recording traces, and verifying behavior via the Tusk CLI.

## Directory Structure

Following the Node.js SDK pattern, e2e tests are co-located with their instrumentations:

```
python/drift/instrumentation/
├── flask/
│   └── e2e-tests/          # Flask HTTP instrumentation tests
├── redis/
│   └── e2e-tests/          # Redis instrumentation tests
├── psycopg/
│   └── e2e-tests/          # Psycopg (v3) instrumentation tests
└── psycopg2/
    └── e2e-tests/          # Psycopg2 (legacy) instrumentation tests
```

Each e2e-tests directory contains:

```
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

### 1. flask-http
**Purpose:** Test Flask HTTP instrumentation

**Features:**
- External API calls (weather, user data, posts)
- Parallel request execution with OpenTelemetry context propagation
- Multiple HTTP methods (GET, POST, DELETE)

**Services:** None (external APIs only)

**Run:**
```bash
cd python/drift/instrumentation/flask/e2e-tests
./run.sh
```

---

### 2. redis
**Purpose:** Test Redis instrumentation

**Features:**
- Redis operations (SET, GET, INCR, DELETE, KEYS)
- Pattern matching
- Expiration

**Services:** Redis 7

**Run:**
```bash
cd python/drift/instrumentation/redis/e2e-tests
./run.sh
```

---

### 3. psycopg
**Purpose:** Test Psycopg (v3) PostgreSQL instrumentation

**Features:**
- Basic CRUD operations (SELECT, INSERT, UPDATE, DELETE)
- Batch operations (executemany)
- Transactions and rollback
- Connection pooling

**Services:** PostgreSQL 13

**Run:**
```bash
cd python/drift/instrumentation/psycopg/e2e-tests
./run.sh
```

---

### 4. psycopg2
**Purpose:** Test Psycopg2 (legacy) PostgreSQL instrumentation

**Features:**
- Same as psycopg but using psycopg2 driver
- Tests backward compatibility

**Services:** PostgreSQL 13

**Run:**
```bash
cd python/drift/instrumentation/psycopg2/e2e-tests
./run.sh
```

---

### 5. django-http
**Purpose:** Test Django HTTP instrumentation (no database)

**Features:**
- Django middleware instrumentation
- HTTP request/response capture
- External API calls
- Route parameter handling

**Services:** None

**Run:**
```bash
cd python/drift/instrumentation/django/e2e-tests
./run.sh
```

---

## How E2E Tests Work

### Architecture

The e2e tests follow a **Docker entrypoint-driven architecture** where the Python `entrypoint.py` script inside the container handles the full test lifecycle:

```
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
│  Phase 4: Cleanup                       │
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
docker build -t python-e2e-base:latest -f src/e2e-common/python-base/Dockerfile .
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
cd python/drift/instrumentation/<instrumentation-name>/e2e-tests
./run.sh
```

Example:
```bash
cd python/drift/instrumentation/flask/e2e-tests
./run.sh
```

### All Tests (Sequential)

```bash
for dir in python/drift/instrumentation/*/e2e-tests; do
  if [ -f "$dir/run.sh" ]; then
    cd "$dir"
    ./run.sh
    cd -
  fi
done
```

### All Tests (Parallel)

```bash
cd python/drift/instrumentation/flask/e2e-tests && ./run.sh 8000 &
cd python/drift/instrumentation/redis/e2e-tests && ./run.sh 8001 &
cd python/drift/instrumentation/psycopg/e2e-tests && ./run.sh 8002 &
wait
```

Each test uses a unique Docker Compose project name based on the port, so they don't conflict.

---

## Understanding Test Output

### Successful Test

```
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
cd python/drift/instrumentation/flask/e2e-tests
cat .tusk/traces/*.jsonl | jq
```

### View Logs

Logs are saved to `.tusk/logs/`:

```bash
cd python/drift/instrumentation/flask/e2e-tests
cat .tusk/logs/*
```

### Run Test App Locally

You can run the test app outside Docker for debugging:

```bash
cd python/drift/instrumentation/flask/e2e-tests
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
cd python/drift/instrumentation/flask/e2e-tests
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
          - flask-http
          - redis
          - psycopg
          - psycopg2
          - django-http

    steps:
      - uses: actions/checkout@v3

      - name: Build base image
        run: |
          docker build \
            -t python-e2e-base:latest \
            -f src/e2e-common/python-base/Dockerfile \
            .

      - name: Run ${{ matrix.test }} test
        run: |
          cd python/drift/instrumentation/${{ matrix.test }}/e2e-tests
          ./run.sh

      - name: Upload traces on failure
        if: failure()
        uses: actions/upload-artifact@v3
        with:
          name: traces-${{ matrix.test }}
          path: python/drift/instrumentation/${{ matrix.test }}/e2e-tests/.tusk/traces/
```

---

## Adding New Tests

To add a new e2e test:

1. **Create directory**: `python/drift/instrumentation/<instrumentation-name>/e2e-tests/`

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
   cd python/drift/instrumentation/<instrumentation-name>/e2e-tests
   ./run.sh
   ```

5. **Add to CI**: Update GitHub Actions workflow

---

## Troubleshooting

### "python-e2e-base:latest not found"

Build the base image first:
```bash
docker build -t python-e2e-base:latest -f src/e2e-common/python-base/Dockerfile .
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

---

## Comparison with Node.js E2E Tests

| Aspect | Node.js | Python |
|--------|---------|--------|
| **Module variants** | CJS + ESM | Single version (no module variants needed) |
| **Orchestration** | Bash scripts | Python entrypoint |
| **External script** | Complex (9-step pipeline) | Simple (just starts containers) |
| **Exit codes** | Complex pipe handling | Native Docker propagation |
| **Setup location** | External script | Inside container (entrypoint) |
| **Shared utilities** | e2e-helpers.sh | Reusable entrypoint.py |

Both approaches achieve the same goal, but Python's entrypoint-driven design is simpler and more maintainable.

---

## Related Documentation

- [Base Image README](../../src/e2e-common/python-base/README.md) - Python e2e base Docker image
- [Python SDK README](../README.md) - Main Python SDK documentation
- [Node.js E2E Tests](../../src/instrumentation/libraries/postgres/e2e-tests/) - For comparison

---

## Maintaining These Tests

### Updating Tusk CLI Version

Update in base image:
```dockerfile
# src/e2e-common/python-base/Dockerfile
ARG TUSK_CLI_VERSION=v2.0.0
```

Then rebuild all tests (they'll use new base image).

### Updating Python Version

Update in base image:
```dockerfile
# src/e2e-common/python-base/Dockerfile
FROM python:3.13-slim  # Update from 3.12
```

### Adding Dependencies

Update `requirements.txt` in specific test directory:
```
# python/e2e-tests/flask-http/requirements.txt
Flask>=3.2.0  # Update version
```

---

## Success Criteria

All tests should:
- ✅ Exit with code 0 on success
- ✅ Record traces to `.tusk/traces/*.jsonl`
- ✅ Pass all Tusk CLI tests
- ✅ Complete in < 2 minutes
- ✅ Clean up containers on exit
- ✅ Work in both local and CI environments
