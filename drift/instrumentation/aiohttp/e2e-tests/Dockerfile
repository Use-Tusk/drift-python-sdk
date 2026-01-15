FROM python-e2e-base:latest

# Copy SDK source for editable install
COPY . /sdk

# Copy test files
COPY drift/instrumentation/aiohttp/e2e-tests /app

WORKDIR /app

# Install dependencies (requirements.txt uses -e /sdk for SDK)
RUN pip install -q -r requirements.txt

# Make entrypoint executable
RUN chmod +x entrypoint.py

# Create .tusk directories
RUN mkdir -p /app/.tusk/traces /app/.tusk/logs

# Run entrypoint
ENTRYPOINT ["python", "entrypoint.py"]

