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

### Demo Scripts

```bash
timeout 10 uv run python tests/test_flask_demo.py
timeout 10 uv run python tests/test_fastapi_demo.py
```
