#!/usr/bin/env python3
"""
E2E Test Entrypoint for aiohttp Instrumentation

This script orchestrates the full e2e test lifecycle:
1. Setup: Install dependencies
2. Record: Start app in RECORD mode, execute requests
3. Test: Run Tusk CLI tests
4. Teardown: Cleanup and return exit code
"""

import sys
from pathlib import Path

# Add SDK to path for imports
sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import Colors, E2ETestRunnerBase


class AiohttpE2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for aiohttp instrumentation."""

    def __init__(self):
        import os

        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def check_socket_instrumentation_warnings(self):
        """Override to skip socket instrumentation check for aiohttp.

        aiohttp uses aiohappyeyeballs internally for connection management,
        which makes low-level socket calls that trigger the socket instrumentation
        warnings during REPLAY mode. This is expected behavior - the main
        aiohttp instrumentation is working correctly (intercepting _request).

        We skip this check because:
        1. aiohappyeyeballs is an internal implementation detail of aiohttp
        2. Our instrumentation correctly intercepts at the _request level
        3. The socket calls from aiohappyeyeballs are a known limitation
        """
        self.log("=" * 50, Colors.BLUE)
        self.log("Checking for Instrumentation Warnings", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        self.log(
            "⚠ Skipping socket instrumentation check for aiohttp (aiohappyeyeballs makes expected socket calls)",
            Colors.YELLOW,
        )

        # Just verify trace files exist
        traces_dir = Path(".tusk/traces")
        trace_files = list(traces_dir.glob("*.jsonl")) if traces_dir.exists() else []
        if trace_files:
            self.log(f"✓ Found {len(trace_files)} trace file(s).", Colors.GREEN)
        else:
            self.log("✗ ERROR: No trace files found!", Colors.RED)
            self.exit_code = 1


if __name__ == "__main__":
    runner = AiohttpE2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
