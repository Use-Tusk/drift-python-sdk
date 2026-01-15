#!/bin/bash
# Profile the SDK using different profilers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
RESULTS_DIR="$SCRIPT_DIR/results"

cd "$PROJECT_ROOT"

mkdir -p "$RESULTS_DIR"

PROFILER="${1:-cprofile}"  # Default to cProfile

case "$PROFILER" in
    "pyspy"|"py-spy"|"flamegraph")
        echo "=============================================="
        echo "Running py-spy flame graph profiler"
        echo "=============================================="
        
        if ! command -v py-spy &> /dev/null; then
            echo "py-spy not found. Install with: pip install py-spy"
            echo "Or on macOS: brew install py-spy"
            exit 1
        fi
        
        OUTPUT="$RESULTS_DIR/flamegraph_$(date +%Y%m%d_%H%M%S).svg"
        echo "Output: $OUTPUT"
        echo ""
        
        # py-spy needs sudo on macOS for sampling
        if [[ "$OSTYPE" == "darwin"* ]]; then
            echo "Note: py-spy may require sudo on macOS"
            sudo py-spy record -o "$OUTPUT" --rate 100 -- python benchmarks/profile/simple_profile.py
        else
            py-spy record -o "$OUTPUT" --rate 100 -- python benchmarks/profile/simple_profile.py
        fi
        
        echo ""
        echo "Flame graph saved to: $OUTPUT"
        echo "Open in browser to view."
        ;;
        
    "cprofile"|"cProfile")
        echo "=============================================="
        echo "Running cProfile deterministic profiler"
        echo "=============================================="
        
        OUTPUT="$RESULTS_DIR/cprofile_$(date +%Y%m%d_%H%M%S).prof"
        echo "Output: $OUTPUT"
        echo ""
        
        python -m cProfile -o "$OUTPUT" benchmarks/profile/simple_profile.py
        
        echo ""
        echo "Profile saved to: $OUTPUT"
        echo ""
        echo "Analyzing top functions by cumulative time..."
        echo ""
        
        python -c "
import pstats
p = pstats.Stats('$OUTPUT')
p.strip_dirs()
p.sort_stats('cumulative')
print('=== Top 30 by cumulative time ===')
p.print_stats(30)
print()
print('=== Top 30 by total time (self) ===')
p.sort_stats('tottime')
p.print_stats(30)
"
        ;;
        
    "scalene")
        echo "=============================================="
        echo "Running Scalene CPU/memory profiler"
        echo "=============================================="
        
        if ! command -v scalene &> /dev/null; then
            echo "scalene not found. Install with: pip install scalene"
            exit 1
        fi
        
        OUTPUT="$RESULTS_DIR/scalene_$(date +%Y%m%d_%H%M%S).html"
        echo "Output: $OUTPUT"
        echo ""
        
        scalene --html --outfile "$OUTPUT" benchmarks/profile/simple_profile.py
        
        echo ""
        echo "Scalene report saved to: $OUTPUT"
        ;;
        
    "viztracer")
        echo "=============================================="
        echo "Running VizTracer timeline profiler"
        echo "=============================================="
        
        if ! python -c "import viztracer" 2>/dev/null; then
            echo "viztracer not found. Install with: pip install viztracer"
            exit 1
        fi
        
        OUTPUT="$RESULTS_DIR/viztracer_$(date +%Y%m%d_%H%M%S).json"
        echo "Output: $OUTPUT"
        echo ""
        
        python -m viztracer -o "$OUTPUT" benchmarks/profile/simple_profile.py
        
        echo ""
        echo "VizTracer trace saved to: $OUTPUT"
        echo "View with: vizviewer $OUTPUT"
        ;;
        
    *)
        echo "Usage: $0 [profiler]"
        echo ""
        echo "Available profilers:"
        echo "  cprofile   - Built-in deterministic profiler (default)"
        echo "  pyspy      - Sampling profiler with flame graphs"
        echo "  scalene    - CPU/memory profiler with line-level detail"
        echo "  viztracer  - Timeline/trace profiler"
        echo ""
        echo "Examples:"
        echo "  $0 cprofile    # Run cProfile"
        echo "  $0 pyspy       # Generate flame graph"
        echo "  $0 scalene     # CPU/memory analysis"
        exit 1
        ;;
esac

echo ""
echo "Done!"
