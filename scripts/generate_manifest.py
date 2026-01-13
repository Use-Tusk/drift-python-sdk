#!/usr/bin/env python3
"""
Generates instrumentation-manifest.json by parsing instrumentation source files
using the Python AST (no code execution).

This approach mirrors the Node SDK's generateManifest.ts script.

## How it works (high level)

- Scans `drift/instrumentation/<library>/instrumentation.py` (excluding internal dirs).
- Parses each file with Python's AST and finds `super().__init__(...)` calls.
- Extracts `module_name` and `supported_versions` from the call.
- Outputs a JSON manifest matching the Node SDK format.

## Maintainer notes (adding new instrumentations)

- Keep `module_name` and `supported_versions` as string literals in the __init__ call.
- Ensure the library folder has an `instrumentation.py`; that's what this script scans.
- If an instrumentation is internal (e.g., `socket`, `datetime`), add it to INTERNAL_INSTRUMENTATIONS.

Run: python scripts/generate_manifest.py [--dry-run] [--output PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Script location
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
INSTRUMENTATION_DIR = PROJECT_ROOT / "drift" / "instrumentation"

# Internal instrumentations that shouldn't be in the public manifest.
# These patch Python built-ins or provide internal SDK functionality.
INTERNAL_INSTRUMENTATIONS = {
    "socket",  # Internal TCP-level patching
    "datetime",  # Internal time mocking
    "wsgi",  # Base class for framework instrumentations
    "http",  # Transform engine, not a standalone instrumentation
    "e2e_common",  # Test infrastructure
}


def get_sdk_version() -> str:
    """Read version from pyproject.toml."""
    pyproject_path = PROJECT_ROOT / "pyproject.toml"

    with open(pyproject_path) as f:
        for line in f:
            if line.startswith("version = "):
                # Extract version from: version = "0.1.6"
                return line.split('"')[1]

    raise RuntimeError("Could not find version in pyproject.toml")


def discover_instrumentation_files() -> list[Path]:
    """Discover all instrumentation.py files in the instrumentation directory."""
    files: list[Path] = []

    for entry in INSTRUMENTATION_DIR.iterdir():
        if entry.is_dir() and entry.name not in INTERNAL_INSTRUMENTATIONS:
            instrumentation_file = entry / "instrumentation.py"
            if instrumentation_file.exists():
                files.append(instrumentation_file)

    return sorted(files)


def extract_string_from_node(node: ast.expr) -> str | None:
    """Extract a string value from an AST node (handles Constant nodes)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def parse_instrumentation_file(file_path: Path) -> dict[str, str] | None:
    """
    Parse a Python file and extract module_name and supported_versions
    from the InstrumentationBase.__init__() call.

    Returns dict with 'module_name' and 'supported_versions', or None if not found.
    """
    with open(file_path) as f:
        source = f.read()

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as e:
        print(f"  Warning: Syntax error in {file_path}: {e}", file=sys.stderr)
        return None

    # Find super().__init__(...) calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Check if it's super().__init__(...)
        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "__init__":
            continue
        if not isinstance(node.func.value, ast.Call):
            continue
        func_value = node.func.value
        if not isinstance(func_value.func, ast.Name):
            continue
        if func_value.func.id != "super":
            continue

        # Found a super().__init__() call, extract keyword arguments
        module_name: str | None = None
        supported_versions: str | None = None

        for keyword in node.keywords:
            if keyword.arg == "module_name":
                module_name = extract_string_from_node(keyword.value)
            elif keyword.arg == "supported_versions":
                supported_versions = extract_string_from_node(keyword.value)

        if module_name:
            return {
                "module_name": module_name,
                "supported_versions": supported_versions or "*",
            }

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate instrumentation manifest for the Python SDK")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print manifest to stdout without writing to file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "instrumentation-manifest.json",
        help="Output file path (default: instrumentation-manifest.json in project root)",
    )
    args = parser.parse_args()

    print("Discovering instrumentation files...")
    files = discover_instrumentation_files()
    print(f"Found {len(files)} instrumentation files (excluding internal)")

    instrumentations: list[dict[str, str | list[str]]] = []

    for file_path in files:
        relative_path = file_path.relative_to(INSTRUMENTATION_DIR)
        result = parse_instrumentation_file(file_path)

        if result is None:
            print(f"  Warning: No instrumentation found in {relative_path}", file=sys.stderr)
        else:
            print(f"  {relative_path}: {result['module_name']}@{result['supported_versions']}")
            instrumentations.append(
                {
                    "packageName": result["module_name"],
                    "supportedVersions": [result["supported_versions"]],
                }
            )

    # Sort by package name for consistent output
    instrumentations.sort(key=lambda x: x["packageName"])  # type: ignore[arg-type, return-value]

    sdk_version = get_sdk_version()

    manifest = {
        "sdkVersion": sdk_version,
        "language": "python",
        "pythonVersion": ">=3.12",
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "instrumentations": instrumentations,
    }

    manifest_json = json.dumps(manifest, indent=2) + "\n"

    if args.dry_run:
        print("\n--- Manifest (dry run) ---")
        print(manifest_json)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(manifest_json)
        print(f"\nGenerated {args.output}")

    print(f"SDK version: {sdk_version}")
    print(f"Instrumentations: {len(instrumentations)}")
    for entry in instrumentations:
        versions = entry["supportedVersions"]
        print(f"  - {entry['packageName']}: {', '.join(versions)}")  # type: ignore[arg-type]

    return 0


if __name__ == "__main__":
    sys.exit(main())
