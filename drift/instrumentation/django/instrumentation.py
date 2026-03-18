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
        if not self._try_inject_middleware():
            # Settings not configured yet — defer injection until django.setup() runs
            self._defer_middleware_injection()

    def _try_inject_middleware(self) -> bool:
        """Attempt to inject DriftMiddleware into Django settings.

        Returns:
            True if middleware was injected (or already present), False if
            settings are not yet configured and injection should be deferred.
        """
        global _middleware_injected

        if _middleware_injected:
            logger.debug("Middleware already injected, skipping")
            return True

        try:
            from django.conf import settings

            if not settings.configured:
                logger.debug("Django settings not configured yet, will defer middleware injection")
                return False

            middleware_setting = self._get_middleware_setting(settings)
            if not middleware_setting:
                logger.warning("Could not find middleware setting, cannot inject")
                return True  # Don't retry — this won't change

            current_middleware = list(getattr(settings, middleware_setting, []))

            middleware_path = "drift.instrumentation.django.middleware.DriftMiddleware"
            if middleware_path in current_middleware:
                logger.debug("DriftMiddleware already in settings, skipping injection")
                _middleware_injected = True
                return True

            # Insert at position 0 to capture all requests
            current_middleware.insert(0, middleware_path)
            setattr(settings, middleware_setting, current_middleware)

            from .middleware import DriftMiddleware

            DriftMiddleware.transform_engine = self._transform_engine  # type: ignore

            _middleware_injected = True
            logger.debug(f"Injected DriftMiddleware at position 0 in {middleware_setting}")

            self._force_database_reconnect()

            print("Django instrumentation applied")
            return True

        except ImportError as e:
            logger.warning(f"Could not import Django settings: {e}")
            return True  # Don't retry on import errors
        except Exception as e:
            logger.error(f"Failed to inject middleware: {e}", exc_info=True)
            return True  # Don't retry on unexpected errors

    def _defer_middleware_injection(self) -> None:
        """Monkey-patch django.setup() to inject middleware after settings are configured.

        When TuskDrift.initialize() runs before DJANGO_SETTINGS_MODULE is set
        (common in manage.py where the SDK init is the first import), Django
        settings aren't available yet. This defers injection to run after
        django.setup() completes, which is when settings are guaranteed to be
        configured.
        """
        import django

        original_setup = django.setup

        def patched_setup(*args, **kwargs):
            # Restore original setup first to avoid re-entrance
            django.setup = original_setup
            # Run the original django.setup()
            result = original_setup(*args, **kwargs)
            # Now settings are configured — inject middleware
            self._try_inject_middleware()
            return result

        django.setup = patched_setup
        logger.debug("Deferred middleware injection to django.setup()")

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
