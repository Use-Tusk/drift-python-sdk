# SDK Benchmarks

Simple Go-style benchmarks for measuring SDK overhead.

## Quick Start (Automated)

```bash
./run_benchmark.sh [duration_seconds]
```

This automatically:
1. Starts the delay server
2. Runs baseline benchmark (SDK disabled)
3. Runs benchmark with SDK enabled
4. Prints comparison with overhead percentages

## Manual Mode

```bash
# Terminal 1: Start delay server
python delay_server.py

# Terminal 2: Run baseline (SDK disabled)
TUSK_DRIFT_MODE=DISABLED python app.py

# Terminal 3: Run benchmark
python benchmark.py --url=http://localhost:8080

# Terminal 2: Restart with SDK enabled
# Ctrl+C, then:
TUSK_DRIFT_MODE=RECORD python app.py

# Terminal 3: Run benchmark again
python benchmark.py --url=http://localhost:8080
```

## Output

```
Benchmark_Sort              1000        1234567 ns/op         810.37 ops/s
Benchmark_Downstream         500       20123456 ns/op          49.69 ops/s
```

## For Node SDK

Use the same `benchmark.py` against Node test server:
```bash
python benchmark.py --url=http://localhost:3000
```
