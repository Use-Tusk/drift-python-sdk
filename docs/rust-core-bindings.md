# Rust Core Bindings

This document explains how Rust acceleration works in the Python SDK, how to enable it, and what fallback behavior to expect.

## Overview

The SDK can offload selected hot-path logic to Rust through optional Python bindings (`drift-core-python`), defined in the [`drift-core`](https://github.com/Use-Tusk/drift-core) repository. This is controlled by environment flags and is designed to fail open.

At a high level:

- Python SDK logic remains the source of truth.
- Rust paths are opportunistic optimizations.
- If Rust is unavailable or fails at runtime, SDK behavior falls back to Python equivalents.

## Enablement

Rust is enabled by default when `TUSK_USE_RUST_CORE` is unset.

Use `TUSK_USE_RUST_CORE` to explicitly override behavior:

- Truthy: `1`, `true`, `yes`, `on`
- Falsy: `0`, `false`, `no`, `off`

Examples:

```bash
# Explicitly enable (same as unset)
TUSK_USE_RUST_CORE=1

# Explicitly disable
TUSK_USE_RUST_CORE=0
```

## Installation Requirements

Rust acceleration requires the `drift-core-python` package to be installed in the runtime environment.

Notes:

- The SDK does not auto-install this package at runtime.
- If the package is missing or cannot be imported on a machine, the SDK continues on Python code paths.

You can install the SDK with Rust bindings via extras:

```bash
pip install "tusk-drift-python-sdk[rust]"
```

## Platform Compatibility

`drift-core` publishes native artifacts across a defined support matrix. See:

- [`drift-core` compatibility matrix](https://github.com/Use-Tusk/drift-core/blob/main/docs/compatibility-matrix.md)

If no compatible wheel exists for your environment, `pip` may attempt a source build of `drift-core-python`, which typically requires a Rust toolchain and native build prerequisites.

## Fallback Behavior

The bridge module is fail-open:

- Rust calls are guarded.
- On import failures or call exceptions, the corresponding helper returns `None`.
- Calling code then uses the existing Python implementation.
- On startup, the SDK logs whether Rust is enabled/disabled and whether it had to fall back to Python.

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

- Default production-safe posture: keep Rust enabled (default) only on tested deployment matrices.
- Performance posture: enable Rust + benchmark on your workloads before broad rollout.
- Reliability posture: keep parity tests and smoke tests in CI to detect drift between Python and Rust paths.

## Related Docs

- [Environment Variables](./environment-variables.md)
- [Initialization Guide](./initialization.md)
- [Context Propagation](./context-propagation.md)
