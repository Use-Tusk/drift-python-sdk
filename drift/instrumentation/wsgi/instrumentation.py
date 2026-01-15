"""Generic WSGI instrumentation for any WSGI-compliant application.

This module provides a framework-agnostic WSGI instrumentation that can be used to
instrument custom WSGI applications or frameworks that aren't explicitly supported.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any

from typing_extensions import override

if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIApplication


from ..base import InstrumentationBase
from ..http import HttpTransformEngine

logger = logging.getLogger(__name__)


class WsgiInstrumentation(InstrumentationBase):
    """Generic WSGI instrumentation that can wrap any WSGI application.

    This instrumentation provides the core WSGI request/response tracing logic
    that can be used by framework-specific instrumentations (Flask, Bottle, etc.)
    or applied to custom WSGI applications.

    Args:
        enabled: Whether instrumentation is enabled
        transforms: HTTP transforms configuration
        framework_name: Name of framework for span attribution (default: "wsgi")

    Example:
        >>> from drift import TuskDrift
        >>> from drift.instrumentation import WsgiInstrumentation
        >>>
        >>> sdk = TuskDrift.initialize(api_key="...")
        >>>
        >>> # Custom WSGI app
        >>> def my_wsgi_app(environ, start_response):
        ...     status = '200 OK'
        ...     headers = [('Content-Type', 'text/plain')]
        ...     start_response(status, headers)
        ...     return [b'Hello WSGI']
        >>>
        >>> # Wrap with instrumentation
        >>> wsgi_instr = WsgiInstrumentation(framework_name="my-app")
        >>> wrapped_app = wsgi_instr.wrap_wsgi_app(my_wsgi_app)
    """

    def __init__(
        self,
        enabled: bool = True,
        transforms: dict[str, Any] | list[dict[str, Any]] | None = None,
        framework_name: str = "wsgi",
    ):
        """Initialize WSGI instrumentation.

        Args:
            enabled: Whether instrumentation is enabled
            transforms: HTTP transforms configuration (can be dict or list)
            framework_name: Name of framework for span attribution
        """
        self._framework_name = framework_name
        self._transform_engine = HttpTransformEngine(self._resolve_http_transforms(transforms))

        # Note: We don't call super().__init__() with a module_name
        # because this is meant to be used programmatically, not via import hooks
        # Framework-specific instrumentations will use import hooks
        self.name = f"{framework_name.title()}WsgiInstrumentation"
        self.module_name = ""  # No automatic patching
        self.supported_versions = "*"
        self.enabled = enabled

    def _resolve_http_transforms(
        self, provided: dict[str, Any] | list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """Resolve HTTP transforms from provided config or SDK config.

        Args:
            provided: Provided transform configuration

        Returns:
            List of HTTP transforms or None
        """
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
        """Not used - this instrumentation is applied programmatically.

        This method exists to satisfy the InstrumentationBase interface but
        is not used for WSGI instrumentation. Use wrap_wsgi_app() instead.
        """
        pass

    def wrap_wsgi_app(self, wsgi_app: WSGIApplication) -> WSGIApplication:
        """Wrap a WSGI application to add tracing.

        Args:
            wsgi_app: The WSGI application callable to wrap

        Returns:
            Wrapped WSGI application with tracing enabled

        Example:
            >>> instrumentation = WsgiInstrumentation(framework_name="bottle")
            >>> wrapped_app = instrumentation.wrap_wsgi_app(bottle_app)
        """
        from .handler import handle_wsgi_request

        transform_engine = self._transform_engine
        framework_name = self._framework_name
        instrumentation_name = self.name

        # Create a wrapper that matches the WsgiAppMethod signature (app, environ, start_response)
        # This allows handle_wsgi_request to work with both Flask-like unbound methods
        # and plain WSGI apps
        def wsgi_app_method(app, environ, start_response):  # type: ignore[no-untyped-def]
            # Ignore the app parameter and call the original WSGI app directly
            return wsgi_app(environ, start_response)

        def instrumented_wsgi_app(environ, start_response):
            return handle_wsgi_request(
                wsgi_app,
                environ,
                start_response,
                wsgi_app_method,
                framework_name=framework_name,
                instrumentation_name=instrumentation_name,
                transform_engine=transform_engine,
            )

        return instrumented_wsgi_app
