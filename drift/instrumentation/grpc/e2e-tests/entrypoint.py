#!/usr/bin/env python3
"""
E2E Test Entrypoint for gRPC Instrumentation

This script orchestrates the full e2e test lifecycle:
1. Setup: Install dependencies, generate proto files
2. Record: Start app in RECORD mode, execute requests
3. Test: Run Tusk CLI tests
4. Teardown: Cleanup and return exit code
"""

import sys
from pathlib import Path

# Add SDK to path for imports
sys.path.insert(0, "/sdk")

from drift.instrumentation.e2e_common.base_runner import E2ETestRunnerBase


class GrpcE2ETestRunner(E2ETestRunnerBase):
    """E2E test runner for gRPC instrumentation."""

    def __init__(self):
        import os

        port = int(os.getenv("PORT", "8000"))
        super().__init__(app_port=port)

    def setup(self):
        """Phase 1: Setup dependencies and generate proto files."""
        self.log("=" * 50, self.Colors.BLUE)
        self.log("Phase 1: Setup", self.Colors.BLUE)
        self.log("=" * 50, self.Colors.BLUE)

        self.log("Installing Python dependencies...", self.Colors.BLUE)
        self.run_command(["pip", "install", "-q", "-r", "requirements.txt"])

        # Generate proto files
        self.log("Generating proto files...", self.Colors.BLUE)
        self.run_command(
            [
                "python",
                "-m",
                "grpc_tools.protoc",
                "-I",
                "src/proto",
                "--python_out=src",
                "--grpc_python_out=src",
                "src/proto/greeter.proto",
            ]
        )

        self.log("Setup complete", self.Colors.GREEN)

    # Use Colors from base class
    @property
    def Colors(self):
        from drift.instrumentation.e2e_common.base_runner import Colors

        return Colors


if __name__ == "__main__":
    runner = GrpcE2ETestRunner()
    exit_code = runner.run()
    sys.exit(exit_code)
