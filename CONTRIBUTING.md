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
uv run ruff check drift/ tests/ --fix  # Lint and auto-fix
uv run ruff format drift/ tests/       # Format
uv run ty check drift/ tests/          # Type check
```

## Running Tests

### Unit Tests

```bash
uv run pytest tests/unit/ -v

# Run with coverage
uv run pytest tests/unit/ -v --cov=drift --cov-report=term-missing

# Run a specific test file
uv run pytest tests/unit/test_json_schema_helper.py -v
uv run pytest tests/unit/test_adapters.py -v

# Run a specific test class or function
uv run pytest tests/unit/test_metrics.py::TestMetricsCollector -v
uv run pytest tests/unit/test_metrics.py::TestMetricsCollector::test_record_spans_exported -v
```

### Integration Tests

```bash
# Flask/FastAPI integration tests
timeout 30 uv run pytest tests/integration/ -v
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

## Documentation

| Document | Description |
|----------|-------------|
| `docs/context-propagation.md` | Context propagation behavior, edge cases, and patterns |
| `drift/instrumentation/README-e2e-tests.md` | E2E test architecture and debugging |

## For Maintainers

### Releasing

Releases are automated using GitHub Actions. When a GitHub Release is created, the package is automatically built and published to PyPI using [trusted publishing](https://docs.pypi.org/trusted-publishers/).

Prerequisites:

- [GitHub CLI](https://cli.github.com/) (`gh`) installed and authenticated
- On the `main` branch with no uncommitted changes
- Local branch up to date with remote

#### Creating a release

Use the release script to bump the version, create a tag, and publish a GitHub Release:

```bash
# Patch release (0.1.5 → 0.1.6)
./scripts/release.sh patch

# Minor release (0.1.5 → 0.2.0)
./scripts/release.sh minor
```

The script will:

1. Run preflight checks (lint, format, tests)
2. Calculate the next version from `pyproject.toml`
3. Update the version in `pyproject.toml`
4. Commit and tag the version bump
5. Push to origin and create a GitHub Release

Once the release is created, GitHub Actions will automatically:

- Build the Python package with `uv build`
- Publish to PyPI using trusted publishing (no API tokens needed)

#### Manual workflow dispatch

For testing purposes, you can also trigger the publish workflow manually from the Actions tab with an optional version override (e.g., `0.1.6-test`). This is useful for testing the publishing process without affecting the main version.
