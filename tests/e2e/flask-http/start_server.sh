#!/bin/bash

# Exit on error
set -e

echo "Starting Flask server in RECORD mode..."

# Start Flask server in background
export TUSK_DRIFT_MODE=RECORD
python app.py &
SERVER_PID=$!

# Wait for server to be ready
echo "Waiting for server to be ready..."
max_attempts=30
attempt=0

while [ $attempt -lt $max_attempts ]; do
    if curl -s http://localhost:5000/health > /dev/null 2>&1; then
        echo "Server is ready!"
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done

if [ $attempt -eq $max_attempts ]; then
    echo "Server failed to start within 30 seconds"
    kill $SERVER_PID 2>/dev/null || true
    exit 1
fi

# Hit all endpoints to generate traces
echo "Generating traces by hitting endpoints..."
python test_requests.py http://localhost:5000

# Wait a bit for spans to be collected
echo "Waiting for spans to be collected..."
sleep 3

echo "RECORD mode complete. Server is still running."
echo "Server PID: $SERVER_PID"

# Keep server running (don't exit)
wait $SERVER_PID
