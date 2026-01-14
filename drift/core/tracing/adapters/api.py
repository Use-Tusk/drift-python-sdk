"""API span adapter for exporting spans to Tusk backend via native binary protobuf.

This adapter uses betterproto to serialize protobuf messages to binary format
and sends them directly to the Tusk backend over HTTP.

Features:
- Retry with exponential backoff for transient failures
- Circuit breaker to prevent cascading failures
- Optional gzip compression for large payloads (currently disabled by default)
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, override

from ...resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    RetryConfig,
    retry_async,
)
from .base import ExportResult, SpanExportAdapter

if TYPE_CHECKING:
    from betterproto.lib.google.protobuf import Struct

    from ...types import CleanSpanData

logger = logging.getLogger(__name__)

DRIFT_API_PATH = "/api/drift/tusk.drift.backend.v1.SpanExportService/ExportSpans"

# Compression threshold (bytes) - only compress if payload exceeds this
COMPRESSION_THRESHOLD = 1024  # 1KB


@dataclass
class ApiSpanAdapterConfig:
    """Configuration for the API span adapter."""

    api_key: str
    tusk_backend_base_url: str
    observable_service_id: str
    environment: str
    sdk_version: str
    sdk_instance_id: str

    # Retry configuration
    max_retry_attempts: int = 3
    initial_retry_delay_seconds: float = 0.1
    max_retry_delay_seconds: float = 10.0

    # Circuit breaker configuration
    circuit_failure_threshold: int = 5
    circuit_timeout_seconds: float = 30.0

    # Compression (disabled by default, matches Node SDK behavior)
    enable_compression: bool = False
    compression_threshold: int = COMPRESSION_THRESHOLD


class ApiSpanAdapter(SpanExportAdapter):
    """
    Exports spans to Tusk backend API via native binary protobuf.

    Uses betterproto to serialize protobuf messages to binary format and
    sends them directly to the backend over HTTP.

    Features:
    - Automatic retry with exponential backoff for transient failures
    - Circuit breaker to fail fast when backend is unavailable
    - Optional gzip compression for large payloads
    """

    def __init__(self, config: ApiSpanAdapterConfig) -> None:
        """
        Initialize the API adapter.

        Args:
            config: Configuration for connecting to the Tusk backend
        """
        self._config = config
        self._base_url = f"{config.tusk_backend_base_url}{DRIFT_API_PATH}"

        # Initialize retry configuration
        self._retry_config = RetryConfig(
            max_attempts=config.max_retry_attempts,
            initial_delay_seconds=config.initial_retry_delay_seconds,
            max_delay_seconds=config.max_retry_delay_seconds,
        )

        # Initialize circuit breaker
        self._circuit_breaker = CircuitBreaker(
            name="api_export",
            config=CircuitBreakerConfig(
                failure_threshold=config.circuit_failure_threshold,
                timeout_seconds=config.circuit_timeout_seconds,
            ),
        )

        # Statistics
        self._spans_exported = 0
        self._spans_failed = 0
        self._bytes_sent = 0
        self._bytes_compressed = 0

        logger.debug(
            f"ApiSpanAdapter initialized with native protobuf serialization "
            f"(retry={config.max_retry_attempts}, compression={config.enable_compression})"
        )

    def __repr__(self) -> str:
        return f"ApiSpanAdapter(url={self._base_url}, env={self._config.environment})"

    @property
    @override
    def name(self) -> str:
        return "api"

    @property
    def spans_exported(self) -> int:
        """Total number of spans successfully exported."""
        return self._spans_exported

    @property
    def spans_failed(self) -> int:
        """Total number of spans that failed to export."""
        return self._spans_failed

    @property
    def bytes_sent(self) -> int:
        """Total bytes sent (after compression if enabled)."""
        return self._bytes_sent

    @property
    def circuit_state(self) -> str:
        """Current circuit breaker state."""
        return self._circuit_breaker.state.value

    @override
    async def export_spans(self, spans: list[CleanSpanData]) -> ExportResult:
        """Export spans to the Tusk backend API using native binary protobuf.

        Features:
        - Circuit breaker to fail fast if backend is unavailable
        - Retry with exponential backoff for transient failures
        - Optional gzip compression for large payloads
        """
        # Check circuit breaker first
        if not self._circuit_breaker.allow_request():
            logger.warning(f"Circuit breaker is open, dropping {len(spans)} spans")
            self._spans_failed += len(spans)
            return ExportResult.failed(CircuitOpenError("api_export"))

        try:
            # Define the export operation for retry
            async def do_export() -> ExportResult:
                return await self._do_export(spans)

            # Execute with retry
            result = await retry_async(
                do_export,
                config=self._retry_config,
                retryable_exceptions=(Exception,),  # Retry all exceptions
                operation_name="span_export",
            )

            # Record success
            self._circuit_breaker.record_success()
            self._spans_exported += len(spans)
            return result

        except Exception as error:
            # Record failure for circuit breaker
            self._circuit_breaker.record_failure()
            self._spans_failed += len(spans)
            logger.error(f"Failed to export spans after retries: {error}")
            return ExportResult.failed(error if isinstance(error, Exception) else Exception(str(error)))

    async def _do_export(self, spans: list[CleanSpanData]) -> ExportResult:
        """Perform the actual export to the backend."""
        import aiohttp
        from tusk.drift.backend.v1 import ExportSpansRequest, ExportSpansResponse

        proto_spans = [self._transform_span_to_protobuf(span) for span in spans]

        # Build the protobuf request
        request = ExportSpansRequest(
            observable_service_id=self._config.observable_service_id,
            environment=self._config.environment,
            sdk_version=self._config.sdk_version,
            sdk_instance_id=self._config.sdk_instance_id,
            spans=proto_spans,
        )

        request_bytes = bytes(request)
        original_size = len(request_bytes)

        # Apply compression if enabled and payload is large enough
        headers = {
            "Accept": "application/protobuf",
            "x-api-key": self._config.api_key,
            "x-td-skip-instrumentation": "true",
        }

        if self._config.enable_compression and original_size >= self._config.compression_threshold:
            request_bytes = gzip.compress(request_bytes, compresslevel=6)
            headers["Content-Type"] = "application/protobuf"
            headers["Content-Encoding"] = "gzip"
            self._bytes_compressed += original_size - len(request_bytes)
            logger.debug(
                f"Compressed {original_size} -> {len(request_bytes)} bytes "
                f"({100 * len(request_bytes) / original_size:.1f}%)"
            )
        else:
            headers["Content-Type"] = "application/protobuf"

        self._bytes_sent += len(request_bytes)

        async with (
            aiohttp.ClientSession() as session,
            session.post(self._base_url, data=request_bytes, headers=headers) as http_response,
        ):
            if http_response.status >= 500:
                # Server errors are retryable
                error_text = await http_response.text()
                raise Exception(f"Server error (status {http_response.status}): {error_text}")
            elif http_response.status != 200:
                # Client errors (4xx) are not retryable
                error_text = await http_response.text()
                raise Exception(f"API request failed (status {http_response.status}): {error_text}")

            response_bytes = await http_response.read()
            response = ExportSpansResponse().parse(response_bytes)

            if not response.success:
                raise Exception(f'API export reported failure: "{response.message}"')

        logger.debug(f"Successfully exported {len(spans)} spans to remote endpoint")
        return ExportResult.success()

    @override
    async def shutdown(self) -> None:
        """Shutdown and cleanup."""
        logger.debug(
            f"ApiSpanAdapter shutting down. "
            f"Exported: {self._spans_exported}, Failed: {self._spans_failed}, "
            f"Bytes sent: {self._bytes_sent}, Compressed: {self._bytes_compressed}"
        )

    def _transform_span_to_protobuf(self, clean_span: CleanSpanData) -> Any:
        """Transform CleanSpanData to protobuf Span format."""
        from tusk.drift.core.v1 import Span

        input_struct = _dict_to_struct(clean_span.input_value or {})
        output_struct = _dict_to_struct(clean_span.output_value or {})

        timestamp = datetime.fromtimestamp(
            clean_span.timestamp.seconds + clean_span.timestamp.nanos / 1_000_000_000,
            tz=UTC,
        )

        duration = timedelta(
            seconds=clean_span.duration.seconds,
            microseconds=clean_span.duration.nanos // 1000,
        )

        metadata_struct = _dict_to_struct({})
        if clean_span.metadata is not None:
            metadata_dict = clean_span.metadata if isinstance(clean_span.metadata, dict) else {}
            metadata_struct = _dict_to_struct(metadata_dict)

        from tusk.drift.core.v1 import PackageType as ProtoPackageType

        from ...types import PackageType as SDKPackageType

        if clean_span.package_type and hasattr(clean_span.package_type, "value"):
            package_type_value = ProtoPackageType(clean_span.package_type.value)
        else:
            package_type_value = ProtoPackageType(SDKPackageType.UNSPECIFIED.value)

        from tusk.drift.core.v1 import SpanStatus as ProtoSpanStatus

        kind_value = clean_span.kind.value if hasattr(clean_span.kind, "value") else clean_span.kind

        status_code_value = (
            clean_span.status.code.value if hasattr(clean_span.status.code, "value") else clean_span.status.code
        )
        proto_status = ProtoSpanStatus(code=status_code_value, message=clean_span.status.message or "")

        def convert_json_schema(sdk_schema: Any) -> Any:
            """Convert SDK JsonSchema to protobuf JsonSchema."""
            if sdk_schema is None:
                return None

            from tusk.drift.core.v1 import JsonSchema as ProtoJsonSchema

            if isinstance(sdk_schema, ProtoJsonSchema):
                return sdk_schema

            from ...json_schema_helper import JsonSchema as SDKJsonSchema

            if not isinstance(sdk_schema, SDKJsonSchema):
                return None

            proto_properties = {}
            if sdk_schema.properties:
                for key, value in sdk_schema.properties.items():
                    converted = convert_json_schema(value)
                    if converted is not None:
                        proto_properties[key] = converted

            proto_items = convert_json_schema(sdk_schema.items) if sdk_schema.items else None

            type_value = sdk_schema.type.value if hasattr(sdk_schema.type, "value") else sdk_schema.type
            encoding_value = (
                sdk_schema.encoding.value if sdk_schema.encoding and hasattr(sdk_schema.encoding, "value") else None
            )
            decoded_type_value = (
                sdk_schema.decoded_type.value
                if sdk_schema.decoded_type and hasattr(sdk_schema.decoded_type, "value")
                else None
            )

            return ProtoJsonSchema(
                type=type_value,
                properties=proto_properties,
                items=proto_items,
                encoding=encoding_value,  # type: ignore[arg-type]
                decoded_type=decoded_type_value,  # type: ignore[arg-type]
                match_importance=sdk_schema.match_importance,
            )

        proto_input_schema = convert_json_schema(clean_span.input_schema)
        proto_output_schema = convert_json_schema(clean_span.output_schema)

        return Span(
            trace_id=clean_span.trace_id,
            span_id=clean_span.span_id,
            parent_span_id=clean_span.parent_span_id,
            name=clean_span.name,
            package_name=clean_span.package_name,
            instrumentation_name=clean_span.instrumentation_name,
            submodule_name=clean_span.submodule_name,
            package_type=package_type_value,
            input_value=input_struct,
            output_value=output_struct,
            input_schema=proto_input_schema,
            output_schema=proto_output_schema,
            input_schema_hash=clean_span.input_schema_hash or "",
            output_schema_hash=clean_span.output_schema_hash or "",
            input_value_hash=clean_span.input_value_hash or "",
            output_value_hash=clean_span.output_value_hash or "",
            kind=kind_value,
            status=proto_status,
            is_pre_app_start=clean_span.is_pre_app_start,
            timestamp=timestamp,
            duration=duration,
            is_root_span=clean_span.is_root_span,
            metadata=metadata_struct,  # type: ignore[arg-type]
        )


def _dict_to_struct(data: dict[str, Any]) -> Struct:
    """Convert a Python dict to protobuf Struct."""
    from betterproto.lib.google.protobuf import ListValue, Struct, Value

    def value_to_proto(val: Any) -> Value:
        """Convert a Python value to protobuf Value."""
        if val is None:
            # betterproto 2.0.0b7 uses integer 0 for null value (NullValue.NULL_VALUE doesn't exist)
            return Value(null_value=0)  # type: ignore[arg-type]
        elif isinstance(val, bool):
            return Value(bool_value=val)
        elif isinstance(val, (int, float)):
            return Value(number_value=float(val))
        elif isinstance(val, str):
            return Value(string_value=val)
        elif isinstance(val, dict):
            return Value(struct_value=_dict_to_struct(val))
        elif isinstance(val, (list, tuple)):
            list_vals = [value_to_proto(item) for item in val]
            return Value(list_value=ListValue(values=list_vals))
        else:
            return Value(string_value=str(val))

    fields = {key: value_to_proto(value) for key, value in data.items()}
    return Struct(fields=fields)


def create_api_adapter(
    api_key: str,
    observable_service_id: str,
    environment: str = "development",
    sdk_version: str = "0.1.0",
    sdk_instance_id: str | None = None,
    tusk_backend_base_url: str = "https://api.usetusk.ai",
    *,
    enable_compression: bool = False,
    max_retry_attempts: int = 3,
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
        enable_compression: Whether to enable gzip compression for large payloads (disabled by default)
        max_retry_attempts: Maximum number of retry attempts for failed exports

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
        enable_compression=enable_compression,
        max_retry_attempts=max_retry_attempts,
    )

    return ApiSpanAdapter(config)
