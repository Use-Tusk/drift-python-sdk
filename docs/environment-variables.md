# Environment Variables

This guide covers the environment variables that can be set when using the Tusk Drift SDK.

## TUSK_DRIFT_MODE

The `TUSK_DRIFT_MODE` environment variable controls how the SDK operates in your application.

### Available Modes

| Mode       | Description                                          | When to Use                                                                                  |
| ---------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `RECORD`   | Records traces for all instrumented operations       | Set this in environments where you want to capture API traces (e.g., staging, production)    |
| `REPLAY`   | Replays previously recorded traces                   | Automatically set by the Tusk CLI when running `tusk run` - you should NOT set this manually |
| `DISABLED` | Disables all instrumentation and recording           | Use when you want to completely disable Tusk with no performance impact                      |
| Unset      | Same as `DISABLED` - no instrumentation or recording | Default state when the variable is not set                                                   |

### Important Notes

**Recording Traces:**

- Set `TUSK_DRIFT_MODE=RECORD` in any environment where you want to record traces
- This is typically staging, production, or local development environments
- Traces will be saved according to your `recording` configuration in `.tusk/config.yaml`

**Replaying Traces:**

- `TUSK_DRIFT_MODE` is automatically set to `REPLAY` by the Tusk CLI when you run `tusk run`
- **Do NOT** manually set `TUSK_DRIFT_MODE=REPLAY` in your application startup commands
- The start command specified in your `.tusk/config.yaml` should NOT cause `TUSK_DRIFT_MODE` to be set to anything - the CLI handles this automatically

**Disabling Tusk:**

- If `TUSK_DRIFT_MODE` is unset or set to `DISABLED`, the SDK will not add any instrumentation
- No data will be recorded and there should be **no performance impact**
- This is useful for environments where you don't need Tusk functionality

### Examples

**Recording in development:**

```bash
TUSK_DRIFT_MODE=RECORD python app.py
```

**Recording in production (via environment variable):**

```bash
# In your .env file or deployment configuration
TUSK_DRIFT_MODE=RECORD
```

**Start command in config.yaml (correct):**

```yaml
# .tusk/config.yaml
start_command: "python app.py" # Do NOT include TUSK_DRIFT_MODE here
```

**Replaying traces (handled by CLI):**

```bash
# The CLI automatically sets TUSK_DRIFT_MODE=REPLAY
tusk run
```

**Disabling Tusk:**

```bash
# Either unset the variable or explicitly disable
TUSK_DRIFT_MODE=DISABLED python app.py

# Or simply don't set it at all
python app.py
```

## TUSK_API_KEY

Your Tusk Drift API key, required when using Tusk Cloud for storing and managing traces.

- **Required:** Only if using Tusk Cloud (not needed for local-only trace storage)
- **Where to get it:** [Tusk Drift Dashboard](https://usetusk.ai/app/settings/api-keys)

### How to Set

**For Recording:**

- Must be provided in the `TuskDrift.initialize()` call:

  ```python
  sdk = TuskDrift.initialize(
      api_key=os.environ.get("TUSK_API_KEY"),  # or hard-coded for non-production
      # ... other options
  )
  ```

**For Replay:**

- Can be set as an environment variable:

  ```bash
  TUSK_API_KEY=your-api-key-here tusk run
  ```

- Or use the Tusk CLI login command (recommended):

  ```bash
  tusk auth login
  ```

  This will securely store your auth key for future replay sessions.

## TUSK_SAMPLING_RATE

Controls what percentage of requests are recorded during trace collection.

- **Type:** Number between 0.0 and 1.0
- **Default:** 1.0 (100% of requests)
- **Precedence:** This environment variable is overridden by the `sampling_rate` parameter in `TuskDrift.initialize()`, but takes precedence over the `sampling_rate` setting in `.tusk/config.yaml`

**Examples:**

```bash
# Record all requests (100%)
TUSK_SAMPLING_RATE=1.0 python app.py

# Record 10% of requests
TUSK_SAMPLING_RATE=0.1 python app.py
```

For more details on sampling rate configuration methods and precedence, see the [Initialization Guide](./initialization.md#configure-sampling-rate).

## Rust Core Flags

These variables control optional Rust-accelerated paths in the SDK.

| Variable | Description | Default |
| --- | --- | --- |
| `TUSK_USE_RUST_CORE` | Enables Rust binding usage when available (`1`, `true`, `yes`) | `0` (disabled) |
| `TUSK_SKIP_PROTO_VALIDATION` | Skips expensive protobuf validation in hot path (`1`, `true`, `yes`) | `0` (disabled) |

**Notes:**

- The SDK is fail-open: if Rust bindings are unavailable or a Rust call fails, it falls back to Python implementation.
- `TUSK_USE_RUST_CORE` does not install Rust bindings automatically. The `drift-core-python` package still must be installed in your environment.
- `TUSK_SKIP_PROTO_VALIDATION` is performance-focused and should be used with confidence in parity tests and serialization correctness.

See [`rust-core-bindings.md`](./rust-core-bindings.md) for more details.

**Example usage:**

```bash
# Enable Rust path (if drift-core-python is installed)
TUSK_USE_RUST_CORE=1 python app.py

# Enable Rust path and skip proto validation
TUSK_USE_RUST_CORE=1 TUSK_SKIP_PROTO_VALIDATION=1 python app.py
```

## Connection Variables

These variables configure how the SDK connects to the Tusk CLI during replay:

| Variable           | Description                         | Default                   |
| ------------------ | ----------------------------------- | ------------------------- |
| `TUSK_MOCK_HOST`   | CLI host for TCP connection         | -                         |
| `TUSK_MOCK_PORT`   | CLI port for TCP connection         | -                         |
| `TUSK_MOCK_SOCKET` | Unix socket path for CLI connection | `/tmp/tusk-connect.sock`  |

These are typically set automatically by the Tusk CLI and do not need to be configured manually.

## Related Docs

- [Initialization Guide](./initialization.md) - SDK initialization parameters and config file settings
- [Quick Start Guide](./quickstart.md) - Record and replay your first trace
