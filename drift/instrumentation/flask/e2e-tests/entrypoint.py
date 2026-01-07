#!/usr/bin/env python3
"""
E2E Test Entrypoint for Flask Instrumentation

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

from drift.instrumentation.e2e_common.base_runner import E2ETestRunnerBase


class FlaskE2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for Flask instrumentation."""

    def __init__(self):
        import os
        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)


if __name__ == "__main__":
    runner = FlaskE2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
