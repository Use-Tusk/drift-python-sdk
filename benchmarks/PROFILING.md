# Profiling the Drift Python SDK

This document explains how to profile the SDK to understand where performance overhead comes from.

## Quick Start

```bash
cd /path/to/drift-python-sdk

# Run cProfile (recommended starting point)
./benchmarks/profile/profile.sh cprofile
```

## Available Profilers

### 1. cProfile (Built-in, Deterministic)

**Best for:** Understanding call counts and cumulative time per function.

```bash
./benchmarks/profile/profile.sh cprofile
```

This runs the profiling workload and outputs:

- A `.prof` file in `benchmarks/profile/results/`
- Summary of top functions by cumulative and total time

**View interactively with snakeviz:**

```bash
pip install snakeviz
snakeviz benchmarks/profile/results/cprofile_*.prof
```

Opens an interactive sunburst diagram in your browser showing the call hierarchy.

### 2. py-spy (Sampling, Flame Graphs)

**Best for:** Low-overhead profiling and classic flame graph visualization.

```bash
# Requires sudo on macOS
sudo ./benchmarks/profile/profile.sh pyspy
```

Generates an SVG flame graph that you can open in any browser.

**Install:**

```bash
pip install py-spy
# Or on macOS: brew install py-spy
```

### 3. Scalene (CPU + Memory)

**Best for:** Understanding both CPU time and memory allocation per line.

```bash
pip install scalene
./benchmarks/profile/profile.sh scalene
```

Generates an HTML report with line-by-line CPU and memory breakdown.

### 4. VizTracer (Timeline)

**Best for:** Seeing the sequence and timing of function calls over time.

```bash
pip install viztracer
./benchmarks/profile/profile.sh viztracer
```

**View the trace:**

```bash
vizviewer benchmarks/profile/results/viztracer_*.json
```

Opens a Chrome DevTools-style timeline visualization.

## Profile Analysis Results

Based on profiling 500 HTTP requests through the SDK:

### Top Overhead Sources

| Function | Time/Call | Description |
|----------|-----------|-------------|
| `span_serialization.clean_span_to_proto` | ~1.7ms | Converting spans to protobuf format |
| `td_span_processor.on_end` | ~2.1ms | Processing spans when they complete |
| `handler.finalize_wsgi_span` | ~2.2ms | Finalizing HTTP/WSGI spans |
| `otel_converter.otel_span_to_clean_span_data` | ~0.4ms | Converting OpenTelemetry spans |

### Key Findings

1. **Span serialization is the biggest bottleneck**
   - `_dict_to_struct`, `_value_to_proto`, `_json_schema_to_proto` are called recursively
   - Converting rich span data to protobuf is inherently expensive

2. **The instrumentation itself is cheap**
   - Function patching/wrapping adds minimal overhead
   - Most time is spent in span processing, not in the hooks

3. **Sampling reduces overhead proportionally**
   - At 10% sampling, most requests skip span serialization entirely
   - This explains why lower sampling rates dramatically improve performance

### Optimization Opportunities

Based on the profile data:

1. Lazy serialization - Defer protobuf conversion until export time
2. Batch serialization - Serialize multiple spans together
3. Schema caching - Cache JSON schema conversions
4. Attribute filtering - Skip serializing large/unnecessary attributes

## Custom Profiling

### Profile a Specific Workload

Edit `benchmarks/profile/simple_profile.py` to customize:

```python
# Adjust number of iterations
iterations = 1000

# Change request mix
if i % 3 == 0:
    # Your custom endpoint
    response = session.get(f"{server_url}/your-endpoint")
```

### Profile with Different SDK Settings

```python
# In simple_profile.py, modify SDK initialization:
sdk = TuskDrift.initialize(
    api_key="profile-test-key",
    env="profile",
    sampling_rate=0.1,  # Test with different sampling rates
    transforms={...},    # Test with transforms enabled
    log_level="warning",
)
```

### Profile Production Code

You can use py-spy to attach to a running process:

```bash
# Find your Python process PID
ps aux | grep python

# Attach and record
sudo py-spy record -o profile.svg --pid <PID> --duration 30
```

## Comparing Before/After Changes

1. Run profile before changes:

   ```bash
   ./benchmarks/profile/profile.sh cprofile
   mv benchmarks/profile/results/cprofile_*.prof benchmarks/profile/results/before.prof
   ```

2. Make your changes

3. Run profile after changes:

   ```bash
   ./benchmarks/profile/profile.sh cprofile
   mv benchmarks/profile/results/cprofile_*.prof benchmarks/profile/results/after.prof
   ```

4. Compare with pstats:

   ```python
   import pstats

   before = pstats.Stats('benchmarks/profile/results/before.prof')
   after = pstats.Stats('benchmarks/profile/results/after.prof')

   print("=== BEFORE ===")
   before.strip_dirs().sort_stats('cumulative').print_stats(20)

   print("=== AFTER ===")
   after.strip_dirs().sort_stats('cumulative').print_stats(20)
   ```

## Output Files

Profile results are saved to `benchmarks/profile/results/` (gitignored):

| File | Description |
|------|-------------|
| `cprofile_*.prof` | cProfile binary data |
| `flamegraph_*.svg` | py-spy flame graph |
| `scalene_*.html` | Scalene HTML report |
| `viztracer_*.json` | VizTracer timeline data |
| `traces/` | SDK trace output during profiling |

## Tips

- **Start with cProfile** - It's built-in and gives good overview
- **Use snakeviz for exploration** - Interactive visualization helps find hotspots
- **Profile realistic workloads** - Micro-benchmarks may not reflect production patterns
- **Compare sampling rates** - Profile with 100% vs 10% sampling to see the difference
- **Watch for I/O** - File writes and network calls can dominate profiles
