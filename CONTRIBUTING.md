# Contributing to Drift Python SDK

## Development Setup

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone https://github.com/Use-Tusk/drift-python-sdk.git
cd drift-python-sdk

# Create virtual environment and install dependencies
uv sync --all-extras
```

## Code Quality

This project uses [ruff](https://docs.astral.sh/ruff/) for linting/formatting and [ty](https://github.com/astral-sh/ty) for type checking.

```bash
uv run ruff check drift/ --fix   # Lint and auto-fix
uv run ruff format drift/        # Format
uv run ty check drift/           # Type check
```

## Running Tests

### Unit Tests

```bash
uv run python -m unittest discover -s tests/unit -v

# Run a specific test file
uv run python -m unittest tests.unit.test_json_schema_helper -v
uv run python -m unittest tests.unit.test_adapters -v
```

### Integration Tests

```bash
# Flask/FastAPI integration tests
timeout 30 uv run python -m unittest discover -s tests/integration -v
```

### Database Integration Tests

Requires Docker for running test databases.

```bash
# Start test databases
docker compose -f docker-compose.test.yml up -d

# Run database tests
uv run python -m unittest tests.integration.test_database -v

# Stop databases
docker compose -f docker-compose.test.yml down
```

### E2E Tests

E2E tests validate full instrumentation workflows using Docker containers. They record real API interactions and verify replay behavior using the Tusk CLI.

#### Prerequisites

1. Build the base Docker image (required before running any e2e test):

    ```bash
    docker build -t python-e2e-base:latest -f drift/instrumentation/e2e_common/Dockerfile.base .
    ```

2. Docker and Docker Compose must be installed.

#### Running E2E Tests

Run all e2e tests:

```bash
./run-all-e2e-tests.sh        # Sequential (default)
./run-all-e2e-tests.sh 2      # 2 tests in parallel
./run-all-e2e-tests.sh 0      # All tests in parallel
```

Run a single instrumentation's e2e test:

```bash
cd drift/instrumentation/flask/e2e-tests
./run.sh

# Or with a custom port:
./run.sh 8001
```

#### Available E2E Tests

| Instrumentation | Location | Services |
|-----------------|----------|----------|
| Flask | `drift/instrumentation/flask/e2e-tests/` | None (external APIs) |
| FastAPI | `drift/instrumentation/fastapi/e2e-tests/` | None (external APIs) |
| Django | `drift/instrumentation/django/e2e-tests/` | None (external APIs) |
| Redis | `drift/instrumentation/redis/e2e-tests/` | Redis 7 |
| Psycopg | `drift/instrumentation/psycopg/e2e-tests/` | PostgreSQL 13 |
| Psycopg2 | `drift/instrumentation/psycopg2/e2e-tests/` | PostgreSQL 13 |

#### E2E Test Structure

Each e2e test directory contains:

```text
e2e-tests/
├── Dockerfile           # Builds on python-e2e-base
├── docker-compose.yml   # Service orchestration
├── run.sh               # External runner script
├── entrypoint.py        # Test orchestrator (setup → record → test)
├── requirements.txt     # Python dependencies
├── .tusk/config.yaml    # Tusk CLI configuration
└── src/
    ├── app.py           # Test application
    └── test_requests.py # HTTP request script
```

#### Debugging E2E Tests

View traces after a test:

```bash
cd drift/instrumentation/flask/e2e-tests
# JSONL files contain one JSON object per line, use jq to format them
cat .tusk/traces/*.jsonl | jq .
```

View service logs:

```bash
cat .tusk/logs/*
```

Run test app locally (outside Docker):

```bash
cd drift/instrumentation/flask/e2e-tests
pip install -r requirements.txt
TUSK_DRIFT_MODE=RECORD python src/app.py

# In another terminal:
python src/test_requests.py
```

For more details, see `drift/instrumentation/README-e2e-tests.md`.
