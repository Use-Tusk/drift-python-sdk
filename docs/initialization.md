# Initialization

## Prerequisites

Before setting up the SDK, ensure you have:

- Python 3.9 or later installed
- Installed the [Tusk Drift CLI](https://github.com/Use-Tusk/tusk-cli?tab=readme-ov-file#install)
- Obtained an API key from the [Tusk Drift dashboard](https://usetusk.ai/app/settings/api-keys) (only required if using Tusk Cloud)

> [!TIP]
> For automated setup, use `tusk drift setup` which handles SDK installation and initialization for you.
> The steps below are for manual setup.

## Step 1: Install the SDK

Install the SDK with your framework's extras:

```bash
# Base installation
pip install tusk-drift-python-sdk
```

### With Framework Support

```bash
# Flask
pip install tusk-drift-python-sdk[flask]

# FastAPI
pip install tusk-drift-python-sdk[fastapi]

# Django
pip install tusk-drift-python-sdk[django]
```

## Step 2: Initialize the SDK

Create an initialization file or add the SDK initialization to your application entry point. The SDK must be initialized **before** your application starts handling requests.

**IMPORTANT**: Ensure that `TuskDrift` is initialized before any other telemetry providers (e.g., OpenTelemetry, Sentry, etc.). If not, your existing telemetry may not work properly.

### Initialization Parameters

<table>
  <thead>
    <tr>
      <th>Option</th>
      <th>Type</th>
      <th>Default</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>api_key</code></td>
      <td><code>str</code></td>
      <td><b>Required if using Tusk Cloud</b></td>
      <td>Your Tusk Drift API key.</td>
    </tr>
    <tr>
      <td><code>env</code></td>
      <td><code>str</code></td>
      <td><code>os.environ.get("ENV", "development")</code></td>
      <td>The environment name.</td>
    </tr>
    <tr>
      <td><code>log_level</code></td>
      <td><code>"silent" | "error" | "warn" | "info" | "debug"</code></td>
      <td><code>"info"</code></td>
      <td>The logging level.</td>
    </tr>
    <tr>
      <td><code>sampling_rate</code></td>
      <td><code>float</code></td>
      <td><code>None</code></td>
      <td>Override the base sampling rate (0.0 - 1.0) for recording. Takes precedence over <code>TUSK_RECORDING_SAMPLING_RATE</code> and config file base-rate settings. Does not change <code>recording.sampling.mode</code>.</td>
    </tr>
  </tbody>
</table>

> **See also:** [Environment Variables guide](./environment-variables.md) for detailed information about environment variables.

## Framework-Specific Setup

### Flask

```python
import os
from flask import Flask
from drift import TuskDrift

# Initialize SDK BEFORE creating Flask app
sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
    env=os.environ.get("FLASK_ENV", "development"),
    log_level="debug"
)

app = Flask(__name__)

@app.route("/")
def hello():
    return "Hello, World!"

if __name__ == "__main__":
    # Mark app as ready before starting server
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000)
```

### FastAPI

```python
import os
import uvicorn
from fastapi import FastAPI
from drift import TuskDrift

# Initialize SDK BEFORE creating FastAPI app
sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
    env=os.environ.get("ENV", "development"),
    log_level="debug"
)

app = FastAPI()

@app.get("/")
async def hello():
    return {"message": "Hello, World!"}

if __name__ == "__main__":
    # Mark app as ready before starting server
    sdk.mark_app_as_ready()
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Django

For Django applications, initialize the SDK in your `manage.py` or WSGI/ASGI entry point:

```python
# manage.py
import os
import sys

# Initialize SDK BEFORE Django setup
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

from drift import TuskDrift

sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
    env=os.environ.get("DJANGO_ENV", "development"),
    log_level="debug"
)

import django
django.setup()

# Mark app as ready
sdk.mark_app_as_ready()

def main():
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)

if __name__ == "__main__":
    main()
```

## Configure Sampling Rate

Sampling controls what percentage of inbound requests are recorded in `RECORD` mode.

Tusk Drift supports two sampling modes in `.tusk/config.yaml`:

- `fixed`: record requests at a constant base rate.
- `adaptive`: start from a base rate and automatically shed load when queue pressure, export failures, or memory pressure indicate the SDK should back off. In severe conditions the SDK can temporarily pause recording entirely.

Sampling configuration is resolved in two layers:

1. **Base rate precedence** (highest to lowest):
   - `TuskDrift.initialize(sampling_rate=...)`
   - `TUSK_RECORDING_SAMPLING_RATE`
   - legacy alias `TUSK_SAMPLING_RATE`
   - `.tusk/config.yaml` `recording.sampling.base_rate`
   - `.tusk/config.yaml` legacy `recording.sampling_rate`
   - default base rate `1.0`
2. **Mode and minimum rate**:
   - `recording.sampling.mode` comes from `.tusk/config.yaml` and defaults to `fixed`
   - `recording.sampling.min_rate` is only used in `adaptive` mode and defaults to `0.001` when omitted

> [!NOTE]
> Requests before `sdk.mark_app_as_ready()` are always recorded. Sampling applies to normal inbound traffic after startup.

### Method 1: Init Parameter (Programmatic Base-Rate Override)

Set the base sampling rate directly in your initialization code:

```python
sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
    sampling_rate=0.1,  # Base rate: 10% of requests
)
```

### Method 2: Environment Variable

Set the `TUSK_RECORDING_SAMPLING_RATE` environment variable to override the base sampling rate:

```bash
# Development - record everything
TUSK_RECORDING_SAMPLING_RATE=1.0 python app.py

# Production - sample 10% of requests
TUSK_RECORDING_SAMPLING_RATE=0.1 python app.py
```

`TUSK_SAMPLING_RATE` is still supported as a backward-compatible alias, but new setups should prefer `TUSK_RECORDING_SAMPLING_RATE`.

### Method 3: Configuration File

Use the nested `recording.sampling` config to choose `fixed` vs `adaptive` mode and set the base/minimum rates.

**Fixed sampling example:**

```yaml
# ... existing configuration ...

recording:
  sampling:
    mode: fixed
    base_rate: 0.1
  export_spans: true
  enable_env_var_recording: true
```

**Adaptive sampling example:**

```yaml
# ... existing configuration ...

recording:
  sampling:
    mode: adaptive
    base_rate: 0.25
    min_rate: 0.01
  export_spans: true
```

**Legacy config still supported:**

```yaml
recording:
  sampling_rate: 0.1
```

### Recording Configuration Options

<table>
  <thead>
    <tr>
      <th>Option</th>
      <th>Type</th>
      <th>Default</th>
      <th>Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><code>sampling.mode</code></td>
      <td><code>"fixed" | "adaptive"</code></td>
      <td><code>"fixed"</code></td>
      <td>Selects constant sampling or adaptive load shedding.</td>
    </tr>
    <tr>
      <td><code>sampling.base_rate</code></td>
      <td><code>float</code></td>
      <td><code>1.0</code></td>
      <td>The base sampling rate (0.0 - 1.0). This is the preferred config key and can be overridden by <code>TUSK_RECORDING_SAMPLING_RATE</code> or the <code>sampling_rate</code> init parameter.</td>
    </tr>
    <tr>
      <td><code>sampling.min_rate</code></td>
      <td><code>float</code></td>
      <td><code>0.001</code> in <code>adaptive</code> mode</td>
      <td>The minimum steady-state sampling rate for adaptive mode. In critical conditions the SDK can still temporarily pause recording.</td>
    </tr>
    <tr>
      <td><code>sampling_rate</code></td>
      <td><code>float</code></td>
      <td><code>None</code></td>
      <td>Legacy fallback for the base sampling rate. Still supported for backward compatibility, but <code>recording.sampling.base_rate</code> is preferred.</td>
    </tr>
    <tr>
      <td><code>export_spans</code></td>
      <td><code>bool</code></td>
      <td><code>false</code></td>
      <td>Whether to export spans to Tusk backend or local files (<code>.tusk/traces</code>). If false, spans are only exported to local files.</td>
    </tr>
    <tr>
      <td><code>enable_env_var_recording</code></td>
      <td><code>bool</code></td>
      <td><code>false</code></td>
      <td>Whether to enable environment variable recording and replaying. Recommended if your application's business logic depends on environment variables.</td>
    </tr>
  </tbody>
</table>

## Mark App as Ready

Once your application has completed initialization (database connections, middleware setup, etc.), mark it as ready:

```python
sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
)

# Your application setup...

# Mark app as ready for recording/replay
sdk.mark_app_as_ready()
print("Server started and ready for Tusk Drift")
```

The `mark_app_as_ready()` call signals to the SDK that your application is fully initialized and ready to handle requests. This ensures that traces are only recorded for requests that occur after your application is properly set up.
