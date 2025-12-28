FROM python-e2e-base:latest

# Copy SDK source (for development)
COPY python /sdk

# Copy test files
COPY python/drift/instrumentation/flask/e2e-tests /app

WORKDIR /app

# Install dependencies
RUN pip install -q -r requirements.txt

# Make entrypoint executable
RUN chmod +x entrypoint.py

# Run entrypoint
ENTRYPOINT ["python", "entrypoint.py"]
