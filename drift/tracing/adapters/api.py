"""API span adapter for exporting spans to Tusk backend."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, override

from .base import ExportResult, SpanExportAdapter

if TYPE_CHECKING:
    import aiohttp

    from ...core.types import CleanSpanData

logger = logging.getLogger(__name__)

DRIFT_API_PATH = "/api/drift"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0


@dataclass
class ApiSpanAdapterConfig:
    """Configuration for the API span adapter."""

    api_key: str
    tusk_backend_base_url: str
    observable_service_id: str
    environment: str
    sdk_version: str
    sdk_instance_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES


class ApiSpanAdapter(SpanExportAdapter):
    """
    Exports spans to Tusk backend API via HTTP/Twirp.

    Uses the SpanExportService to send spans to the backend.
    """

    def __init__(self, config: ApiSpanAdapterConfig) -> None:
        """
        Initialize the API adapter.

        Args:
            config: Configuration for connecting to the Tusk backend
        """
        self._config = config
        self._base_url = f"{config.tusk_backend_base_url}{DRIFT_API_PATH}"
        self._session: aiohttp.ClientSession | None = None

        logger.debug("ApiSpanAdapter initialized")

    def __repr__(self) -> str:
        return f"ApiSpanAdapter(url={self._base_url}, env={self._config.environment})"

    @property
    @override
    def name(self) -> str:
        return "api"

    async def _get_session(self) -> "aiohttp.ClientSession":
        """Get or create the aiohttp session."""
        import aiohttp

        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @override
    async def export_spans(self, spans: list["CleanSpanData"]) -> ExportResult:
        """Export spans to the Tusk backend API with retry logic."""
        try:
            import aiohttp
        except ImportError:
            logger.error("aiohttp is required for API adapter. Install it with: pip install aiohttp")
            return ExportResult.failed("aiohttp is required for API adapter")

        # Transform spans to protobuf format
        proto_spans = [self._transform_span_to_proto(span) for span in spans]

        # Build the request
        request_data = {
            "observable_service_id": self._config.observable_service_id,
            "environment": self._config.environment,
            "sdk_version": self._config.sdk_version,
            "sdk_instance_id": self._config.sdk_instance_id,
            "spans": proto_spans,
        }

        url = f"{self._base_url}/SpanExportService/ExportSpans"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._config.api_key,
            "x-td-skip-instrumentation": "true",
        }

        last_error: Exception | None = None
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(self._config.max_retries):
            try:
                session = await self._get_session()
                async with session.post(url, json=request_data, headers=headers) as response:
                    if response.status == 200:
                        result = await response.json()
                        if not result.get("success", True):
                            raise Exception(f"API export failed: {result.get('message', 'Unknown error')}")
                        logger.debug("Successfully exported %d spans to %s", len(spans), url)
                        return ExportResult.success()

                    error_text = await response.text()
                    last_error = Exception(f"API export failed (status {response.status}): {error_text}")

                    # Don't retry on client errors (4xx)
                    if 400 <= response.status < 500:
                        logger.error("Client error exporting spans to %s: %s", url, error_text)
                        return ExportResult.failed(last_error)

            except aiohttp.ClientError as e:
                last_error = e
                logger.warning("Attempt %d failed: %s", attempt + 1, e)

            except Exception as e:
                last_error = e
                logger.warning("Attempt %d failed: %s", attempt + 1, e)

            # Exponential backoff before retry
            if attempt < self._config.max_retries - 1:
                await asyncio.sleep(backoff)
                backoff *= 2

        logger.error("Failed to export spans after %d attempts: %s", self._config.max_retries, last_error)
        return ExportResult.failed(last_error or Exception("Unknown error"))

    @override
    async def shutdown(self) -> None:
        """Shutdown and close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _transform_span_to_proto(self, span: "CleanSpanData") -> dict[str, Any]:
        """Transform CleanSpanData to protobuf-compatible dictionary."""
        from ...core.json_schema_helper import JsonSchema
        from ...core.types import PackageType, SpanKind, StatusCode

        # Convert timestamp and duration to protobuf format
        timestamp = datetime.fromtimestamp(
            span.timestamp.seconds + span.timestamp.nanos / 1_000_000_000,
            tz=timezone.utc,
        )
        duration = timedelta(
            seconds=span.duration.seconds,
            microseconds=span.duration.nanos // 1000,
        )

        result: dict[str, Any] = {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "package_name": span.package_name,
            "instrumentation_name": span.instrumentation_name,
            "submodule_name": span.submodule_name,
            "package_type": span.package_type.value if isinstance(span.package_type, PackageType) else 0,
            "input_value": span.input_value or {},
            "output_value": span.output_value or {},
            "input_schema": self._schema_to_dict(span.input_schema),
            "output_schema": self._schema_to_dict(span.output_schema),
            "input_schema_hash": span.input_schema_hash or "",
            "output_schema_hash": span.output_schema_hash or "",
            "input_value_hash": span.input_value_hash or "",
            "output_value_hash": span.output_value_hash or "",
            "kind": span.kind.value if isinstance(span.kind, SpanKind) else 0,
            "status": {
                "code": span.status.code.value if isinstance(span.status.code, StatusCode) else 0,
                "message": span.status.message or "",
            },
            "is_pre_app_start": span.is_pre_app_start,
            "timestamp": timestamp.isoformat(),
            "duration": duration.total_seconds(),
            "is_root_span": span.is_root_span,
        }

        # Add metadata if present
        if span.metadata is not None:
            result["metadata"] = {
                k: v for k, v in span.metadata.__dict__.items() if v is not None
            }

        return result

    def _schema_to_dict(self, schema: Any) -> dict[str, Any]:
        """Convert JsonSchema to a dictionary."""
        from ...core.json_schema_helper import JsonSchema

        if schema is None:
            return {"type": 0, "properties": {}}

        if isinstance(schema, JsonSchema):
            return schema.to_primitive()

        return schema


def create_api_adapter(
    api_key: str,
    observable_service_id: str,
    environment: str = "development",
    sdk_version: str = "0.1.0",
    sdk_instance_id: str | None = None,
    tusk_backend_base_url: str = "https://api.usetusk.ai",
) -> ApiSpanAdapter:
    """
    Create an API span adapter with the given configuration.

    Args:
        api_key: Tusk API key for authentication
        observable_service_id: ID of the observable service in Tusk
        environment: Environment name (e.g., "development", "production")
        sdk_version: Version of the SDK
        sdk_instance_id: Unique ID for this SDK instance (auto-generated if not provided)
        tusk_backend_base_url: Base URL for the Tusk backend

    Returns:
        Configured ApiSpanAdapter instance
    """
    import uuid

    if sdk_instance_id is None:
        sdk_instance_id = str(uuid.uuid4())

    config = ApiSpanAdapterConfig(
        api_key=api_key,
        tusk_backend_base_url=tusk_backend_base_url,
        observable_service_id=observable_service_id,
        environment=environment,
        sdk_version=sdk_version,
        sdk_instance_id=sdk_instance_id,
    )

    return ApiSpanAdapter(config)
