<p align="center">
  <img src="https://github.com/Use-Tusk/drift-python-sdk/raw/main/images/tusk-banner.png" alt="Tusk Drift Banner">
</p>

<p align="center">
  <a href="https://pypi.org/project/tusk-drift-python-sdk/"><img src="https://img.shields.io/pypi/v/tusk-drift-python-sdk" alt="PyPI version"></a>
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License: Apache 2.0"></a>
  <a href="https://github.com/Use-Tusk/drift-python-sdk/commits/main/"><img src="https://img.shields.io/github/last-commit/Use-Tusk/drift-python-sdk" alt="GitHub last commit"></a>
  <a href="https://x.com/usetusk"><img src="https://img.shields.io/twitter/url?url=https%3A%2F%2Fx.com%2Fusetusk&style=flat&logo=x&label=Tusk&color=BF40BF" alt="Tusk X account"></a>
  <a href="https://join.slack.com/t/tusk-community/shared_invite/zt-3fve1s7ie-NAAUn~UpHsf1m_2tdoGjsQ"><img src="https://img.shields.io/badge/slack-badge?style=flat&logo=slack&label=Tusk&color=BF40BF" alt="Tusk Community Slack"></a>
</p>

The Python Tusk Drift SDK enables fast and deterministic API testing by capturing and replaying API calls made to/from your service. Automatically record real-world API calls, then replay them as tests using the [Tusk CLI](https://github.com/Use-Tusk/tusk-drift-cli) to find regressions. During replay, all outbound requests are intercepted with recorded data to ensure consistent behavior without side-effects.

<div align="center">

![Demo](images/demo.gif)

<p><a href="https://github.com/Use-Tusk/drift-python-demo">Try it on a demo repo →</a></p>

</div>

## Documentation

For comprehensive guides and API reference, visit our [full documentation](https://docs.usetusk.ai/api-tests/installation#setup).

### SDK Guides

- [Initialization Guide](docs/initialization.md) - Set up the SDK in your Python application
- [Environment Variables](docs/environment-variables.md) - Environment variables reference
- [Quick Start Guide](docs/quickstart.md) - Record and replay your first trace

<div align="center">

![Tusk Drift Animated Diagram](images/tusk-drift-animated-diagram-light.gif#gh-light-mode-only)
![Tusk Drift Animated Diagram](images/tusk-drift-animated-diagram-dark.gif#gh-dark-mode-only)

</div>

## Requirements

- Python 3.9+

Tusk Drift currently supports the following packages and versions:

| Package | Supported Versions |
|---------|-------------------|
| Flask | `>=2.0.0` |
| FastAPI | `>=0.68.0` |
| Django | `>=3.2.0` |
| Requests | all versions |
| HTTPX | all versions |
| aiohttp | all versions |
| urllib3 | all versions |
| psycopg | `>=3.1.12` |
| psycopg2 | all versions |
| Redis | `>=4.0.0` |
| Kinde | `>=2.0.1` |

If you're using packages or versions not listed above, please create an issue with the package + version you'd like an instrumentation for.

## Installation

### Step 1: Install the CLI

First, install the Tusk Drift CLI by following our [CLI installation guide](https://github.com/Use-Tusk/tusk-drift-cli?tab=readme-ov-file#install).

### Step 2: Set up Tusk Drift

#### AI-powered setup (recommended)

Use our AI agent to automatically set up Tusk Drift for your service:

```bash
cd path/to/your/service
export ANTHROPIC_API_KEY=your-api-key
tusk setup
```

The agent will analyze your codebase, install the SDK, instrument it into your application, create configuration files, and test the setup with recording and replay.

#### Manual setup

Alternatively, you can set up Tusk Drift manually:

1. Install the SDK:

   ```bash
   pip install tusk-drift-python-sdk
   ```

2. Create configuration: Run `tusk init` to create your `.tusk/config.yaml` config file interactively, or create it manually per the [configuration docs](https://github.com/Use-Tusk/tusk-drift-cli/blob/main/docs/configuration.md).

3. Initialize the SDK: Refer to the [initialization guide](docs/initialization.md) to instrument the SDK in your service.

4. Record and replay: Follow the [quick start guide](docs/quickstart.md) to record and replay your first test!

## Troubleshooting

Having issues?

- Check our [initialization guide](docs/initialization.md) for common setup issues
- Create an issue or reach us at [support@usetusk.ai](mailto:support@usetusk.ai).

## Community

Join our open source community on [Slack](https://join.slack.com/t/tusk-community/shared_invite/zt-3fve1s7ie-NAAUn~UpHsf1m_2tdoGjsQ).
