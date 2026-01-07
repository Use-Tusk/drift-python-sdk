#!/usr/bin/env python3
"""
Base E2E Test Runner for Python SDK Instrumentations

This module provides a reusable base class for e2e test orchestration.
Each instrumentation's entrypoint.py can inherit from this class and
customize only the setup phase.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Colors:
    """ANSI color codes for terminal output."""

    GREEN = "\033[0;32m"
    RED = "\033[0;31m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"  # No Color


class E2ETestRunnerBase:
    """
    Base class for e2e test orchestration.

    Subclasses should override:
    - setup(): To add instrumentation-specific setup (e.g., database schema)

    The base class provides:
    - Signal handling for graceful cleanup
    - Application lifecycle management
    - Trace recording and verification
    - Tusk CLI test execution
    - Result parsing and reporting
    """

    def __init__(self, app_port: int = 8000):
        self.app_port = app_port
        self.app_process: subprocess.Popen | None = None
        self.exit_code = 0

        # Register signal handlers for cleanup
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle termination signals gracefully."""
        print(f"\n{Colors.YELLOW}Received signal {signum}, cleaning up...{Colors.NC}")
        self.cleanup()
        sys.exit(1)

    def log(self, message: str, color: str = Colors.NC):
        """Print colored log message."""
        print(f"{color}{message}{Colors.NC}", flush=True)

    def run_command(self, cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a command and return result."""
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        result = subprocess.run(cmd, env=full_env, capture_output=True, text=True)

        if check and result.returncode != 0:
            self.log(f"Command failed: {' '.join(cmd)}", Colors.RED)
            self.log(f"stdout: {result.stdout}", Colors.RED)
            self.log(f"stderr: {result.stderr}", Colors.RED)
            raise subprocess.CalledProcessError(result.returncode, cmd)

        return result

    def wait_for_service(self, check_cmd: list[str], timeout: int = 30, interval: int = 1) -> bool:
        """Wait for a service to become ready."""
        elapsed = 0
        last_error = None
        while elapsed < timeout:
            try:
                result = subprocess.run(check_cmd, capture_output=True, timeout=5, text=True)
                if result.returncode == 0:
                    return True
                last_error = f"returncode={result.returncode}, stderr={result.stderr}"
            except subprocess.TimeoutExpired:
                last_error = "timeout"
            except subprocess.CalledProcessError as e:
                last_error = str(e)

            time.sleep(interval)
            elapsed += interval

        self.log(f"Service check failed after {timeout}s. Last error: {last_error}", Colors.RED)
        raise TimeoutError(f"Service not ready after {timeout}s")

    def setup(self):
        """
        Phase 1: Setup dependencies and services.

        Override this method in subclasses to add instrumentation-specific setup
        (e.g., database schema initialization, external service setup).
        """
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 1: Setup", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        # Install Python dependencies
        self.log("Installing Python dependencies...", Colors.BLUE)
        self.run_command(["pip", "install", "-q", "-r", "requirements.txt"])

        self.log("Setup complete", Colors.GREEN)

    def record_traces(self) -> bool:
        """Phase 2: Start app and record traces."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 2: Recording Traces", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        # Clear existing traces
        traces_dir = Path(".tusk/traces")
        logs_dir = Path(".tusk/logs")

        if traces_dir.exists():
            for f in traces_dir.glob("*.jsonl"):
                f.unlink()
        if logs_dir.exists():
            for f in logs_dir.glob("*"):
                f.unlink()

        # Start application in RECORD mode
        self.log("Starting application in RECORD mode...", Colors.GREEN)
        env = {"TUSK_DRIFT_MODE": "RECORD", "PYTHONUNBUFFERED": "1"}

        self.app_process = subprocess.Popen(
            ["python", "src/app.py"],
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # Wait for app to be ready
        self.log("Waiting for application to be ready...", Colors.BLUE)
        try:
            self.wait_for_service(
                ["curl", "-fsS", f"http://localhost:{self.app_port}/health"],
                timeout=30,
            )
            self.log("Application is ready", Colors.GREEN)
        except TimeoutError:
            self.log("Application failed to become ready", Colors.RED)
            if self.app_process:
                # Print app output for debugging
                self.app_process.terminate()
                stdout, _ = self.app_process.communicate(timeout=5)
                self.log(f"App output: {stdout}", Colors.YELLOW)
            self.exit_code = 1
            return False

        # Execute test requests
        self.log("Executing test requests...", Colors.GREEN)
        try:
            self.run_command(["python", "src/test_requests.py"])
        except subprocess.CalledProcessError:
            self.log("Test requests failed", Colors.RED)
            self.exit_code = 1
            return False

        # Wait for traces to flush
        self.log("Waiting for traces to flush...", Colors.YELLOW)
        time.sleep(3)

        # Stop application
        self.log("Stopping application...", Colors.YELLOW)
        if self.app_process:
            self.app_process.terminate()
            try:
                self.app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.app_process.kill()
                self.app_process.wait()
            self.app_process = None

        # Verify traces were created
        trace_files = list(traces_dir.glob("*.jsonl"))
        self.log(f"Recorded {len(trace_files)} trace files", Colors.GREEN)

        if len(trace_files) == 0:
            self.log("ERROR: No traces recorded!", Colors.RED)
            self.exit_code = 1
            return False

        return True

    def run_tests(self):
        """Phase 3: Run Tusk CLI tests."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 3: Running Tusk Tests", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        env = {"TUSK_ANALYTICS_DISABLED": "1"}

        result = self.run_command(
            ["tusk", "run", "--print", "--output-format", "json", "--enable-service-logs"],
            env=env,
            check=False,
        )

        # Debug: show what tusk run returned
        self.log(f"tusk run exit code: {result.returncode}", Colors.YELLOW)
        if result.stdout:
            self.log(f"tusk run stdout:\n{result.stdout}", Colors.YELLOW)
        if result.stderr:
            self.log(f"tusk run stderr:\n{result.stderr}", Colors.YELLOW)

        # Parse JSON results
        self.parse_test_results(result.stdout)

        if result.returncode != 0:
            self.exit_code = 1

    def parse_test_results(self, output: str):
        """Parse and display test results."""
        self.log("=" * 50)
        self.log("Test Results:")
        self.log("=" * 50)

        try:
            # Extract JSON objects from output (handles pretty-printed JSON)
            results = []
            decoder = json.JSONDecoder()
            idx = 0
            output = output.strip()

            while idx < len(output):
                # Skip whitespace
                while idx < len(output) and output[idx] in " \t\n\r":
                    idx += 1
                if idx >= len(output):
                    break

                # Try to decode a JSON object starting at idx
                if output[idx] == "{":
                    try:
                        obj, end_idx = decoder.raw_decode(output, idx)
                        if isinstance(obj, dict) and "test_id" in obj:
                            results.append(obj)
                        idx += end_idx
                    except json.JSONDecodeError:
                        idx += 1
                else:
                    idx += 1

            all_passed = True
            for result in results:
                test_id = result.get("test_id", "unknown")
                passed = result.get("passed", False)
                duration = result.get("duration", 0)

                if passed:
                    self.log(f"✓ Test ID: {test_id} (Duration: {duration}ms)", Colors.GREEN)
                else:
                    self.log(f"✗ Test ID: {test_id} (Duration: {duration}ms)", Colors.RED)
                    all_passed = False

            if all_passed and len(results) > 0:
                self.log("All tests passed!", Colors.GREEN)
            elif len(results) == 0:
                self.log("No test results found", Colors.YELLOW)
            else:
                self.log("Some tests failed!", Colors.RED)
                self.exit_code = 1

        except Exception as e:
            self.log(f"Failed to parse test results: {e}", Colors.RED)
            self.log(f"Raw output:\n{output}", Colors.YELLOW)
            self.exit_code = 1

    def cleanup(self):
        """Phase 4: Cleanup resources."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 4: Cleanup", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        # Stop app process if still running
        if self.app_process:
            self.log("Stopping application process...", Colors.YELLOW)
            self.app_process.terminate()
            try:
                self.app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.app_process.kill()
                self.app_process.wait()

        # Traces are kept in volume for inspection
        self.log("Cleanup complete", Colors.GREEN)

    def run(self) -> int:
        """Run the full e2e test lifecycle."""
        try:
            self.setup()

            if not self.record_traces():
                return 1

            self.run_tests()

            return self.exit_code

        except Exception as e:
            self.log(f"Test failed with exception: {e}", Colors.RED)
            import traceback

            traceback.print_exc()
            return 1

        finally:
            self.cleanup()


if __name__ == "__main__":
    # Can be run directly for testing
    runner = E2ETestRunnerBase()
    exit_code = runner.run()
    sys.exit(exit_code)
