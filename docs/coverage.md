# Code Coverage (Python)

The Python SDK collects per-test code coverage during Tusk Drift replay using `coverage.py`. Unlike Node.js (which uses V8's built-in coverage), Python requires the `coverage` package to be installed.

## Requirements

```bash
pip install tusk-drift-python-sdk[coverage]
```

If `coverage` is not installed when coverage is enabled, the SDK logs a warning and coverage is skipped. Tests still run normally.

## How It Works

### coverage.py Integration

When coverage is enabled (via `--show-coverage`, `--coverage-output`, or `coverage.enabled: true` in config), the CLI sets `TUSK_COVERAGE=true`. The SDK detects this during initialization and starts coverage.py:

```python
# What the SDK does internally:
import coverage
cov = coverage.Coverage(
    source=[os.path.realpath(os.getcwd())],
    branch=True,
    omit=["*/site-packages/*", "*/venv/*", "*/.venv/*", "*/tests/*", "*/test_*.py", "*/__pycache__/*"],
)
cov.start()
```

Key points:
- `branch=True` enables branch coverage (arc-based tracking)
- `source` is set to the real path of the working directory (symlinks resolved)
- Third-party code (site-packages, venv) is excluded by default

### Snapshot Flow

1. **Baseline**: CLI sends `CoverageSnapshotRequest(baseline=true)`. The SDK:
   - Calls `cov.stop()`
   - Uses `cov.analysis2(filename)` for each measured file to get ALL coverable lines (statements + missing)
   - Returns lines with count=0 for uncovered, count=1 for covered
   - Calls `cov.erase()` then `cov.start()` to reset counters

2. **Per-test**: CLI sends `CoverageSnapshotRequest(baseline=false)`. The SDK:
   - Calls `cov.stop()`
   - Uses `cov.get_data().lines(filename)` to get only executed lines since last reset
   - Returns only covered lines (count=1)
   - Calls `cov.erase()` then `cov.start()` to reset

3. **Communication**: Results are sent back to the CLI via the existing protobuf channel — same socket used for replay. No HTTP server or extra ports.

### Branch Coverage

Branch coverage uses coverage.py's arc tracking. The SDK extracts per-line branch data using:

```python
analysis = cov._analyze(filename)          # Private API
missing_arcs = analysis.missing_branch_arcs()
executed_arcs = set(data.arcs(filename) or [])
```

For each branch point (line with multiple execution paths), the SDK reports:
- `total`: number of branch paths from that line
- `covered`: number of paths that were actually taken

**Note:** `_analyze()` is a private coverage.py API. It's the only way to get per-line branch arc data. The public API (`analysis2()`) only provides aggregate branch counts. This means branch coverage may break on major coverage.py version upgrades.

### Path Handling

The SDK uses `os.path.realpath()` for the source root to handle symlinked project directories. File paths reported by coverage.py are also resolved via `realpath` before comparison. This prevents the silent failure where all files get filtered out because symlink paths don't match.

## Environment Variables

Set automatically by the CLI. You should not set these manually.

| Variable | Description |
|----------|-------------|
| `TUSK_COVERAGE` | Set to `true` by the CLI when coverage is enabled. The SDK checks this to decide whether to start coverage.py. |

Note: `NODE_V8_COVERAGE` is also set by the CLI (for Node.js), but the Python SDK ignores it — it only checks `TUSK_COVERAGE`.

## Thread Safety

Coverage collection uses a module-level lock (`threading.Lock`) to ensure thread safety:

- `start_coverage_collection()`: Acquires lock while initializing. Guards against double initialization — if called twice, stops the existing instance first.
- `take_coverage_snapshot()`: Acquires lock for the entire stop/read/erase/start cycle.
- `stop_coverage_collection()`: Acquires lock while stopping and cleaning up.

This is important because the protobuf communicator runs coverage handlers in a background thread.

## Limitations

- **`coverage` package required**: Unlike Node.js (V8 coverage is built-in), Python needs `pip install coverage`. If not installed, coverage silently doesn't work (warning logged).
- **Performance overhead**: coverage.py uses `sys.settrace()` which adds 10-30% execution overhead. This only applies during coverage replay runs.
- **Multi-process servers**: gunicorn with `--workers > 1` forks worker processes. The SDK starts coverage.py in the main process; forked workers don't inherit it. Use `--workers 1` during coverage runs.
- **Private API for branches**: `_analyze()` is not part of coverage.py's public API. Branch coverage detail may break on future coverage.py versions.
- **Python 3.12+ recommended for async**: coverage.py's `sys.settrace` can miss some async lines on Python < 3.12. Python 3.12+ uses `sys.monitoring` for better async tracking.
- **Startup ordering**: coverage.py starts during SDK initialization. Code that executes before `TuskDrift.initialize()` (e.g., module-level code in `tusk_drift_init.py`) isn't tracked. This is why `tusk_drift_init.py` typically shows 0% coverage.
- **C extensions invisible**: coverage.py can't track C extensions (numpy, Cython modules). Not relevant for typical web API servers.
