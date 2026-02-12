#!/usr/bin/env python3
"""
E2E Test Entrypoint for SQLAlchemy Instrumentation.
"""

import os
import sys

# Add SDK to path for imports
sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import Colors, E2ETestRunnerBase


class SqlAlchemyE2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for SQLAlchemy instrumentation with Postgres setup."""

    def __init__(self):
        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def setup(self):
        """Phase 1: Setup dependencies and wait for Postgres."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 1: Setup", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        self.log("Installing Python dependencies...", Colors.BLUE)
        self.run_command(["pip", "install", "-q", "-r", "requirements.txt"])

        self.log("Waiting for Postgres...", Colors.BLUE)
        pg_host = os.getenv("POSTGRES_HOST", "postgres")
        pg_user = os.getenv("POSTGRES_USER", "testuser")
        pg_db = os.getenv("POSTGRES_DB", "testdb")

        if not self.wait_for_service(["pg_isready", "-h", pg_host, "-U", pg_user, "-d", pg_db], timeout=30):
            self.log("Postgres failed to become ready", Colors.RED)
            raise TimeoutError("Postgres not ready")

        self.log("Postgres is ready", Colors.GREEN)
        self.log("Setup complete", Colors.GREEN)


if __name__ == "__main__":
    runner = SqlAlchemyE2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
