FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install tusk CLI
ARG TUSK_CLI_VERSION=latest
RUN curl -fsSL https://get.tusk.dev | bash -s -- --version ${TUSK_CLI_VERSION} || \
    echo "Warning: tusk CLI installation skipped (may not be available yet)"

# Copy SDK source (context is python/ directory)
COPY . /sdk

# Install SDK dependencies
RUN pip install --no-cache-dir -r /sdk/requirements.txt

# Set Python path
ENV PYTHONPATH=/sdk:$PYTHONPATH

# Copy app file
COPY tests/e2e/flask-http/app.py /app/

# will be overridden by run.sh
CMD ["tail", "-f", "/dev/null"]
