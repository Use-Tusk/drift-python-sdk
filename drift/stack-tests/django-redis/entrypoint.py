#!/usr/bin/env python3
"""
E2E Test Entrypoint for Django + Redis Test

This script orchestrates the full e2e test lifecycle:
1. Setup: Install dependencies, wait for Redis
2. Record: Start app in RECORD mode, execute requests
3. Test: Run Tusk CLI tests
4. Teardown: Cleanup and return exit code
"""

import os
import sys

# Add SDK to path for imports
sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import Colors, E2ETestRunnerBase


class DjangoRedisE2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for Django + Redis test."""

    def __init__(self):
        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def setup(self):
        """Phase 1: Setup dependencies and wait for Redis."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 1: Setup", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        # Install Python dependencies
        self.log("Installing Python dependencies...", Colors.BLUE)
        self.run_command(["pip", "install", "-q", "-r", "requirements.txt"])

        # Wait for Redis to be ready using Python
        self.log("Waiting for Redis...", Colors.BLUE)
        redis_host = os.getenv("REDIS_HOST", "redis")
        redis_port = os.getenv("REDIS_PORT", "6379")

        # Use Python to check Redis instead of redis-cli
        check_script = f'''
import redis
import sys
try:
    r = redis.Redis(host="{redis_host}", port={redis_port})
    r.ping()
    sys.exit(0)
except Exception:
    sys.exit(1)
'''
        if not self.wait_for_service(["python", "-c", check_script], timeout=30):
            self.log("Redis failed to become ready", Colors.RED)
            raise TimeoutError("Redis not ready")

        self.log("Redis is ready", Colors.GREEN)
        self.log("Setup complete", Colors.GREEN)


if __name__ == "__main__":
    runner = DjangoRedisE2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
