"""Django instrumentation for Drift SDK."""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any, override, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...core.drift_sdk import TuskDrift

from ..base import InstrumentationBase
from ..http import HttpTransformEngine

# Global flag to prevent duplicate middleware injection
_middleware_injected = False


class DjangoInstrumentation(InstrumentationBase):
    """Django instrumentation via middleware injection.

    Injects DriftMiddleware into Django's middleware stack at position 0
    (beginning of the stack) to capture all HTTP requests/responses.

    Args:
        enabled: Whether instrumentation is enabled
        transforms: HTTP transform configuration
    """

    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None):
        self._transform_engine = HttpTransformEngine(
            self._resolve_http_transforms(transforms)
        )
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
        """Patch Django by injecting middleware into settings.

        Args:
            module: The Django module
        """
        global _middleware_injected

        if _middleware_injected:
            logger.debug("[DjangoInstrumentation] Middleware already injected, skipping")
            return

        try:
            # Import Django settings
            from django.conf import settings

            # Check if settings are configured
            if not settings.configured:
                logger.warning(
                    "[DjangoInstrumentation] Django settings not configured, cannot inject middleware"
                )
                return

            # Detect middleware setting name (MIDDLEWARE vs MIDDLEWARE_CLASSES)
            middleware_setting = self._get_middleware_setting(settings)
            if not middleware_setting:
                logger.warning(
                    "[DjangoInstrumentation] Could not find middleware setting, cannot inject"
                )
                return

            # Get current middleware list
            current_middleware = list(getattr(settings, middleware_setting, []))

            # Check if our middleware is already present
            middleware_path = "drift.instrumentation.django.middleware.DriftMiddleware"
            if middleware_path in current_middleware:
                logger.debug(
                    "[DjangoInstrumentation] DriftMiddleware already in settings, skipping injection"
                )
                _middleware_injected = True
                return

            # Insert DriftMiddleware at position 0 (beginning of stack)
            # This ensures we capture all requests, including those rejected by later middleware
            current_middleware.insert(0, middleware_path)

            # Update Django settings
            setattr(settings, middleware_setting, current_middleware)

            # Set transform engine on middleware class
            from .middleware import DriftMiddleware

            DriftMiddleware.transform_engine = self._transform_engine  # type: ignore

            _middleware_injected = True
            logger.info(
                f"[DjangoInstrumentation] Injected DriftMiddleware at position 0 in {middleware_setting}"
            )
            
            # Close all existing database connections to force Django to recreate them
            # with our patched psycopg2.connect (which includes cursor_factory)
            self._force_database_reconnect()
            
            print("Django instrumentation applied")

        except ImportError as e:
            logger.warning(
                f"[DjangoInstrumentation] Could not import Django settings: {e}"
            )
        except Exception as e:
            logger.error(
                f"[DjangoInstrumentation] Failed to inject middleware: {e}",
                exc_info=True,
            )

    def _force_database_reconnect(self) -> None:
        """Force Django to close and recreate database connections.
        
        This ensures that connections use our instrumented psycopg2.connect
        with cursor_factory applied.
        """
        try:
            from django.db import connections
            for conn in connections.all():
                if conn.connection is not None:
                    logger.debug(f"[DjangoInstrumentation] Closing connection: {conn.alias}")
                    conn.close()
            logger.info("[DjangoInstrumentation] Closed all database connections to force reconnect with instrumentation")
            
            # Also patch Django's cursor creation directly
            self._patch_django_cursor_creation()
        except Exception as e:
            logger.warning(f"[DjangoInstrumentation] Failed to close database connections: {e}")
    
    def _patch_django_cursor_creation(self) -> None:
        """Patch Django's database cursor creation to use instrumented cursors."""
        try:
            from django.db.backends.postgresql.base import DatabaseWrapper
            from ...core.drift_sdk import TuskDrift
            # Import the psycopg2 instrumentation instance
            from ..psycopg2 import instrumentation as psycopg2_instr_module
            
            original_cursor = DatabaseWrapper.cursor
            sdk = TuskDrift.get_instance()
            
            # Get the psycopg2 instrumentation instance
            psycopg2_instr = psycopg2_instr_module._instance
            if not psycopg2_instr:
                logger.warning("[DjangoInstrumentation] Psycopg2Instrumentation instance not found")
                return
            
            def patched_cursor(self):
                """Patched Django cursor() method."""
                logger.debug("[DJANGO_CURSOR] Creating cursor via Django")
                cursor = original_cursor(self)
                logger.debug(f"[DJANGO_CURSOR] Got cursor: {type(cursor)}")
                
                # Wrap the cursor with our instrumentation
                # We need to replace execute/executemany methods
                original_execute = cursor.execute
                original_executemany = cursor.executemany
                
                def wrapped_execute(query, vars=None):
                    logger.debug(f"[DJANGO_CURSOR] wrapped_execute called")
                    return psycopg2_instr._traced_execute(cursor, original_execute, sdk, query, vars)
                
                def wrapped_executemany(query, vars_list):
                    logger.debug(f"[DJANGO_CURSOR] wrapped_executemany called")
                    return psycopg2_instr._traced_executemany(cursor, original_executemany, sdk, query, vars_list)
                
                cursor.execute = wrapped_execute
                cursor.executemany = wrapped_executemany
                logger.debug("[DJANGO_CURSOR] Wrapped cursor execute/executemany methods")
                
                return cursor
            
            DatabaseWrapper.cursor = patched_cursor
            logger.info("[DjangoInstrumentation] Patched Django PostgreSQL cursor creation")
            
        except Exception as e:
            logger.warning(f"[DjangoInstrumentation] Failed to patch Django cursor creation: {e}", exc_info=True)

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
