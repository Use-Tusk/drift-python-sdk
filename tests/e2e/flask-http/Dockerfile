FROM python:3.12-slim

WORKDIR /app

# Install curl for health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install tusk CLI
ARG TUSK_CLI_VERSION=latest
RUN curl -fsSL https://get.tusk.dev | bash -s -- --version ${TUSK_CLI_VERSION} || \
    echo "Warning: tusk CLI installation skipped (may not be available yet)"

# Copy SDK source
COPY . /sdk

# Install SDK dependencies
RUN pip install --no-cache-dir -r /sdk/python/requirements.txt

# Set Python path to include SDK
ENV PYTHONPATH=/sdk/python:$PYTHONPATH

# Copy application files
COPY python/tests/e2e/flask-http/app.py /app/
COPY python/tests/e2e/flask-http/.tusk /app/.tusk

# Default command (will be overridden by run.sh)
CMD ["tail", "-f", "/dev/null"]
