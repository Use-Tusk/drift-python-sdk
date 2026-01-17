"""PyJWT instrumentation for REPLAY mode.

Patches PyJWT to disable all verification during test replay:
1. _merge_options - returns all verification options as False
2. _verify_signature - no-op (defense in depth)
3. _validate_claims - no-op (defense in depth)

Only active in REPLAY mode.
"""

from __future__ import annotations

import logging
from types import ModuleType

from ...core.types import TuskDriftMode
from ..base import InstrumentationBase

logger = logging.getLogger(__name__)


class PyJWTInstrumentation(InstrumentationBase):
    """Patches PyJWT to disable verification in REPLAY mode."""

    def __init__(
        self, mode: TuskDriftMode = TuskDriftMode.DISABLED, enabled: bool = True
    ) -> None:
        self.mode = mode
        should_enable = enabled and mode == TuskDriftMode.REPLAY

        super().__init__(
            name="PyJWTInstrumentation",
            module_name="jwt",
            supported_versions="*",
            enabled=should_enable,
        )

    def patch(self, module: ModuleType) -> None:
        if self.mode != TuskDriftMode.REPLAY:
            return

        self._patch_merge_options()
        self._patch_signature_verification()
        self._patch_claim_validation()
        logger.debug("[PyJWTInstrumentation] All patches applied")

    def _patch_signature_verification(self) -> None:
        """No-op signature verification."""
        try:
            from jwt import api_jws

            def patched_verify_signature(self, *args, **kwargs):
                logger.debug("[PyJWTInstrumentation] _verify_signature called - skipping verification")
                return None

            api_jws.PyJWS._verify_signature = patched_verify_signature
            logger.debug("[PyJWTInstrumentation] Patched PyJWS._verify_signature")
        except Exception as e:
            logger.warning(f"[PyJWTInstrumentation] Failed to patch _verify_signature: {e}")

    def _patch_claim_validation(self) -> None:
        """No-op claim validation."""
        try:
            from jwt import api_jwt

            def patched_validate_claims(self, *args, **kwargs):
                logger.debug("[PyJWTInstrumentation] _validate_claims called - skipping validation")
                return None

            api_jwt.PyJWT._validate_claims = patched_validate_claims
            logger.debug("[PyJWTInstrumentation] Patched PyJWT._validate_claims")
        except Exception as e:
            logger.warning(f"[PyJWTInstrumentation] Failed to patch _validate_claims: {e}")

    def _patch_merge_options(self) -> None:
        """Patch _merge_options to always return disabled verification options."""
        try:
            from jwt import api_jwt

            disabled_options = {
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
                "verify_jti": False,
                "require": [],
                "strict_aud": False,
            }

            def patched_merge_options(self, options=None):
                logger.debug("[PyJWTInstrumentation] _merge_options called - returning disabled options")
                return disabled_options

            api_jwt.PyJWT._merge_options = patched_merge_options
            logger.debug("[PyJWTInstrumentation] Patched PyJWT._merge_options")
        except Exception as e:
            logger.warning(f"[PyJWTInstrumentation] Failed to patch _merge_options: {e}")
