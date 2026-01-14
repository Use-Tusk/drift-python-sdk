"""Version information for the Drift Python SDK."""

import importlib.metadata

try:
    SDK_VERSION = importlib.metadata.version("tusk-drift-python-sdk")
except importlib.metadata.PackageNotFoundError:
    SDK_VERSION = "0.0.0.dev"

# Minimum CLI version required for this SDK
MIN_CLI_VERSION = "0.1.0"

# SDK language identifier
SDK_LANGUAGE = "python"
