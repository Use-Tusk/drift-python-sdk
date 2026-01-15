# Benchmarks

These benchmarks measure the performance overhead of the Drift Python SDK.

## Overview

The benchmark suite runs a Flask test server and makes HTTP requests to various endpoints
while measuring latency, throughput, CPU usage, and memory consumption. Three configurations
are tested:

1. SDK Disabled (baseline) - No SDK instrumentation
2. SDK Active - SDK in RECORD mode, capturing traces
3. SDK Active with Transforms - SDK with data transformation rules enabled

## Usage

### Prerequisites

Make sure you have the required dependencies:

```bash
cd /path/to/drift-python-sdk
uv pip install psutil flask requests
```

### Running All Benchmarks

```bash
# Run all benchmarks and compare results
./run_benchmarks.sh
```

### Running Individual Benchmarks

```bash
# Realistic workload benchmark (recommended)
python benchmarks/bench/realistic_workload.py

# Fixed QPS latency test (measures latency at controlled request rates)
python benchmarks/bench/fixed_qps_latency.py

# Synthetic benchmarks (stress tests)
python benchmarks/bench/sdk_disabled.py
python benchmarks/bench/sdk_active.py
python benchmarks/bench/sdk_active_with_transforms.py

# Sampling rate comparison
python benchmarks/bench/sdk_sampling_rates.py
```

### Comparing Results

After running benchmarks, compare the results:

```bash
python benchmarks/compare_benchmarks.py
```

### Configuration

You can configure benchmarks via environment variables:

- `BENCHMARK_ENABLE_MEMORY=false` - Disable memory monitoring (reduces CPU overhead)

Or modify the options in `common.py`:

```python
DEFAULT_OPTIONS = {
    "time_per_task_ms": 10_000,  # Duration per task (10 seconds default)
    "warmup_iterations": 5,      # Warmup iterations before measurement
    "enable_memory_tracking": True,
}
```

## Benchmark Tasks

### Realistic Workloads (Recommended)

These endpoints simulate production API behavior:

- **GET /api/typical-read** (~5-10ms): Auth check + DB read + response serialization
- **POST /api/typical-write** (~15-25ms): Validation + DB write + response
- **POST /api/realistic** (~10-20ms): Validation + DB query + data processing + response

### Synthetic Workloads

These are stress tests, not representative of production:

- **POST /api/compute-hash**: Pure CPU (iterative SHA-256) - useful for profiling
- **POST /api/io-bound**: Pure I/O (sleep delays) - tests baseline overhead
- **POST /api/auth/login, /api/users**: Sensitive data for transform testing

## Output

Results are saved to `benchmarks/results/` as JSON files:

- `sdk-disabled.json` - Baseline results
- `sdk-active.json` - SDK enabled results
- `sdk-active-with-transforms.json` - SDK with transforms results

The comparison script outputs a markdown table showing:

- Throughput delta (negative = worse)
- Tail latency (p99) delta (positive = worse)
- CPU usage delta
- Memory overhead

## Interpreting Results

- **Throughput $\Delta$**: Percentage change in operations/second. Negative means slower.
- **Tail Latency $\Delta$**: Percentage change in p99 latency. Positive means slower.
- **CPU User $\Delta$**: Change in user-space CPU percentage.
- **Memory $\Delta$**: Additional memory used by the SDK.

Ideally, the SDK should have minimal impact:

- Throughput should be within ±5%
- Tail latency increase should be <10%
- Memory overhead should be reasonable (<50MB)

## Profiling

For detailed profiling to understand where SDK overhead comes from, see **[PROFILING.md](./PROFILING.md)**.

Quick start:

```bash
# Run cProfile analysis
./benchmarks/profile/profile.sh cprofile

# View interactively
pip install snakeviz
snakeviz benchmarks/profile/results/cprofile_*.prof
```

## Architecture

```text
benchmarks/
├── bench/
│   ├── common.py              # Shared benchmark logic
│   ├── fixed_qps_latency.py   # Fixed QPS latency test
│   ├── realistic_workload.py  # Realistic API workload benchmark
│   ├── resource_monitor.py    # CPU/memory monitoring
│   ├── result_utils.py        # Result serialization
│   ├── sdk_disabled.py        # Baseline benchmark (synthetic)
│   ├── sdk_active.py          # SDK active benchmark (synthetic)
│   ├── sdk_active_with_transforms.py
│   └── sdk_sampling_rates.py  # Sampling rate impact benchmark
├── profile/
│   ├── profile.sh             # Profiler runner script
│   ├── simple_profile.py      # Profiling workload
│   └── results/               # Profile output (gitignored)
├── server/
│   └── test_server.py         # Flask test server
├── results/                   # JSON output (gitignored)
├── compare_benchmarks.py      # Result comparison script
├── run_benchmarks.sh          # Runner script
├── PROFILING.md               # Profiling documentation
└── README.md
```
