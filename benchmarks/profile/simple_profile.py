#!/usr/bin/env python3
"""Simple profiling script for the SDK.

This script can be used with various profilers:

1. py-spy (flame graphs):
   py-spy record -o profile.svg -- python benchmarks/profile/simple_profile.py

2. cProfile (call stats):
   python -m cProfile -o profile.prof benchmarks/profile/simple_profile.py
   # Then view with: python -m pstats profile.prof

3. scalene (CPU/memory):
   scalene benchmarks/profile/simple_profile.py
"""

import os
import sys
import time

# Set SDK to RECORD mode
os.environ["TUSK_DRIFT_MODE"] = "RECORD"

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pathlib import Path

import requests

from drift import TuskDrift
from drift.core.tracing.adapters.filesystem import FilesystemSpanAdapter

# Setup traces directory
PROFILE_TRACE_DIR = Path(__file__).parent / "results" / "traces"
PROFILE_TRACE_DIR.mkdir(parents=True, exist_ok=True)

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="profile-test-key",
    env="profile",
    log_level="warn",
)

if sdk.span_exporter:
    sdk.span_exporter.clear_adapters()
    adapter = FilesystemSpanAdapter(base_directory=PROFILE_TRACE_DIR)
    sdk.span_exporter.add_adapter(adapter)

sdk.mark_app_as_ready()

# Import server after SDK init
from benchmarks.server.test_server import TestServer


def main():
    """Run profiling workload."""
    # Start test server
    server = TestServer()
    server_info = server.start()
    server_url = server_info["url"]

    # Wait for server
    time.sleep(0.5)

    # Use session for connection pooling
    session = requests.Session()

    try:
        print(f"\nTest server started at {server_url}")
        print("Running profiling workload...")

        # Number of iterations (adjust for longer/shorter profiling)
        iterations = 500

        start_time = time.time()

        for i in range(iterations):
            # Mix of different request types
            if i % 3 == 0:
                # Simple GET
                response = session.get(f"{server_url}/api/simple")
                response.json()
            elif i % 3 == 1:
                # POST with JSON body
                response = session.post(
                    f"{server_url}/api/echo",
                    json={"data": "test", "iteration": i},
                )
                response.json()
            else:
                # Sensitive data endpoint (triggers transform checks)
                response = session.post(
                    f"{server_url}/api/auth/login",
                    json={"email": "test@example.com", "password": "secret123"},
                )
                response.json()

            if (i + 1) % 100 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed
                print(f"  {i + 1}/{iterations} iterations ({rate:.1f} req/s)")

        elapsed = time.time() - start_time
        print(f"\nCompleted {iterations} iterations in {elapsed:.2f}s")
        print(f"Average: {iterations / elapsed:.1f} req/s")
    finally:
        # Cleanup - always execute even if an exception is raised
        session.close()
        server.stop()
        sdk.shutdown()


if __name__ == "__main__":
    main()
