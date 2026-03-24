---
name: bug-hunt
description: Hunt for instrumentation bugs by analyzing code gaps and running e2e tests through DISABLED/RECORD/REPLAY cycle
disable-model-invocation: true
---

# Instrumentation Bug Hunting

## Arguments

$ARGUMENTS - The library name, optionally followed by focus context.

**Format**: `<library> [focus on <area>]`

**Examples**:

- `/bug-hunt redis` — broad bug hunting across all redis functionality
- `/bug-hunt redis focus on pub sub interactions` — prioritize pub/sub patterns
- `/bug-hunt psycopg2 focus on async cursors and connection pooling` — prioritize those areas

**Parsing**: The first word of `$ARGUMENTS` is always the library name. Everything after it is the optional focus context. All references to `<library>` below mean this parsed first word — NOT the raw `$ARGUMENTS` string.

## Library-to-GitHub-Repo Mapping

Use this mapping to clone the package source code for analysis:

| Library    | GitHub Repo                                       | Notes                                          |
| ---------- | ------------------------------------------------- | ---------------------------------------------- |
| aiohttp    | https://github.com/aio-libs/aiohttp               |                                                |
| django     | https://github.com/django/django                  |                                                |
| fastapi    | https://github.com/tiangolo/fastapi               |                                                |
| flask      | https://github.com/pallets/flask                  |                                                |
| grpc       | https://github.com/grpc/grpc                      | Focus on `src/python/grpcio/`                  |
| httpx      | https://github.com/encode/httpx                   |                                                |
| psycopg    | https://github.com/psycopg/psycopg                | Monorepo — focus on `psycopg/`                 |
| psycopg2   | https://github.com/psycopg/psycopg2               |                                                |
| redis      | https://github.com/redis/redis-py                 |                                                |
| requests   | https://github.com/psf/requests                   |                                                |
| sqlalchemy | https://github.com/sqlalchemy/sqlalchemy          |                                                |
| urllib     | N/A                                               | Built-in Python stdlib — no repo to clone      |
| urllib3    | https://github.com/urllib3/urllib3                |                                                |

## E2E Test Paths

Each library has a single e2e-tests directory (no ESM/CJS variants):

| Library    | E2E test path                                          |
| ---------- | -------------------------------------------------------|
| aiohttp    | `drift/instrumentation/aiohttp/e2e-tests/`             |
| django     | `drift/instrumentation/django/e2e-tests/`              |
| fastapi    | `drift/instrumentation/fastapi/e2e-tests/`             |
| flask      | `drift/instrumentation/flask/e2e-tests/`               |
| grpc       | `drift/instrumentation/grpc/e2e-tests/`                |
| httpx      | `drift/instrumentation/httpx/e2e-tests/`               |
| psycopg    | `drift/instrumentation/psycopg/e2e-tests/`             |
| psycopg2   | `drift/instrumentation/psycopg2/e2e-tests/`            |
| redis      | `drift/instrumentation/redis/e2e-tests/`               |
| requests   | `drift/instrumentation/requests/e2e-tests/`            |
| sqlalchemy | `drift/instrumentation/sqlalchemy/e2e-tests/`          |
| urllib     | `drift/instrumentation/urllib/e2e-tests/`              |
| urllib3    | `drift/instrumentation/urllib3/e2e-tests/`             |

---

## Phase 0: Environment Setup

### 0.1 Parse and validate the arguments

Extract the library name (first word) and optional focus context (remaining words) from the arguments.

The library must be one of: aiohttp, django, fastapi, flask, grpc, httpx, psycopg, psycopg2, redis, requests, sqlalchemy, urllib, urllib3.

If the library is invalid, list the valid options and stop.

If focus context is provided, it will guide Phases 1 and 2 to prioritize that area of the library's functionality.

### 0.2 Docker Setup (Claude Code Web only)

Check if Docker is running. If not, start it:

```bash
dockerd --storage-driver=vfs &>/tmp/dockerd.log &
# Wait for Docker to be ready
for i in $(seq 1 30); do
  docker info &>/dev/null 2>&1 && break
  sleep 1
done
docker info &>/dev/null 2>&1 || { echo "Docker failed to start. Check /tmp/dockerd.log"; exit 1; }
```

If Docker is already running, skip this step.

### 0.3 Build the base Docker image

This is required before running any e2e tests:

```bash
cd <repo-root>
docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .
```

### 0.4 Clone the package source code (for analysis only)

If the library has a GitHub repo (see mapping above), clone it for reference:

```bash
git clone --depth 1 <repo-url> /tmp/<library-name>-source
```

This is read-only reference material — you will NOT modify this repo.

### 0.5 Create a working branch

Skip this step if you are already on a dedicated branch (e.g., in Claude Code Web where each session has its own branch).

```bash
git checkout -b bug-hunt/<library>-$(date +%Y-%m-%d)
```

---

## Phase 1: Develop Understanding

**If focus context was provided**, prioritize your analysis around that area. For example, if the focus is "pub sub interactions", concentrate on pub/sub-related code paths in the instrumentation, tests, and package source.

### 1.1 Analyze the Instrumentation Code

Read the instrumentation code at:

```
drift/instrumentation/<library>/instrumentation.py
```

Also check for any additional files in the same directory:

```
drift/instrumentation/<library>/
```

Identify:

- Which functions from the package are patched/instrumented
- The patching strategy (what gets wrapped, when, and how)
- Any helper modules or utilities used
- **If focus context provided**: Which patches relate to the focus area, and what's missing?

### 1.2 Analyze Existing E2E Tests

Review the test files:

- `drift/instrumentation/<library>/e2e-tests/src/app.py` — all test endpoints (Flask/FastAPI/Django app)
- `drift/instrumentation/<library>/e2e-tests/src/test_requests.py` — which endpoints are called
- `drift/instrumentation/<library>/e2e-tests/entrypoint.py` — test orchestration and setup

Understand what functionality is already tested and identify coverage gaps.

- **If focus context provided**: What tests already exist for the focus area? What's missing?

### 1.3 Analyze the Package Source Code

If you cloned the package source, read it to understand:

- The package's entry points and full API surface
- Functions that are currently patched vs functions that exist but aren't patched
- Alternative call patterns, overloads, and edge cases
- **If focus context provided**: Deep-dive into the focus area's API surface and usage patterns

---

## Phase 2: Identify Potential Gaps

**If focus context was provided**, prioritize bugs related to that area. You may still note other potential issues, but test the focus area first.

Reason about potential issues in the instrumentation. Consider:

- **Untested parameters**: Parameter combinations not covered by existing tests
- **Alternative call patterns**: Can patched functions be invoked differently (sync vs async, context managers, generators)?
- **Missing patches**: Functions that should be instrumented but aren't
- **Edge cases**: None values, empty results, large payloads, streaming responses, connection errors
- **Context managers**: Are `with` statements and async context managers handled?
- **Async variations**: For async libraries (aiohttp, httpx, fastapi), are all async patterns covered?
- **ORM/wrapper usage**: Libraries like SQLAlchemy that wrap database drivers — are those call paths instrumented?
- **Real-world usage patterns**: How is the package typically used in production?

Produce a prioritized list of potential bugs to investigate.

---

## Phase 3: Initialize Bug Tracking Document

Create `BUG_TRACKING.md` in the e2e test directory:

```bash
# Path: drift/instrumentation/<library>/e2e-tests/BUG_TRACKING.md
```

```markdown
# <library> Instrumentation Bug Tracking

Generated: <current date and time>

## Summary

- Total tests attempted: 0
- Confirmed bugs: 0
- No bugs found: 0
- Skipped tests: 0

---

## Test Results

(Tests will be documented below as they are completed)
```

---

## Phase 4: Write Tests and Verify Issues

For each potential bug, follow this workflow:

### 4.1 Initial Setup (Once)

Navigate to the e2e test directory:

```bash
cd drift/instrumentation/<library>/e2e-tests/
```

Build and start Docker containers:

```bash
docker compose build
docker compose run --rm -d --name bug-hunt-app app /bin/bash -c "sleep infinity"
```

This starts the container in the background so you can exec into it.

### 4.2 Test Each Potential Bug (Repeat for each)

#### A. Clean Previous Test Data

```bash
docker exec bug-hunt-app rm -rf .tusk/traces/* .tusk/logs/*
```

#### B. Write New Test Endpoint

Add a new endpoint to `src/app.py` that exercises the potential bug. Also add the corresponding request to `src/test_requests.py`.

Example for Flask:

```python
@app.route("/test/my-new-test", methods=["GET"])
def my_new_test():
    # Your test code here
    return jsonify({"success": True})
```

#### C. Test in DISABLED Mode (No Instrumentation)

Start server without instrumentation:

```bash
docker exec -e TUSK_DRIFT_MODE=DISABLED bug-hunt-app python src/app.py &
sleep 5
```

Hit the endpoint:

```bash
docker exec bug-hunt-app curl -s http://localhost:8000/test/my-new-test
```

**Verify**: Response is correct and endpoint works.

Stop the server:

```bash
docker exec bug-hunt-app pkill -f "python src/app.py" || true
sleep 2
```

**If the endpoint fails in DISABLED mode**:

- Update `BUG_TRACKING.md` with status: "Skipped - Failed in DISABLED mode"
- Fix the test code or move on to next potential bug

#### D. Test in RECORD Mode (With Instrumentation)

Clean traces and logs:

```bash
docker exec bug-hunt-app rm -rf .tusk/traces/* .tusk/logs/*
```

Start server in RECORD mode:

```bash
docker exec -e TUSK_DRIFT_MODE=RECORD bug-hunt-app python src/app.py &
sleep 5
```

Hit the endpoint:

```bash
docker exec bug-hunt-app curl -s http://localhost:8000/test/my-new-test
```

Wait for spans to export:

```bash
sleep 3
```

Stop the server:

```bash
docker exec bug-hunt-app pkill -f "python src/app.py" || true
sleep 2
```

**Check for issues:**

1. **Endpoint returns error or wrong response vs DISABLED mode**:
   - BUG FOUND: Instrumentation breaks functionality
   - Update `BUG_TRACKING.md`: Status "Confirmed Bug - RECORD mode failure", Failure Point "RECORD"
   - Keep the endpoint, move to next

2. **No traces created** (`docker exec bug-hunt-app ls .tusk/traces/`):
   - BUG FOUND: Instrumentation failed to capture traffic
   - Update `BUG_TRACKING.md`: Status "Confirmed Bug - No traces captured", Failure Point "RECORD"
   - Keep the endpoint, move to next

#### E. Test in REPLAY Mode

Run the Tusk CLI to replay:

```bash
docker exec -e TUSK_ANALYTICS_DISABLED=1 bug-hunt-app tusk drift run --print --output-format "json" --enable-service-logs
```

**Check for issues:**

1. **Test fails** (`"passed": false` in JSON output):
   - BUG FOUND: Replay doesn't match recording
   - Update `BUG_TRACKING.md`: Status "Confirmed Bug - REPLAY mismatch", Failure Point "REPLAY"

2. **No logs created** (`docker exec bug-hunt-app ls .tusk/logs/`):
   - BUG FOUND: Replay failed to produce logs
   - Update `BUG_TRACKING.md`: Status "Confirmed Bug - No replay logs", Failure Point "REPLAY"

3. **Logs contain socket warnings**:

   ```bash
   docker exec bug-hunt-app cat .tusk/logs/*.log | grep -i "TCP connect() called from inbound request context"
   ```

   - BUG FOUND: Unpatched dependency detected
   - Update `BUG_TRACKING.md`: Status "Confirmed Bug - Unpatched dependency", Failure Point "REPLAY"

#### F. No Bug Found

If all modes pass with no issues:

- Update `BUG_TRACKING.md`: Status "No Bug - Test passed all modes"
- **Remove the test endpoint** from `src/app.py` and `src/test_requests.py`
- Move to next potential bug

---

## Phase 5: Bug Tracking Documentation Format

After each test, append to `BUG_TRACKING.md`:

```markdown
### Test N: [Brief description]

**Status**: [Confirmed Bug | No Bug | Skipped]

**Endpoint**: `/test/endpoint-name`

**Failure Point**: [DISABLED | RECORD | REPLAY | N/A]

**Description**:
[What this test was trying to uncover]

**Expected Behavior**:
[What should happen]

**Actual Behavior**:
[What actually happened]

**Error Logs**:
```

[Relevant error messages, stack traces, or warnings]

```

**Additional Notes**:
[Observations, potential root causes, context]

---
```

**Important**: Update `BUG_TRACKING.md` immediately after each test — do not batch updates.

---

## Phase 6: Cleanup and Commit

After testing all potential bugs:

```bash
docker stop bug-hunt-app || true
docker compose down -v
```

Clean up cloned package source:

```bash
rm -rf /tmp/*-source
```

**Final state of the e2e test files:**

- `src/app.py` should contain ONLY the original endpoints + new endpoints that expose confirmed bugs
- `src/test_requests.py` should be updated to include requests to bug-exposing endpoints
- `BUG_TRACKING.md` should have accurate summary counts and all test results

Commit the changes:

```bash
git add drift/instrumentation/<library>/e2e-tests/
git commit -m "bug-hunt(<library>): add e2e tests exposing instrumentation bugs"
```

Push the branch (skip if in Claude Code Web where the session handles this):

```bash
git push origin bug-hunt/<library>-$(date +%Y-%m-%d)
```

---

## Success Criteria

1. Created `BUG_TRACKING.md` before starting any tests
2. Tested all identified potential bugs
3. Updated `BUG_TRACKING.md` after each individual test
4. Only bug-exposing endpoints remain in test files
5. Removed test endpoints that didn't expose bugs
6. Accurate summary counts in `BUG_TRACKING.md`
7. Changes committed and pushed to a `bug-hunt/` branch
