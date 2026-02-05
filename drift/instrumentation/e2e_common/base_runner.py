#!/usr/bin/env python3
"""
Base E2E Test Runner for Python SDK Instrumentations

This module provides a reusable base class for e2e test orchestration.
Each instrumentation's entrypoint.py can inherit from this class and
customize only the setup phase.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
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
    - Benchmark mode (BENCHMARKS env var)
    """

    def __init__(self, app_port: int = 8000):
        self.app_port = app_port
        self.app_process: subprocess.Popen | None = None
        self.app_log_file: tempfile._TemporaryFileWrapper | None = None
        self.exit_code = 0
        self.expected_request_count: int | None = None

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

    def _parse_request_count(self, output: str):
        """Parse the request count from test_requests.py output."""
        for line in output.split("\n"):
            if line.startswith("TOTAL_REQUESTS_SENT:"):
                try:
                    count = int(line.split(":")[1])
                    self.expected_request_count = count
                    self.log(f"Captured request count: {count}", Colors.GREEN)
                except (ValueError, IndexError):
                    self.log(f"Failed to parse request count from: {line}", Colors.YELLOW)

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

    def _start_app(self, mode: str) -> bool:
        """Start the application with the given TUSK_DRIFT_MODE and wait for it to be ready.

        Returns True if the app started successfully, False otherwise.
        """
        self.log(f"Starting application with TUSK_DRIFT_MODE={mode}", Colors.GREEN)
        app_env = {**os.environ, "TUSK_DRIFT_MODE": mode, "PYTHONUNBUFFERED": "1"}

        # Use a temporary file to capture app output for debugging.
        # Note: Can't use context manager here - file must stay open for subprocess
        # and be cleaned up later in cleanup(). Using delete=False + manual unlink.
        self.app_log_file = tempfile.NamedTemporaryFile(mode="w+", suffix=".log", delete=False)  # noqa: SIM115

        self.app_process = subprocess.Popen(
            ["python", "src/app.py"],
            env=app_env,
            stdout=self.app_log_file,
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

            # Show early app output for diagnostics (SDK init messages, mode confirmation)
            if self.app_log_file:
                self.app_log_file.flush()
                self.app_log_file.seek(0)
                early_output = self.app_log_file.read(2048)
                if early_output.strip():
                    self.log(f"App startup output:\n{early_output.rstrip()}", Colors.YELLOW)

            return True
        except TimeoutError:
            self.log("Application failed to become ready", Colors.RED)
            if self.app_process:
                self.app_process.terminate()
                try:
                    self.app_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.app_process.kill()
                    self.app_process.wait()
            # Read and display app output for debugging
            if self.app_log_file:
                self.app_log_file.flush()
                self.app_log_file.seek(0)
                app_output = self.app_log_file.read()
                self.log(f"App output:\n{app_output}", Colors.YELLOW)
            return False

    def _stop_app(self):
        """Stop the running application process and clean up the log file."""
        self.log("Stopping application...", Colors.YELLOW)
        if self.app_process:
            self.app_process.terminate()
            try:
                self.app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.app_process.kill()
                self.app_process.wait()
            self.app_process = None

        if self.app_log_file:
            try:
                self.app_log_file.close()
                os.unlink(self.app_log_file.name)
            except OSError:
                pass
            self.app_log_file = None

    def _run_test_requests(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """Run test_requests.py and return the completed process result."""
        env = {"PYTHONPATH": "/sdk"}
        if extra_env:
            env.update(extra_env)
        return self.run_command(
            ["python", "src/test_requests.py"],
            env=env,
        )

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

        if not self._start_app(mode="RECORD"):
            self.exit_code = 1
            return False

        # Execute test requests
        self.log("Executing test requests...", Colors.GREEN)
        try:
            result = self._run_test_requests()
            # Parse request count from output
            self._parse_request_count(result.stdout)
        except subprocess.CalledProcessError:
            self.log("Test requests failed", Colors.RED)
            self.exit_code = 1
            return False

        # Wait for traces to flush
        self.log("Waiting for traces to flush...", Colors.YELLOW)
        time.sleep(3)

        # Stop application
        self._stop_app()

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
                        # raw_decode returns absolute index, not relative offset
                        idx = end_idx
                    except json.JSONDecodeError:
                        idx += 1
                else:
                    idx += 1

            all_passed = True
            passed_count = 0
            for result in results:
                test_id = result.get("test_id", "unknown")
                passed = result.get("passed", False)
                duration = result.get("duration", 0)

                if passed:
                    self.log(f"✓ Test ID: {test_id} (Duration: {duration}ms)", Colors.GREEN)
                    passed_count += 1
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

            # Validate request count matches passed tests
            if self.expected_request_count is not None:
                if passed_count < self.expected_request_count:
                    self.log(
                        f"✗ Request count mismatch: {passed_count} passed tests != {self.expected_request_count} requests sent",
                        Colors.RED,
                    )
                    self.exit_code = 1
                else:
                    self.log(
                        f"✓ Request count validation: {passed_count} passed tests >= {self.expected_request_count} requests sent",
                        Colors.GREEN,
                    )

        except Exception as e:
            self.log(f"Failed to parse test results: {e}", Colors.RED)
            self.log(f"Raw output:\n{output}", Colors.YELLOW)
            self.exit_code = 1

    def check_socket_instrumentation_warnings(self):
        """
        Check for socket instrumentation warnings in logs.

        This detects unpatched dependencies - libraries making TCP calls
        from within a SERVER span context without proper instrumentation.
        Similar to Node SDK's check_tcp_instrumentation_warning.
        """
        self.log("=" * 50, Colors.BLUE)
        self.log("Checking for Instrumentation Warnings", Colors.BLUE)
        self.log("=" * 50, Colors.BLUE)

        logs_dir = Path(".tusk/logs")
        traces_dir = Path(".tusk/traces")

        log_files = list(logs_dir.glob("*")) if logs_dir.exists() else []
        if not log_files:
            self.log("✗ ERROR: No log files found!", Colors.RED)
            self.exit_code = 1
            return

        # Check for TCP instrumentation warning in logs
        warning_pattern = "[SocketInstrumentation] TCP"
        warning_suffix = "called from inbound request context, likely unpatched dependency"

        found_warning = False
        for log_file in log_files:
            try:
                content = log_file.read_text()
                if warning_pattern in content and warning_suffix in content:
                    found_warning = True
                    self.log(f"✗ ERROR: Found socket instrumentation warning in {log_file.name}!", Colors.RED)
                    self.log("  This indicates an unpatched dependency is making TCP calls.", Colors.RED)

                    for line in content.split("\n"):
                        if warning_pattern in line:
                            self.log(f"  {line.strip()}", Colors.YELLOW)
                    break
            except Exception as e:
                self.log(f"Warning: Could not read log file {log_file}: {e}", Colors.YELLOW)

        if found_warning:
            self.exit_code = 1
        else:
            self.log("✓ No socket instrumentation warnings found.", Colors.GREEN)

        # Verify trace files exist (double-check after tusk run)
        trace_files = list(traces_dir.glob("*.jsonl")) if traces_dir.exists() else []
        if trace_files:
            self.log(f"✓ Found {len(trace_files)} trace file(s).", Colors.GREEN)
        else:
            self.log("✗ ERROR: No trace files found!", Colors.RED)
            self.exit_code = 1

    def cleanup(self):
        """Phase 5: Cleanup resources."""
        self.log("=" * 50, Colors.BLUE)
        self.log("Phase 5: Cleanup", Colors.BLUE)
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

        # Clean up app log file
        if self.app_log_file:
            try:
                self.app_log_file.close()
                os.unlink(self.app_log_file.name)
            except OSError:
                pass
            self.app_log_file = None

        # Traces are kept in container for inspection
        self.log("Cleanup complete", Colors.GREEN)

    # ── Benchmark mode ──────────────────────────────────────────────

    def run_benchmarks(self):
        """Run test_requests.py twice (DISABLED vs RECORD) and compare results."""
        duration = os.environ.get("BENCHMARK_DURATION", "10")
        warmup = os.environ.get("BENCHMARK_WARMUP", "3")
        benchmark_env = {
            "BENCHMARKS": os.environ.get("BENCHMARKS", "1"),
            "BENCHMARK_DURATION": duration,
            "BENCHMARK_WARMUP": warmup,
        }
        self.log(f"Benchmark config: duration={duration}s, warmup={warmup}s per endpoint", Colors.BLUE)

        # Run 1: SDK disabled (baseline)
        self.log("=" * 60, Colors.BLUE)
        self.log("BASELINE (SDK DISABLED)", Colors.BLUE)
        self.log("=" * 60, Colors.BLUE)

        if not self._start_app(mode="DISABLED"):
            self.log("Failed to start app in DISABLED mode", Colors.RED)
            self.exit_code = 1
            return

        try:
            baseline_result = self._run_test_requests(extra_env=benchmark_env)
            baseline_output = baseline_result.stdout
            print(baseline_output, end="")
        except subprocess.CalledProcessError:
            self.log("Benchmark test requests failed (DISABLED mode)", Colors.RED)
            self.exit_code = 1
            self._stop_app()
            return

        self._stop_app()

        # Run 2: SDK enabled (RECORD mode)
        self.log("")
        self.log("=" * 60, Colors.BLUE)
        self.log("WITH SDK (TUSK_DRIFT_MODE=RECORD)", Colors.BLUE)
        self.log("=" * 60, Colors.BLUE)

        if not self._start_app(mode="RECORD"):
            self.log("Failed to start app in RECORD mode", Colors.RED)
            self.exit_code = 1
            return

        try:
            sdk_result = self._run_test_requests(extra_env=benchmark_env)
            sdk_output = sdk_result.stdout
            print(sdk_output, end="")
        except subprocess.CalledProcessError:
            self.log("Benchmark test requests failed (RECORD mode)", Colors.RED)
            self.exit_code = 1
            self._stop_app()
            return

        self._stop_app()

        # Parse and compare
        baseline = self._parse_benchmark_output(baseline_output)
        sdk = self._parse_benchmark_output(sdk_output)
        self._print_comparison(baseline, sdk)

    def _parse_benchmark_output(self, output: str) -> dict[str, dict]:
        """Parse Go-style benchmark lines and return {name: {ops: float, reliable: bool}}."""
        results = {}
        for line in output.split("\n"):
            # Match: Benchmark_GET_/health   5200   961538 ns/op   1040.00 ops/s
            # Optional trailing " (~)" marks unreliable (low iteration count)
            m = re.match(r"(Benchmark_\S+)\s+\d+\s+\d+\s+ns/op\s+([\d.]+)\s+ops/s(\s+\(~\))?", line)
            if m:
                results[m.group(1)] = {
                    "ops": float(m.group(2)),
                    "reliable": m.group(3) is None,
                }
        return results

    def _print_comparison(self, baseline: dict[str, dict], sdk: dict[str, dict]):
        """Print a comparison table between baseline and SDK benchmark results."""
        self.log("")
        self.log("=" * 78, Colors.BLUE)
        self.log("COMPARISON (negative = slower with SDK)  (~) = low iterations, unreliable", Colors.BLUE)
        self.log("=" * 78, Colors.BLUE)
        self.log(f"{'Benchmark':<40} {'Baseline':>12} {'Current':>12} {'Diff':>10}")
        self.log("-" * 78)

        all_names = sorted(set(list(baseline.keys()) + list(sdk.keys())))
        for name in all_names:
            base = baseline.get(name)
            cur = sdk.get(name)

            if base is None:
                self.log(f"{name:<40} {'N/A':>12} {cur['ops']:>10.2f}/s {'':>10}")
                continue
            if cur is None:
                self.log(f"{name:<40} {base['ops']:>10.2f}/s {'N/A':>12} {'':>10}")
                continue

            base_ops = base["ops"]
            sdk_ops = cur["ops"]
            reliable = base["reliable"] and cur["reliable"]

            if base_ops > 0:
                diff_pct = ((sdk_ops - base_ops) / base_ops) * 100
            else:
                diff_pct = 0.0

            diff_str = f"{diff_pct:+.1f}%"
            flag = "" if reliable else " (~)"
            color = Colors.RED if diff_pct < -5 else Colors.YELLOW if diff_pct < 0 else Colors.GREEN
            self.log(f"{name:<40} {base_ops:>10.2f}/s {sdk_ops:>10.2f}/s {diff_str:>10}{flag}", color)

    def run(self) -> int:
        """Run the full e2e test lifecycle."""
        try:
            if os.environ.get('BENCHMARKS'):
                self.setup()
                self.run_benchmarks()
                return self.exit_code

            self.setup()

            if not self.record_traces():
                return 1

            self.run_tests()

            self.check_socket_instrumentation_warnings()

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
