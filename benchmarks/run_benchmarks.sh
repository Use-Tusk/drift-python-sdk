#!/bin/bash
# Run all benchmarks and compare results

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "=============================================="
echo "Drift Python SDK Benchmarks"
echo "=============================================="
echo ""

# Check for psutil
if ! python -c "import psutil" 2>/dev/null; then
    echo "Installing psutil..."
    uv pip install psutil
fi

# Check for flask
if ! python -c "import flask" 2>/dev/null; then
    echo "Installing flask..."
    uv pip install flask
fi

# Check for requests
if ! python -c "import requests" 2>/dev/null; then
    echo "Installing requests..."
    uv pip install requests
fi

echo ""
echo "Running SDK Disabled (baseline)..."
echo "----------------------------------------------"
python benchmarks/bench/sdk_disabled.py

echo ""
echo "Running SDK Active..."
echo "----------------------------------------------"
python benchmarks/bench/sdk_active.py

echo ""
echo "Running SDK Active with Transforms..."
echo "----------------------------------------------"
python benchmarks/bench/sdk_active_with_transforms.py

echo ""
echo "=============================================="
echo "Comparing Results"
echo "=============================================="
python benchmarks/compare_benchmarks.py
