#!/usr/bin/env python3
"""E2E test entrypoint for FastAPI + SQLAlchemy stack test."""

import os
import sys

sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import Colors, E2ETestRunnerBase


class FastAPISqlAlchemyE2ETestRunner(E2ETestRunnerBase):
    """E2E runner for FastAPI + SQLAlchemy stack test."""

    def __init__(self):
        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def _reset_schema(self):
        pg_host = os.getenv("POSTGRES_HOST", "postgres")
        pg_user = os.getenv("POSTGRES_USER", "testuser")
        pg_db = os.getenv("POSTGRES_DB", "testdb")
        pg_password = os.getenv("POSTGRES_PASSWORD", "testpass")
        env = {"PGPASSWORD": pg_password}

        schema_sql = """
            DROP TABLE IF EXISTS tasks CASCADE;
            DROP TABLE IF EXISTS projects CASCADE;
            DROP TABLE IF EXISTS users CASCADE;

            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                email VARCHAR(255) NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL
            );

            CREATE TABLE projects (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL
            );

            CREATE TABLE tasks (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                title VARCHAR(200) NOT NULL,
                description TEXT,
                status VARCHAR(30) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            );

            INSERT INTO users (name, email, created_at)
            VALUES ('Project Manager', 'pm@example.com', NOW());

            INSERT INTO projects (name, owner_id, created_at)
            VALUES ('SDK Integration Rollout', 1, NOW());

            INSERT INTO tasks (project_id, title, description, status, created_at, updated_at)
            VALUES
                (1, 'Draft onboarding docs', 'Publish migration notes for existing teams', 'done', NOW(), NOW()),
                (1, 'Wire telemetry dashboard', 'Expose replay pass-rate in analytics', 'in_progress', NOW(), NOW());
        """
        self.run_command(["psql", "-h", pg_host, "-U", pg_user, "-d", pg_db, "-c", schema_sql], env=env)

    def setup(self):
        """Phase 1: Setup dependencies and database."""
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
            raise TimeoutError("Postgres not ready")

        self.log("Resetting schema...", Colors.BLUE)
        self._reset_schema()

        self.log("Setup complete", Colors.GREEN)

    def run_tests(self):
        """Phase 3: Run Tusk CLI tests from a clean DB state."""
        self.log("Resetting schema before replay...", Colors.BLUE)
        self._reset_schema()
        super().run_tests()


if __name__ == "__main__":
    runner = FastAPISqlAlchemyE2ETestRunner()
    sys.exit(runner.run())
