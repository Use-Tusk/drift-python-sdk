# Drift Python SDK

## Installation

```bash
python -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

Alternatively, if you have [direnv](https://direnv.net/), just allow this folder and run `pip install -r requirements.txt`.

## Running Tests

```bash
# Run all unit tests
python -m unittest discover -s tests/unit -v

# Run all integration tests (Flask/FastAPI)
timeout 30 python -m unittest discover -s tests/integration -v

# Run specific test file
python -m unittest tests.unit.test_json_schema_helper -v
python -m unittest tests.unit.test_adapters -v
```

### Database Integration Tests

```bash
# Start test databases
docker compose -f docker-compose.test.yml up -d

# Run database tests
python -m unittest tests.integration.test_database -v

# Stop databases
docker compose -f docker-compose.test.yml down
```

### Demo Scripts

```bash
timeout 10 python tests/test_flask_demo.py
timeout 10 python tests/test_fastapi_demo.py
```
