#!/usr/bin/env python3
"""Script to record traces by starting Flask server and hitting endpoints.

This script:
1. Starts the Flask server in RECORD mode using multiprocessing
2. Waits for the server to be ready
3. Hits all test endpoints to generate traces
4. Stops the server cleanly
"""

import os
import sys
import time
import signal
from multiprocessing import Process
import requests


def start_flask_server():
    """Start the Flask server in a subprocess."""
    # Set RECORD mode
    os.environ["TUSK_DRIFT_MODE"] = "RECORD"

    # Import and run the Flask app
    import app
    port = int(os.environ.get("PORT", 5000))
    app.sdk.mark_app_as_ready()
    app.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def wait_for_server(base_url: str = "http://localhost:5000", max_attempts: int = 30) -> bool:
    """Wait for the Flask server to be ready.

    Args:
        base_url: Base URL of the Flask server
        max_attempts: Maximum number of attempts

    Returns:
        True if server is ready, False otherwise
    """
    print("Waiting for server to be ready...")
    for attempt in range(max_attempts):
        try:
            response = requests.get(f"{base_url}/health", timeout=1)
            if response.status_code == 200:
                print("Server is ready!")
                return True
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(1)

    print(f"Server failed to start within {max_attempts} seconds")
    return False


def hit_endpoints(base_url: str = "http://localhost:5000") -> bool:
    """Hit all test endpoints to generate traces.

    Args:
        base_url: Base URL of the Flask server

    Returns:
        True if all requests succeeded, False otherwise
    """
    endpoints = [
        ("GET", "/health", None, None),
        ("GET", "/test-http-get", None, None),
        ("POST", "/test-http-post", {"title": "test", "body": "test body"}, None),
        ("PUT", "/test-http-put", {"id": 1, "title": "updated"}, None),
        ("DELETE", "/test-http-delete", None, None),
        ("GET", "/test-http-headers", None, None),
        ("GET", "/test-http-query-params", None, None),
        ("GET", "/test-http-error", None, None),
        ("GET", "/test-chained-requests", None, None),
        ("GET", "/greet/World", None, {"greeting": "Hi"}),
        ("POST", "/echo", {"message": "hello"}, None),
    ]

    success_count = 0
    failure_count = 0

    print(f"Hitting all Flask HTTP endpoints at {base_url}...")

    for method, path, json_body, params in endpoints:
        url = f"{base_url}{path}"
        print(f"  - {method} {path}")

        try:
            if method == "GET":
                response = requests.get(url, params=params, timeout=10)
            elif method == "POST":
                response = requests.post(url, json=json_body, timeout=10)
            elif method == "PUT":
                response = requests.put(url, json=json_body, timeout=10)
            elif method == "DELETE":
                response = requests.delete(url, timeout=10)
            else:
                print(f"    ❌ Unknown method: {method}")
                failure_count += 1
                continue

            if response.status_code < 400:
                print(f"    ✓ Status: {response.status_code}")
                success_count += 1
            else:
                print(f"    ❌ Status: {response.status_code}")
                failure_count += 1

        except Exception as e:
            print(f"    ❌ Error: {e}")
            failure_count += 1

    print(f"\nCompleted: {success_count} succeeded, {failure_count} failed")
    return failure_count == 0


def main():
    """Main function to orchestrate the recording process."""
    print("Starting Flask server in RECORD mode...")

    # Start Flask server in a separate process
    server_process = Process(target=start_flask_server)
    server_process.start()

    try:
        # Wait for server to be ready
        if not wait_for_server():
            print("Failed to start server")
            sys.exit(1)

        # Hit all endpoints to generate traces
        print("Generating traces by hitting endpoints...")
        success = hit_endpoints()

        # Wait for spans to be collected
        print("Waiting for spans to be collected...")
        time.sleep(3)

        print("RECORD mode complete. Traces generated successfully.")

        # Create completion marker for healthcheck
        open("/app/.record_complete", "w").close()

        return 0 if success else 1

    finally:
        # Stop the server
        print("Stopping Flask server...")
        server_process.terminate()
        server_process.join(timeout=5)
        if server_process.is_alive():
            print("Force killing server...")
            server_process.kill()
            server_process.join()


if __name__ == "__main__":
    sys.exit(main())
