FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install tusk CLI

RUN curl -fsSL https://raw.githubusercontent.com/Use-Tusk/tusk-drift-cli/main/install.sh | sh

# Copy SDK source (context is python/ directory)
COPY . /sdk

# Install SDK dependencies
RUN pip install -r /sdk/requirements.txt

# Set Python path
ENV PYTHONPATH=/sdk:$PYTHONPATH

# Copy app file
COPY tests/e2e/flask-http/app.py /app/

# will be overridden by run.sh
CMD ["tail", "-f", "/dev/null"]
