# Initialization

## Prerequisites

Before setting up the SDK, ensure you have:

- Completed the [CLI wizard](https://github.com/Use-Tusk/tusk-drift-cli?tab=readme-ov-file#quick-start)
- Obtained an API key from the [Tusk Drift dashboard](https://usetusk.ai/app/settings/api-keys) (only required if using Tusk Cloud)
- Python 3.12 or later installed

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
      <td><code>os.environ.get("NODE_ENV", "development")</code></td>
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
      <td><code>1.0</code></td>
      <td>Override sampling rate (0.0 - 1.0) for recording. Takes precedence over <code>TUSK_SAMPLING_RATE</code> env var and config file.</td>
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

The sampling rate determines what percentage of requests are recorded during replay tests. Tusk Drift supports three ways to configure the sampling rate, with the following precedence (highest to lowest):

1. **Init Parameter**
2. **Environment Variable** (`TUSK_SAMPLING_RATE`)
3. **Configuration File** (`.tusk/config.yaml`)

If not specified, the default sampling rate is `1.0` (100%).

### Method 1: Init Parameter (Programmatic Override)

Set the sampling rate directly in your initialization code:

```python
sdk = TuskDrift.initialize(
    api_key=os.environ.get("TUSK_API_KEY"),
    sampling_rate=0.1,  # 10% of requests
)
```

### Method 2: Environment Variable

Set the `TUSK_SAMPLING_RATE` environment variable:

```bash
# Development - record everything
TUSK_SAMPLING_RATE=1.0 python app.py

# Production - sample 10% of requests
TUSK_SAMPLING_RATE=0.1 python app.py
```

### Method 3: Configuration File

Update the configuration file `.tusk/config.yaml` to include a `recording` section:

```yaml
# ... existing configuration ...

recording:
  sampling_rate: 0.1
  export_spans: true
  enable_env_var_recording: true
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
      <td><code>sampling_rate</code></td>
      <td><code>float</code></td>
      <td><code>1.0</code></td>
      <td>The sampling rate (0.0 - 1.0). 1.0 means 100% of requests are recorded, 0.0 means 0% of requests are recorded.</td>
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
