#!/usr/bin/env python3
"""
E2E Test Entrypoint for Psycopg2 Instrumentation

This script orchestrates the full e2e test lifecycle:
1. Setup: Install dependencies, initialize database schema
2. Record: Start app in RECORD mode, execute requests
3. Test: Run Tusk CLI tests
4. Teardown: Cleanup and return exit code
"""

import os
import sys

# Add SDK to path for imports
sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import Colors, E2ETestRunnerBase


class Psycopg2E2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for Psycopg2 instrumentation with Postgres setup."""

    def __init__(self):
        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def setup(self):
        """Phase 1: Setup dependencies and database."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 1: Setup", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        # Install Python dependencies
        self.log("Installing Python dependencies...", Colors.BLUE)
        self.run_command(["pip", "install", "-q", "-r", "requirements.txt"])

        # Wait for Postgres to be ready
        self.log("Waiting for Postgres...", Colors.BLUE)
        pg_host = os.getenv("POSTGRES_HOST", "postgres")
        pg_user = os.getenv("POSTGRES_USER", "testuser")
        pg_db = os.getenv("POSTGRES_DB", "testdb")

        if not self.wait_for_service(["pg_isready", "-h", pg_host, "-U", pg_user, "-d", pg_db], timeout=30):
            self.log("Postgres failed to become ready", Colors.RED)
            raise TimeoutError("Postgres not ready")

        self.log("Postgres is ready", Colors.GREEN)

        # Initialize database schema
        self.log("Initializing database schema...", Colors.BLUE)
        pg_password = os.getenv("POSTGRES_PASSWORD", "testpass")
        env = {"PGPASSWORD": pg_password}

        schema_sql = """
            DROP TABLE IF EXISTS users CASCADE;
            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            INSERT INTO users (name, email) VALUES
                ('John Doe', 'john@example.com'),
                ('Jane Smith', 'jane@example.com');
        """

        self.run_command(["psql", "-h", pg_host, "-U", pg_user, "-d", pg_db, "-c", schema_sql], env=env)

        self.log("Database schema initialized", Colors.GREEN)
        self.log("Setup complete", Colors.GREEN)


if __name__ == "__main__":
    runner = Psycopg2E2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
