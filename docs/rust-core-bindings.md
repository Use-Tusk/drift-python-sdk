# Rust Core Bindings

This document explains how Rust acceleration works in the Python SDK, how to enable it, and what fallback behavior to expect.

## Overview

The SDK can offload selected hot-path logic to Rust through optional Python bindings (`drift-core-python`), defined in the [`drift-core`](https://github.com/Use-Tusk/drift-core) repository. This is controlled by environment flags and is designed to fail open.

At a high level:

- Python SDK logic remains the source of truth.
- Rust paths are opportunistic optimizations.
- If Rust is unavailable or fails at runtime, SDK behavior falls back to Python equivalents.

## Enablement

Set:

```bash
TUSK_USE_RUST_CORE=1
```

Truthy values are `1`, `true`, and `yes` (case-insensitive). Any other value is treated as disabled.

## Installation Requirements

Rust acceleration requires the `drift-core-python` package to be installed in the runtime environment.

Notes:

- The SDK does not auto-install this package at runtime.
- If the package is missing or cannot be imported on a machine, the SDK continues on Python code paths.

You can install the SDK with Rust bindings via extras:

```bash
pip install "tusk-drift-python-sdk[rust]"
```

## Wheel Platform Coverage

Based on the current `drift-core` publish workflow, prebuilt wheels are built for:

- Linux `x86_64-unknown-linux-gnu`
- Linux `aarch64-unknown-linux-gnu`
- macOS Apple Silicon `aarch64-apple-darwin`
- Windows `x86_64-pc-windows-msvc`

Likely missing prebuilt wheels (source build fallback required) include:

- macOS Intel (`x86_64-apple-darwin`)
- Linux musl targets (e.g. Alpine)
- Windows ARM64
- Other uncommon Python/platform combinations not covered by release artifacts

If no wheel matches the environment, `pip` may attempt a source build of `drift-core-python`, which typically requires a Rust toolchain and native build prerequisites.

## Fallback Behavior

The bridge module is fail-open:

- Rust calls are guarded.
- On import failures or call exceptions, the corresponding helper returns `None`.
- Calling code then uses the existing Python implementation.

This means users do not need Rust installed to run the SDK when Rust acceleration is disabled or unavailable.

## Optional Performance Flag

```bash
TUSK_SKIP_PROTO_VALIDATION=1
```

This skips expensive protobuf validation checks in hot paths.

Use with care:

- Recommended only when parity/smoke tests are healthy.
- Keep it off in environments where strict serialization verification is preferred.

## Practical Guidance

- Default production-safe posture: leave Rust disabled unless you have tested your deployment matrix.
- Performance posture: enable Rust + benchmark on your workloads before broad rollout.
- Reliability posture: keep parity tests and smoke tests in CI to detect drift between Python and Rust paths.

## Related Docs

- [Environment Variables](./environment-variables.md)
- [Initialization Guide](./initialization.md)
- [Context Propagation](./context-propagation.md)
