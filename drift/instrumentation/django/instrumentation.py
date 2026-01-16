from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any

from typing_extensions import override

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

from ..base import InstrumentationBase
from ..http import HttpTransformEngine

_middleware_injected = False


class DjangoInstrumentation(InstrumentationBase):
    """Django instrumentation via middleware injection."""

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None):
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))
        super().__init__(
            name="DjangoInstrumentation",
            module_name="django",
            supported_versions=">=3.2.0",
            enabled=enabled,
        )

    def _resolve_http_transforms(
        self, provided: dict[str, Any] | list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """Resolve HTTP transforms from provided config or SDK config."""
        if isinstance(provided, list):
            return provided
        if isinstance(provided, dict) and isinstance(provided.get("http"), list):
            return provided["http"]

        from ...core.drift_sdk import TuskDrift

        sdk = TuskDrift.get_instance()
        transforms = getattr(sdk.config, "transforms", None)
        if isinstance(transforms, dict) and isinstance(transforms.get("http"), list):
            return transforms["http"]
        return None

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch Django by injecting middleware."""
        global _middleware_injected

        if _middleware_injected:
            logger.debug("Middleware already injected, skipping")
            return

        try:
            from django.conf import settings

            if not settings.configured:
                logger.warning("Django settings not configured, cannot inject middleware")
                return

            middleware_setting = self._get_middleware_setting(settings)
            if not middleware_setting:
                logger.warning("Could not find middleware setting, cannot inject")
                return

            current_middleware = list(getattr(settings, middleware_setting, []))

            middleware_path = "drift.instrumentation.django.middleware.DriftMiddleware"
            if middleware_path in current_middleware:
                logger.debug("DriftMiddleware already in settings, skipping injection")
                _middleware_injected = True
                return

            # Insert at position 0 to capture all requests
            current_middleware.insert(0, middleware_path)
            setattr(settings, middleware_setting, current_middleware)

            from .middleware import DriftMiddleware

            DriftMiddleware.transform_engine = self._transform_engine  # type: ignore

            _middleware_injected = True
            logger.debug(f"Injected DriftMiddleware at position 0 in {middleware_setting}")

            self._force_database_reconnect()

            print("Django instrumentation applied")

        except ImportError as e:
            logger.warning(f"Could not import Django settings: {e}")
        except Exception as e:
            logger.error(f"Failed to inject middleware: {e}", exc_info=True)

    def _force_database_reconnect(self) -> None:
        """Force Django to close and recreate database connections."""
        try:
            from django.db import connections

            for conn in connections.all():
                if conn.connection is not None:
                    conn.close()
            logger.debug("Closed all database connections to force reconnect with instrumentation")

        except Exception as e:
            logger.warning(f"[DjangoInstrumentation] Failed to close database connections: {e}")

    def _get_middleware_setting(self, settings: Any) -> str | None:
        """Detect which middleware setting name to use.

        Django 1.10+ uses MIDDLEWARE, older versions use MIDDLEWARE_CLASSES.

        Args:
            settings: Django settings object

        Returns:
            The middleware setting name, or None if not found
        """
        if hasattr(settings, "MIDDLEWARE"):
            return "MIDDLEWARE"
        elif hasattr(settings, "MIDDLEWARE_CLASSES"):
            return "MIDDLEWARE_CLASSES"
        return None
