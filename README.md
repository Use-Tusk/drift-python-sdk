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

- **Flask**: `flask>=2.0.0`
- **FastAPI**: `fastapi>=0.68.0`
- **Django**: `django>=3.2.0`
- **Requests**: `requests` (all versions)
- **HTTPX**: `httpx` (all versions)
- **psycopg**: `psycopg>=3.0.0`, `psycopg2>=2.8.0`
- **Redis**: `redis` (all versions)

If you're using packages or versions not listed above, please create an issue with the package + version you'd like an instrumentation for.

## Installation

### Step 1: Install the CLI

First, install and configure the Tusk Drift CLI by following our [CLI installation guide](https://github.com/Use-Tusk/tusk-drift-cli?tab=readme-ov-file#install). The CLI helps set up your Tusk configuration file and replays tests.

The wizard will eventually direct you back here when it's time to set up the SDK.

### Step 2: Install the SDK

After completing the CLI wizard, install the SDK:

```bash
pip install tusk-drift-python-sdk
```

### Step 3: Initialize the SDK for your service

Refer to our [initialization guide](docs/initialization.md) to set up the SDK for your service.

### Step 4: Run Your First Test

Follow along our [quick start guide](docs/quickstart.md) to record and replay your first test!

## Troubleshooting

Having issues?

- Check our [initialization guide](docs/initialization.md) for common setup issues
- Create an issue or reach us at [support@usetusk.ai](mailto:support@usetusk.ai).

## Community

Join our open source community on [Slack](https://join.slack.com/t/tusk-community/shared_invite/zt-3fve1s7ie-NAAUn~UpHsf1m_2tdoGjsQ).

## Contributing

We appreciate feedback and contributions. See [CONTRIBUTING.md](/CONTRIBUTING.md).

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.
