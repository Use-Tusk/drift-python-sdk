from __future__ import annotations

import json
import logging
import time
from types import ModuleType
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status, set_span_in_context
from opentelemetry.trace import StatusCode as OTelStatusCode

from ...core.communication.types import MockRequestInput
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper
from ...core.tracing import TdSpanAttributes
from ...core.types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    TuskDriftMode,
    replay_trace_id_context,
)
from ..base import InstrumentationBase

logger = logging.getLogger(__name__)

_instance: RedisInstrumentation | None = None


class RedisInstrumentation(InstrumentationBase):
    """Instrumentation for redis Python client library."""

    def __init__(self, enabled: bool = True) -> None:
        global _instance
        super().__init__(
            name="RedisInstrumentation",
            module_name="redis",
            supported_versions=">=4.0.0",
            enabled=enabled,
        )
        self._original_execute_command = None
        self._original_pipeline_execute = None
        _instance = self

    def patch(self, module: ModuleType) -> None:
        """Patch the redis module."""
        if not hasattr(module, "Redis"):
            logger.warning("redis.Redis not found, skipping instrumentation")
            return

        # Patch sync Redis client
        redis_class = module.Redis
        if hasattr(redis_class, "execute_command"):
            # Store original method
            original_method = redis_class.execute_command
            self._original_execute_command = original_method
            instrumentation = self

            def patched_execute_command(redis_self, *args, **kwargs):
                """Patched execute_command method."""
                sdk = TuskDrift.get_instance()

                if sdk.mode == TuskDriftMode.DISABLED:
                    return original_method(redis_self, *args, **kwargs)

                return instrumentation._traced_execute_command(
                    redis_self,
                    original_method,
                    sdk,
                    args,
                    kwargs,
                )

            redis_class.execute_command = patched_execute_command
            logger.debug("redis.Redis.execute_command instrumented")

        # Patch Pipeline.execute
        try:
            from redis.client import Pipeline

            if hasattr(Pipeline, "execute"):
                original_pipeline_execute = Pipeline.execute
                self._original_pipeline_execute = original_pipeline_execute
                instrumentation = self

                def patched_pipeline_execute(pipeline_self, *args, **kwargs):
                    """Patched Pipeline.execute method."""
                    sdk = TuskDrift.get_instance()

                    if sdk.mode == TuskDriftMode.DISABLED:
                        return original_pipeline_execute(pipeline_self, *args, **kwargs)

                    return instrumentation._traced_pipeline_execute(
                        pipeline_self,
                        original_pipeline_execute,
                        sdk,
                        args,
                        kwargs,
                    )

                Pipeline.execute = patched_pipeline_execute
                logger.debug("redis.client.Pipeline.execute instrumented")
        except ImportError:
            logger.debug("redis.client.Pipeline not available")

        # Patch async Redis client if available
        try:
            import redis.asyncio

            async_redis_class = redis.asyncio.Redis
            if hasattr(async_redis_class, "execute_command"):
                original_async_execute = async_redis_class.execute_command
                instrumentation = self

                async def patched_async_execute_command(redis_self, *args, **kwargs):
                    """Patched async execute_command method."""
                    sdk = TuskDrift.get_instance()

                    if sdk.mode == TuskDriftMode.DISABLED:
                        return await original_async_execute(redis_self, *args, **kwargs)

                    return await instrumentation._traced_async_execute_command(
                        redis_self,
                        original_async_execute,
                        sdk,
                        args,
                        kwargs,
                    )

                async_redis_class.execute_command = patched_async_execute_command
                logger.debug("redis.asyncio.Redis.execute_command instrumented")
        except ImportError:
            logger.debug("redis.asyncio not available")

    def _traced_execute_command(
        self, redis_client: Any, original_execute: Any, sdk: TuskDrift, args: tuple, kwargs: dict
    ) -> Any:
        """Traced Redis execute_command method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_execute(redis_client, *args, **kwargs)

        command_name = args[0] if args else "UNKNOWN"
        command_str = self._format_command(args)

        # Get tracer and start span
        tracer = sdk.get_tracer()
        span_name = f"redis.{command_name}"

        # Start OpenTelemetry span (automatically inherits parent context)
        span = tracer.start_span(
            name=span_name,
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: span_name,
                TdSpanAttributes.PACKAGE_NAME: "redis",
                TdSpanAttributes.INSTRUMENTATION_NAME: "RedisInstrumentation",
                TdSpanAttributes.SUBMODULE_NAME: str(command_name),
                TdSpanAttributes.PACKAGE_TYPE: PackageType.REDIS.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        # Make span active
        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        try:
            # Get span IDs for mock requests
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")

            # Get parent span ID from parent context
            parent_span = trace.get_current_span(ctx)
            parent_span_id = None
            if parent_span and parent_span.is_recording():
                parent_ctx = parent_span.get_span_context()
                parent_span_id = format(parent_ctx.span_id, "016x")

            if sdk.mode == TuskDriftMode.REPLAY:
                # Handle background requests (app ready + no parent span)
                if sdk.app_ready and not parent_span_id:
                    return self._get_default_response(command_name)

                mock_result = self._try_get_mock(sdk, command_str, args, trace_id, span_id, parent_span_id)

                if mock_result is None:
                    is_pre_app_start = not sdk.app_ready
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for Redis command. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}command was not recorded during the trace capture. "
                        f"Command: {command_str}"
                    )

                return self._deserialize_response(mock_result)

            # RECORD mode
            error = None
            result = None

            try:
                result = original_execute(redis_client, *args, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_command_span(
                        span,
                        command_str,
                        args,
                        result if error is None else None,
                        error,
                    )
        finally:
            otel_context.detach(token)
            span.end()

    async def _traced_async_execute_command(
        self, redis_client: Any, original_execute: Any, sdk: TuskDrift, args: tuple, kwargs: dict
    ) -> Any:
        """Traced async Redis execute_command method."""
        # For REPLAY mode, use sync mocking
        if sdk.mode == TuskDriftMode.REPLAY:
            return self._traced_execute_command(redis_client, lambda *a, **kw: None, sdk, args, kwargs)

        # For RECORD mode, actually execute async
        if sdk.mode == TuskDriftMode.DISABLED:
            return await original_execute(redis_client, *args, **kwargs)

        command_name = args[0] if args else "UNKNOWN"
        command_str = self._format_command(args)

        # Get tracer and start span
        tracer = sdk.get_tracer()
        span_name = f"redis.{command_name}"

        # Start OpenTelemetry span (automatically inherits parent context)
        span = tracer.start_span(
            name=span_name,
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: span_name,
                TdSpanAttributes.PACKAGE_NAME: "redis",
                TdSpanAttributes.INSTRUMENTATION_NAME: "RedisInstrumentation",
                TdSpanAttributes.SUBMODULE_NAME: str(command_name),
                TdSpanAttributes.PACKAGE_TYPE: PackageType.REDIS.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        # Make span active
        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        try:
            # RECORD mode
            error = None
            result = None

            try:
                result = await original_execute(redis_client, *args, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_command_span(
                        span,
                        command_str,
                        args,
                        result if error is None else None,
                        error,
                    )
        finally:
            otel_context.detach(token)
            span.end()

    def _traced_pipeline_execute(
        self, pipeline: Any, original_execute: Any, sdk: TuskDrift, args: tuple, kwargs: dict
    ) -> Any:
        """Traced Pipeline.execute method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_execute(pipeline, *args, **kwargs)

        # Get commands from pipeline
        command_stack = self._get_pipeline_commands(pipeline)
        command_str = self._format_pipeline_commands(command_stack)

        # Get tracer and start span
        tracer = sdk.get_tracer()
        span_name = "redis.pipeline"

        # Start OpenTelemetry span (automatically inherits parent context)
        span = tracer.start_span(
            name=span_name,
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: span_name,
                TdSpanAttributes.PACKAGE_NAME: "redis",
                TdSpanAttributes.INSTRUMENTATION_NAME: "RedisInstrumentation",
                TdSpanAttributes.SUBMODULE_NAME: "pipeline",
                TdSpanAttributes.PACKAGE_TYPE: PackageType.REDIS.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        # Make span active
        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        try:
            # Get span IDs for mock requests
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")

            # Get parent span ID from parent context
            parent_span = trace.get_current_span(ctx)
            parent_span_id = None
            if parent_span and parent_span.is_recording():
                parent_ctx = parent_span.get_span_context()
                parent_span_id = format(parent_ctx.span_id, "016x")

            if sdk.mode == TuskDriftMode.REPLAY:
                # Handle background requests
                if sdk.app_ready and not parent_span_id:
                    return []

                mock_result = self._try_get_mock(sdk, command_str, command_stack, trace_id, span_id, parent_span_id)

                if mock_result is None:
                    is_pre_app_start = not sdk.app_ready
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for Redis pipeline. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}pipeline was not recorded during the trace capture. "
                        f"Commands: {command_str}"
                    )

                return self._deserialize_response(mock_result)

            # RECORD mode
            error = None
            result = None

            try:
                result = original_execute(pipeline, *args, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_pipeline_span(
                        span,
                        command_str,
                        command_stack,
                        result if error is None else None,
                        error,
                    )
        finally:
            otel_context.detach(token)
            span.end()

    def _format_command(self, args: tuple) -> str:
        """Format Redis command as string."""
        if not args:
            return ""

        # Format: "COMMAND arg1 arg2 ..."
        # Sanitize sensitive values
        parts = []
        for i, arg in enumerate(args):
            if i == 0:
                # Command name
                parts.append(str(arg).upper())
            else:
                # Mask argument values
                parts.append("?")

        return " ".join(parts)

    def _get_pipeline_commands(self, pipeline: Any) -> list:
        """Extract commands from pipeline."""
        try:
            if hasattr(pipeline, "command_stack"):
                return pipeline.command_stack
            elif hasattr(pipeline, "_command_stack"):
                return pipeline._command_stack
        except AttributeError:
            pass
        return []

    def _format_pipeline_commands(self, command_stack: list) -> str:
        """Format pipeline commands as string."""
        if not command_stack:
            return "PIPELINE"

        commands = []
        for cmd in command_stack:
            if hasattr(cmd, "args"):
                cmd_args = cmd.args
            elif isinstance(cmd, (tuple, list)) and len(cmd) > 0:
                cmd_args = cmd[0] if isinstance(cmd[0], (tuple, list)) else cmd
            else:
                continue

            if cmd_args:
                commands.append(str(cmd_args[0]).upper())

        return "PIPELINE: " + " ".join(commands)

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        command: str,
        args: Any,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
    ) -> dict[str, Any] | None:
        """Try to get a mocked response from CLI."""
        try:
            # Build input value
            input_value = {
                "command": command.strip(),
            }
            if args is not None:
                input_value["arguments"] = self._serialize_args(args)

            # Generate schema and hashes for CLI matching
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})

            # Create mock span for matching
            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            mock_span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id or "",
                name="redis.command",
                package_name="redis",
                package_type=PackageType.REDIS,
                instrumentation_name="RedisInstrumentation",
                submodule_name="command",
                input_value=input_value,
                output_value=None,
                input_schema=None,  # type: ignore
                output_schema=None,  # type: ignore
                input_schema_hash=input_result.decoded_schema_hash,
                output_schema_hash="",
                input_value_hash=input_result.decoded_value_hash,
                output_value_hash="",
                kind=SpanKind.CLIENT,
                status=SpanStatus(code=StatusCode.OK, message=""),
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=0, nanos=0),
                is_root_span=False,
                is_pre_app_start=not sdk.app_ready,
            )

            # Request mock from CLI
            replay_trace_id = replay_trace_id_context.get()

            mock_request = MockRequestInput(
                test_id=replay_trace_id or "",
                outbound_span=mock_span,
            )

            logger.debug(f"Requesting mock from CLI for command: {command[:50]}...")
            mock_response_output = sdk.request_mock_sync(mock_request)
            logger.debug(f"CLI returned: found={mock_response_output.found}")

            if not mock_response_output.found:
                logger.debug(f"No mock found for Redis command: {command}")
                return None

            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for Redis command: {e}")
            return None

    def _finalize_command_span(
        self,
        span: trace.Span,
        command: str,
        args: tuple,
        result: Any,
        error: Exception | None,
    ) -> None:
        """Finalize span with command data."""
        try:
            # Build input value
            input_value = {
                "command": command.strip(),
            }
            if args is not None:
                input_value["arguments"] = self._serialize_args(args)

            # Build output value
            output_value = {}

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                span.set_status(Status(OTelStatusCode.ERROR, str(error)))
            else:
                output_value = {
                    "result": self._serialize_response(result),
                }
                span.set_status(Status(OTelStatusCode.OK))

            # Generate schemas and hashes
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, {})

            # Set span attributes
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(input_value))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(output_value))
            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA, json.dumps(input_result.schema.to_primitive()))
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA, json.dumps(output_result.schema.to_primitive()))
            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_HASH, input_result.decoded_schema_hash)
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_HASH, output_result.decoded_schema_hash)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE_HASH, input_result.decoded_value_hash)
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE_HASH, output_result.decoded_value_hash)

        except Exception as e:
            logger.error(f"Error finalizing Redis command span: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))

    def _finalize_pipeline_span(
        self,
        span: trace.Span,
        command_str: str,
        command_stack: list,
        result: Any,
        error: Exception | None,
    ) -> None:
        """Finalize span with pipeline data."""
        try:
            # Build input value
            serialized_commands = [
                self._serialize_args(cmd.args if hasattr(cmd, "args") else cmd[0]) for cmd in command_stack
            ]
            input_value: dict[str, Any] = {
                "command": command_str,
                "commands": serialized_commands,
            }

            # Build output value
            output_value = {}

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                span.set_status(Status(OTelStatusCode.ERROR, str(error)))
            else:
                output_value = {
                    "results": [self._serialize_response(r) for r in result] if result else [],
                }
                span.set_status(Status(OTelStatusCode.OK))

            # Generate schemas and hashes
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, {})

            # Set span attributes
            span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(input_value))
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(output_value))
            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA, json.dumps(input_result.schema.to_primitive()))
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA, json.dumps(output_result.schema.to_primitive()))
            span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_HASH, input_result.decoded_schema_hash)
            span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_HASH, output_result.decoded_schema_hash)
            span.set_attribute(TdSpanAttributes.INPUT_VALUE_HASH, input_result.decoded_value_hash)
            span.set_attribute(TdSpanAttributes.OUTPUT_VALUE_HASH, output_result.decoded_value_hash)

        except Exception as e:
            logger.error(f"Error finalizing Redis pipeline span: {e}")
            span.set_status(Status(OTelStatusCode.ERROR, str(e)))

    def _serialize_args(self, args: Any) -> list:
        """Serialize command arguments."""
        if isinstance(args, (tuple, list)):
            return [self._serialize_value(arg) for arg in args]
        return [self._serialize_value(args)]

    def _serialize_value(self, value: Any) -> Any:
        """Serialize a single value for JSON."""
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                return value.hex()
        elif isinstance(value, (str, int, float, bool, type(None))):
            return value
        elif isinstance(value, (list, tuple)):
            return [self._serialize_value(v) for v in value]
        elif isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        elif isinstance(value, set):
            return [self._serialize_value(v) for v in value]
        else:
            return str(value)

    def _serialize_response(self, response: Any) -> Any:
        """Serialize Redis response for recording."""
        return self._serialize_value(response)

    def _deserialize_response(self, mock_data: dict[str, Any]) -> Any:
        """Deserialize mocked response data from CLI.

        The CLI wraps the response in: {"response": {"Body": {...output_value...}}}
        For Redis, output_value is: {"result": value} or {"results": [values]}
        """
        logger.debug(f"Deserializing mock_data keys: {list(mock_data.keys()) if mock_data else None}")

        # Navigate through CLI response structure
        if "response" in mock_data:
            response = mock_data["response"]
            if isinstance(response, dict) and "Body" in response:
                body = response["Body"]
                logger.debug(f"Found Body in response: {body}")
                # Body contains the output_value: {"result": ...} or {"results": [...]}
                if isinstance(body, dict):
                    if "result" in body:
                        return body["result"]
                    elif "results" in body:
                        return body["results"]

        # Fallback: check top level (for backwards compatibility)
        if "result" in mock_data:
            return mock_data["result"]
        elif "results" in mock_data:
            return mock_data["results"]

        logger.warning(f"Could not deserialize mock_data structure: {mock_data}")
        return None

    def _get_default_response(self, command_name: str) -> Any:
        """Get default response for background requests."""
        command_upper = str(command_name).upper()

        # Return appropriate default based on command type
        if command_upper in ("GET", "HGET", "LPOP", "RPOP"):
            return None
        elif command_upper in ("SET", "HSET", "LPUSH", "RPUSH", "SADD", "ZADD", "DEL", "EXPIRE"):
            return 1
        elif command_upper in ("MGET", "HGETALL", "LRANGE", "SMEMBERS", "ZRANGE", "KEYS"):
            return []
        elif command_upper in ("EXISTS", "SISMEMBER"):
            return 0
        elif command_upper == "TTL":
            return -1
        elif command_upper == "INCR":
            return 1
        elif command_upper == "DECR":
            return -1
        elif command_upper == "PING":
            return "PONG"
        else:
            return None
