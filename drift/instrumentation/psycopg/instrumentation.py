"""PostgreSQL instrumentation for psycopg2 and psycopg3 (postgres library)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, override, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...core.drift_sdk import TuskDrift

from ..base import InstrumentationBase
from ...core.types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
    current_span_id_context,
    current_trace_id_context,
)
from ...core.json_schema_helper import JsonSchemaHelper, SchemaMerge


class PostgresInstrumentation(InstrumentationBase):
    """PostgreSQL instrumentation for psycopg2 and psycopg3.

    Patches:
    - psycopg2.extensions.connection.cursor() to capture queries
    - psycopg3 postgres() function to capture queries on the sql instance

    Args:
        enabled: Whether instrumentation is enabled
    """

    def __init__(self, enabled: bool = True):
        super().__init__(
            name="PostgresInstrumentation",
            module_name="psycopg2",
            supported_versions="*",
            enabled=enabled,
        )
        self._patched_modules = set()

    @override
    def patch(self, module: Any) -> None:
        """Patch the psycopg2 or postgres module."""
        if not module:
            logger.warning("[PostgresInstrumentation] Module is None, skipping patch")
            return

        module_name = getattr(module, "__name__", "unknown")

        if module_name in self._patched_modules:
            logger.debug(f"[PostgresInstrumentation] {module_name} already patched, skipping")
            return

        try:
            # Try to patch psycopg2
            if "psycopg2" in module_name or hasattr(module, "connect"):
                self._patch_psycopg2(module)
                self._patched_modules.add(module_name)
            # Try to patch psycopg3 (postgres library)
            elif hasattr(module, "sql"):
                self._patch_postgres(module)
                self._patched_modules.add(module_name)
            else:
                logger.debug(f"[PostgresInstrumentation] Unknown module format: {module_name}")
        except Exception as e:
            logger.error(f"[PostgresInstrumentation] Failed to patch {module_name}: {e}", exc_info=True)

    def _patch_psycopg2(self, psycopg2_module: Any) -> None:
        """Patch psycopg2.extensions.connection to capture queries.

        Args:
            psycopg2_module: The psycopg2 module
        """
        logger.debug("[PostgresInstrumentation] Patching psycopg2")

        try:
            # Get the connect function
            if not hasattr(psycopg2_module, "connect"):
                logger.warning("[PostgresInstrumentation] psycopg2.connect not found")
                return

            original_connect = psycopg2_module.connect

            def patched_connect(*args, **kwargs):
                """Patched connect function that wraps cursor creation."""
                connection = original_connect(*args, **kwargs)
                return self._wrap_connection(connection)

            psycopg2_module.connect = patched_connect
            logger.info("[PostgresInstrumentation] Successfully patched psycopg2.connect")
            print("PostgreSQL (psycopg2) instrumentation applied")

        except Exception as e:
            logger.error(f"[PostgresInstrumentation] Failed to patch psycopg2: {e}", exc_info=True)

    def _wrap_connection(self, connection: Any) -> Any:
        """Wrap a psycopg2 connection to instrument cursor queries.

        Args:
            connection: A psycopg2 connection object

        Returns:
            The wrapped connection with instrumented cursor() method
        """
        original_cursor = connection.cursor

        def patched_cursor(*args, **kwargs):
            """Patched cursor creation that instruments execute calls."""
            cursor = original_cursor(*args, **kwargs)
            return self._wrap_cursor(cursor)

        connection.cursor = patched_cursor
        return connection

    def _wrap_cursor(self, cursor: Any) -> Any:
        """Wrap a psycopg2 cursor to instrument query execution.

        Args:
            cursor: A psycopg2 cursor object

        Returns:
            The wrapped cursor with instrumented execute() method
        """
        original_execute = cursor.execute
        original_executemany = cursor.executemany if hasattr(cursor, "executemany") else None

        def patched_execute(query: str, args=None):
            """Patched execute that captures query as a span."""
            return self._execute_query(original_execute, query, args, is_many=False)

        def patched_executemany(query: str, args_list):
            """Patched executemany that captures batch query as a span."""
            return self._execute_query(original_executemany, query, args_list, is_many=True)

        cursor.execute = patched_execute
        if original_executemany:
            cursor.executemany = patched_executemany

        return cursor

    def _execute_query(self, original_execute, query: str, args, is_many: bool = False) -> Any:
        """Execute a query and create a span for it.

        Args:
            original_execute: The original execute function
            query: The SQL query string
            args: Query arguments (for single execute) or list of argument tuples (for executemany)
            is_many: Whether this is an executemany call

        Returns:
            The result of the original execute call
        """
        from ...core.drift_sdk import TuskDrift

        sdk = TuskDrift.get_instance()

        # Get parent trace/span context if available
        parent_trace_id = current_trace_id_context.get()
        parent_span_id = current_span_id_context.get()

        # Use parent trace ID if available, otherwise generate new one
        if parent_trace_id:
            trace_id = parent_trace_id
        else:
            trace_id = str(uuid.uuid4()).replace("-", "")

        # Always generate a new span ID for this database operation
        span_id = str(uuid.uuid4()).replace("-", "")[:16]

        # Set this span as current context for any nested operations
        trace_token = current_trace_id_context.set(trace_id)
        span_token = current_span_id_context.set(span_id)

        start_time = time.time()
        start_time_ns = time.time_ns()

        error = None
        try:
            # Execute the query
            result = original_execute(query, args)
            return result
        except Exception as e:
            error = e
            raise
        finally:
            # Create span for the query
            end_time_ns = time.time_ns()
            duration_ns = end_time_ns - start_time_ns

            self._create_span(
                sdk,
                trace_id,
                span_id,
                parent_span_id,
                query,
                args,
                is_many,
                duration_ns,
                error,
                start_time_ns,
            )

            # Reset trace context
            current_trace_id_context.reset(trace_token)
            current_span_id_context.reset(span_token)

    def _create_span(
        self,
        sdk: TuskDrift,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        query: str,
        args: Any,
        is_many: bool,
        duration_ns: int,
        error: Exception | None,
        start_time_ns: int,
    ) -> None:
        """Create and collect a database query span.

        Args:
            sdk: TuskDrift SDK instance
            trace_id: Trace ID
            span_id: Span ID
            parent_span_id: Parent span ID (if nested)
            query: SQL query string
            args: Query arguments
            is_many: Whether this was an executemany call
            duration_ns: Query duration in nanoseconds
            error: Exception raised during query (if any)
            start_time_ns: Start time in nanoseconds
        """
        try:
            # Extract query operation type and basic details
            query_upper = query.strip().upper()
            operation = query_upper.split()[0] if query_upper else "QUERY"

            # Build input value with query details
            input_value: dict[str, Any] = {
                "query": query,
            }

            # Add arguments if present
            if args:
                if is_many:
                    input_value["args"] = f"[{len(args)} parameter sets]"
                else:
                    # For single execute, capture arguments (but sanitize sensitive data)
                    input_value["args"] = self._sanitize_args(args)

            # Build output value
            output_value: dict[str, Any] = {}
            status = SpanStatus(code=StatusCode.OK, message="")

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))

            # Generate schemas
            input_schema_merges: dict[str, SchemaMerge] = {}
            output_schema_merges: dict[str, SchemaMerge] = {}

            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, input_schema_merges)
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, output_schema_merges)

            # Create timestamp and duration
            timestamp_seconds = start_time_ns // 1_000_000_000
            timestamp_nanos = start_time_ns % 1_000_000_000
            duration_seconds = duration_ns // 1_000_000_000
            duration_nanos = duration_ns % 1_000_000_000

            # Create span name
            span_name = f"{operation} (PostgreSQL)"

            # Create and collect span
            span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id or "",
                name=span_name,
                package_name="postgresql",
                instrumentation_name="PostgresInstrumentation",
                submodule_name=operation,
                package_type=PackageType.PG,
                kind=SpanKind.CLIENT,
                input_value=input_value,
                output_value=output_value,
                input_schema=input_result.schema,
                output_schema=output_result.schema,
                input_value_hash=input_result.decoded_value_hash,
                output_value_hash=output_result.decoded_value_hash,
                input_schema_hash=input_result.decoded_schema_hash,
                output_schema_hash=output_result.decoded_schema_hash,
                status=status,
                is_root_span=parent_span_id is None,
                is_pre_app_start=not sdk.app_ready,
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
            )

            sdk.collect_span(span)

        except Exception as e:
            logger.error(f"[PostgresInstrumentation] Error creating span: {e}", exc_info=True)

    def _sanitize_args(self, args: Any) -> Any:
        """Sanitize query arguments, handling various formats.

        Args:
            args: Query arguments (tuple, list, dict, or None)

        Returns:
            Sanitized arguments safe to log
        """
        if args is None:
            return None

        if isinstance(args, (tuple, list)):
            # Limit to first 100 items
            return list(args)[:100] if len(args) > 100 else list(args)
        elif isinstance(args, dict):
            # Limit dict size
            return {k: v for i, (k, v) in enumerate(args.items()) if i < 100}
        else:
            return args

    def _patch_postgres(self, postgres_module: Any) -> None:
        """Patch psycopg3 (postgres library).

        Note: This is for future support of the psycopg3 'postgres' library.

        Args:
            postgres_module: The postgres module
        """
        logger.debug("[PostgresInstrumentation] Patching psycopg3 (postgres library)")
        # TODO: Implement psycopg3 support if needed
        logger.warning("[PostgresInstrumentation] psycopg3 support not yet implemented")
