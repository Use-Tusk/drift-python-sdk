from __future__ import annotations

import logging
from collections.abc import Iterable
from types import ModuleType
from typing import TYPE_CHECKING, Any, override

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment

from ...core.drift_sdk import TuskDrift
from ..base import InstrumentationBase
from ..http import HttpTransformEngine


class FlaskInstrumentation(InstrumentationBase):
    def __init__(self, enabled: bool = True, transforms: dict[str, Any] | None = None):
        self._transform_engine = HttpTransformEngine(
            self._resolve_http_transforms(transforms)
        )
        super().__init__(
            name="FlaskInstrumentation",
            module_name="flask",
            supported_versions=">=2.0.0",
            enabled=enabled,
        )

    def _resolve_http_transforms(
        self, provided: dict[str, Any] | list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        if isinstance(provided, list):
            return provided
        if isinstance(provided, dict) and isinstance(provided.get("http"), list):
            return provided["http"]

        sdk = TuskDrift.get_instance()
        transforms = getattr(sdk.config, "transforms", None)
        if isinstance(transforms, dict) and isinstance(transforms.get("http"), list):
            return transforms["http"]
        return None

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch Flask to capture HTTP requests/responses"""
        flask_class = getattr(module, "Flask", None)
        if not flask_class:
            logger.warning("Flask.Flask class not found")
            return

        original_wsgi_app: WSGIApplication = flask_class.wsgi_app  # pyright: ignore[reportAny]
        transform_engine = self._transform_engine

        # wraps(original) = functools.update_wrapper(instrumented, original)
        def instrumented_wsgi_app(
            self: WSGIApplication,
            environ: WSGIEnvironment,
            start_response: StartResponse,
        ) -> Iterable[bytes]:
            # Delegate to generic WSGI handler
            from ..wsgi.handler import handle_wsgi_request

            return handle_wsgi_request(
                self,
                environ,
                start_response,
                original_wsgi_app,
                framework_name="flask",
                instrumentation_name="FlaskInstrumentation",
                transform_engine=transform_engine,
            )

        flask_class.wsgi_app = instrumented_wsgi_app  # type: ignore
        print("Flask instrumentation applied")
