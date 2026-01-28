# Stack Tests

This directory contains full-stack end-to-end tests that validate multiple instrumentations working together. These tests cover common real-world technology stacks that customers use.

## What Are Stack Tests?

Stack tests are more comprehensive than single-instrumentation e2e tests. While e2e tests (in each instrumentation's `e2e-tests/` folder) verify individual instrumentations in isolation, stack tests validate how multiple instrumentations interact when combined in realistic application architectures.

These tests exercise complete technology stacks (e.g., Django + PostgreSQL, FastAPI + Redis) rather than individual components.

## Available Stack Tests

| Test | Description | Components |
|------|-------------|------------|
| `django-postgres/` | Django with PostgreSQL database | Django middleware + psycopg2 |
| `fastapi-postgres/` | FastAPI with async PostgreSQL | FastAPI + psycopg (async) |
| `django-redis/` | Django with Redis cache/sessions | Django middleware + Redis |

## Why Stack Tests?

Bugs can occur at the integration points between libraries that don't appear in isolated testing. For example:

- **Django + psycopg2**: Django's PostgreSQL backend calls `register_default_jsonb()` on the connection after `connect()`. If the SDK wraps connections in a way that breaks type checks, this fails.
- **FastAPI + psycopg**: Async context propagation between FastAPI's async handlers and psycopg's async database operations.
- **Django + Redis**: Session middleware interacting with Redis instrumentation.

## Running Tests

Each stack test can be run independently:

```bash
# Run a specific stack test
cd django-postgres && ./run.sh

# Or from the SDK root
cd drift/stack-tests/django-postgres && ./run.sh
```

Or run all tests (e2e + stack) from the SDK root:

```bash
./run-all-e2e-tests.sh
```

## Test Structure

Each stack test follows the same structure as single-instrumentation e2e tests:

```text
<test-name>/
├── docker-compose.yml    # Services (app + dependencies)
├── Dockerfile            # App container build
├── entrypoint.py         # Test orchestration (extends E2ETestRunnerBase)
├── requirements.txt      # Python dependencies (includes -e /sdk)
├── run.sh                # Test runner script
├── .tusk/                # Tusk config and trace storage
│   └── config.yaml
└── src/
    ├── app.py            # Application code
    ├── settings.py       # Framework settings (Django)
    ├── urls.py           # URL routing (Django)
    ├── views.py          # View handlers (Django)
    └── test_requests.py  # Test request sequence
```

## Adding New Stack Tests

1. Create a new directory with the naming pattern `<framework>-<dependency>/`
2. Copy the structure from an existing stack test
3. Modify the app and test requests to exercise the specific integration
4. Add the test to the CI pipeline
