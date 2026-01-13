"""Instrumentation for Kinde SDK authentication.

Patches is_authenticated() to always return True in REPLAY mode,
allowing tests to bypass authentication checks during replay.
"""

from __future__ import annotations

import logging
from typing import Any

from ..base import InstrumentationBase

logger = logging.getLogger(__name__)


class KindeInstrumentation(InstrumentationBase):
    """Instrumentation for the Kinde SDK authentication library.

    Patches OAuth.is_authenticated() to:
    - Return True in REPLAY mode (bypass authentication)
    - Call the original method in RECORD and DISABLED modes

    Since SmartOAuth and AsyncOAuth delegate to OAuth.is_authenticated(),
    patching OAuth covers all authentication entry points.
    """

    def __init__(self, enabled: bool = True) -> None:
        super().__init__(
            name="KindeInstrumentation",
            module_name="kinde_sdk.auth.oauth",
            supported_versions="*",
            enabled=enabled,
        )

    def patch(self, module: Any) -> None:
        """Patch the kinde_sdk.auth.oauth module.

        Patches OAuth.is_authenticated() to return True in REPLAY mode.
        """
        if not hasattr(module, "OAuth"):
            logger.warning("kinde_sdk.auth.oauth.OAuth not found, skipping instrumentation")
            return

        original_is_authenticated = module.OAuth.is_authenticated

        def patched_is_authenticated(oauth_self) -> bool:
            """Patched is_authenticated method.

            Args:
                oauth_self: OAuth instance

            Returns:
                True in REPLAY mode, otherwise delegates to original method
            """
            # Lazy imports to avoid circular dependency
            from ...core.drift_sdk import TuskDrift
            from ...core.types import TuskDriftMode

            sdk = TuskDrift.get_instance()

            # In REPLAY mode, always return True to bypass authentication
            if sdk.mode == TuskDriftMode.REPLAY:
                logger.debug("[KindeInstrumentation] REPLAY mode: returning True for is_authenticated")
                return True

            # In RECORD or DISABLED mode, call the original method
            return original_is_authenticated(oauth_self)

        module.OAuth.is_authenticated = patched_is_authenticated
        logger.info("kinde_sdk.auth.oauth.OAuth.is_authenticated instrumented")
