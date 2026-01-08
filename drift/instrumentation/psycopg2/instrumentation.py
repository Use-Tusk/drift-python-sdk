"""Instrumentation for psycopg2 PostgreSQL client library."""

from __future__ import annotations

import json
import logging
import time
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg2.extensions import cursor as BaseCursorType
    from psycopg2.sql import Composable

    QueryType = str | bytes | Composable

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
from ..utils.psycopg_utils import deserialize_db_value

logger = logging.getLogger(__name__)

# Module-level variable to store the instrumentation instance
# This allows Django instrumentation to access it
_instance: Psycopg2Instrumentation | None = None


class MockConnection:
    """Mock database connection for REPLAY mode when postgres is not available.

    Provides minimal interface for Django/Flask to work without a real database.
    All queries are mocked at the cursor.execute() level.
    """

    def __init__(self, sdk: TuskDrift, instrumentation: Psycopg2Instrumentation, cursor_factory):
        self.sdk = sdk
        self.instrumentation = instrumentation
        self.cursor_factory = cursor_factory
        self.closed = False
        self.autocommit = False

        # Django requires these for connection initialization
        import psycopg2.extensions

        self.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED
        self.encoding = "UTF8"
        self.server_version = 150000  # PostgreSQL 15.0
        self.protocol_version = 3

        # Mock info object for psycopg2
        class MockInfo:
            server_version = 150000

            def parameter_status(self, parameter):
                """Mock parameter_status for Django PostgreSQL backend."""
                # Common parameters Django checks
                if parameter == "TimeZone":
                    return "UTC"
                elif parameter == "server_encoding":
                    return "UTF8"
                elif parameter == "server_version":
                    return "15.0"
                return None

        self.info = MockInfo()

        logger.debug("[MOCK_CONNECTION] Created mock connection for REPLAY mode")

    def cursor(self, name=None, cursor_factory=None):
        """Create a cursor using the instrumented cursor factory."""
        # For mock connections, we create a MockCursor directly
        cursor = MockCursor(self)

        # Wrap execute/executemany for mock cursor
        instrumentation = self.instrumentation
        sdk = self.sdk

        def mock_execute(query, vars=None):
            # For mock cursor, original_execute is just a no-op
            def noop_execute(q, v):
                return None

            return instrumentation._traced_execute(cursor, noop_execute, sdk, query, vars)

        def mock_executemany(query, vars_list):
            # For mock cursor, original_executemany is just a no-op
            def noop_executemany(q, vl):
                return None

            return instrumentation._traced_executemany(cursor, noop_executemany, sdk, query, vars_list)

        # Monkey-patch mock functions onto cursor
        cursor.execute = mock_execute  # type: ignore[method-assign]
        cursor.executemany = mock_executemany  # type: ignore[method-assign]

        logger.debug("[MOCK_CONNECTION] Created cursor")
        return cursor

    def commit(self):
        """Mock commit - no-op in REPLAY mode."""
        logger.debug("[MOCK_CONNECTION] commit() called (no-op)")
        pass

    def rollback(self):
        """Mock rollback - no-op in REPLAY mode."""
        logger.debug("[MOCK_CONNECTION] rollback() called (no-op)")
        pass

    def close(self):
        """Mock close - no-op in REPLAY mode."""
        logger.debug("[MOCK_CONNECTION] close() called (no-op)")
        self.closed = True

    def set_session(self, **kwargs):
        """Mock set_session - no-op in REPLAY mode."""
        logger.debug(f"[MOCK_CONNECTION] set_session() called with {kwargs} (no-op)")
        pass

    def set_isolation_level(self, level):
        """Mock set_isolation_level - no-op in REPLAY mode."""
        logger.debug(f"[MOCK_CONNECTION] set_isolation_level({level}) called (no-op)")
        self.isolation_level = level

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False


class MockCursor:
    """Mock cursor for when we can't create a real cursor from base class.

    This is a fallback when the connection is completely mocked.
    """

    def __init__(self, connection):
        self.connection = connection
        self.rowcount = -1
        self.description = None
        self.arraysize = 1
        self._mock_rows = []
        self._mock_index = 0
        logger.debug("[MOCK_CURSOR] Created fallback mock cursor")

    def execute(self, query: Any, vars: Any = None) -> None:
        """Will be replaced by instrumentation."""
        query_str = _query_to_str(query) if not isinstance(query, str) else query
        logger.debug(f"[MOCK_CURSOR] execute() called: {query_str[:100]}")
        return None

    def executemany(self, query: Any, vars_list: Any) -> None:
        """Will be replaced by instrumentation."""
        query_str = _query_to_str(query) if not isinstance(query, str) else query
        logger.debug(f"[MOCK_CURSOR] executemany() called: {query_str[:100]}")
        return None

    def fetchone(self):
        return None

    def fetchmany(self, size=None):
        return []

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def _query_to_str(query: QueryType) -> str:
    """Convert a query (str, bytes, or Composable) to a string."""
    if isinstance(query, str):
        return query
    elif isinstance(query, bytes):
        return query.decode("utf-8", errors="replace")
    else:
        # Composable object - convert to SQL string
        # We need to use as_string() which requires a connection
        # As a fallback, just use str() representation
        return str(query)


class Psycopg2Instrumentation(InstrumentationBase):
    """Instrumentation for the psycopg2 PostgreSQL client library.

    Patches psycopg2 cursor methods to:
    - Intercept SQL queries in REPLAY mode and return mocked responses
    - Capture query/response data as CLIENT spans in RECORD mode

    This implementation uses psycopg2's cursor_factory feature to wrap cursors.
    In REPLAY mode, if postgres is not available, a mock connection is used.
    """

    def __init__(self, enabled: bool = True) -> None:
        global _instance
        super().__init__(
            name="Psycopg2Instrumentation",
            module_name="psycopg2",
            supported_versions="*",
            enabled=enabled,
        )
        self._original_connect = None
        _instance = self  # Store instance for Django instrumentation to access

    def patch(self, module: ModuleType) -> None:
        """Patch the psycopg2 module."""
        if not hasattr(module, "connect"):
            logger.warning("psycopg2.connect not found, skipping instrumentation")
            return

        # Store original connect function
        self._original_connect = module.connect

        # Capture self and original_connect in the closure
        instrumentation = self
        original_connect = self._original_connect

        # In REPLAY mode, patch psycopg2.extras functions to be no-ops
        # This allows Django to work without a real database connection
        from ...core.drift_sdk import TuskDrift

        sdk = TuskDrift.get_instance()
        if sdk.mode == TuskDriftMode.REPLAY:
            try:
                import psycopg2.extensions
                import psycopg2.extras

                # Patch register functions to be no-ops in REPLAY mode
                original_register_default_json = getattr(psycopg2.extras, "register_default_json", None)
                original_register_default_jsonb = getattr(psycopg2.extras, "register_default_jsonb", None)
                original_register_uuid = getattr(psycopg2.extras, "register_uuid", None)

                if original_register_default_json:
                    psycopg2.extras.register_default_json = lambda *args, **kwargs: None
                if original_register_default_jsonb:
                    psycopg2.extras.register_default_jsonb = lambda *args, **kwargs: None
                if original_register_uuid:
                    psycopg2.extras.register_uuid = lambda *args, **kwargs: None

                logger.info("[PSYCOPG2_REPLAY] Patched psycopg2.extras register functions to be no-ops")
            except Exception as e:
                logger.warning(f"[PSYCOPG2_REPLAY] Failed to patch psycopg2.extras: {e}")

        def patched_connect(*args, **kwargs):
            """Patched psycopg2.connect method."""
            sdk = TuskDrift.get_instance()
            logger.info("[PATCHED_CONNECT] psycopg2.connect() called")
            logger.info(f"[PATCHED_CONNECT]   mode: {sdk.mode}")
            logger.info(f"[PATCHED_CONNECT]   app_ready: {sdk.app_ready}")
            logger.debug(f"[PATCHED_CONNECT]   args: {args[:2] if args else 'none'}")

            # Pass through if SDK is disabled or original connect is missing
            if sdk.mode == TuskDriftMode.DISABLED or original_connect is None:
                if original_connect is None:
                    raise RuntimeError("Original psycopg2.connect not found")
                logger.debug("[PATCHED_CONNECT] SDK disabled, passing through")
                return original_connect(*args, **kwargs)

            # Use cursor_factory to wrap cursors
            # Save any user-provided cursor_factory
            user_cursor_factory = kwargs.pop("cursor_factory", None)

            # Create our instrumented cursor factory
            cursor_factory = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # In REPLAY mode, try to connect but fall back to mock connection if DB is unavailable
            if sdk.mode == TuskDriftMode.REPLAY:
                try:
                    kwargs["cursor_factory"] = cursor_factory
                    logger.debug("[PATCHED_CONNECT] REPLAY mode: Attempting real DB connection...")
                    connection = original_connect(*args, **kwargs)
                    logger.info("[PATCHED_CONNECT] REPLAY mode: Successfully connected to real database")
                    return connection
                except Exception as e:
                    logger.info(
                        f"[PATCHED_CONNECT] REPLAY mode: Database connection failed ({e}), using mock connection"
                    )
                    # Return mock connection that doesn't require a real database
                    return MockConnection(sdk, instrumentation, cursor_factory)

            # In RECORD mode, always require real connection
            kwargs["cursor_factory"] = cursor_factory
            logger.debug("[PATCHED_CONNECT] RECORD mode: Connecting to database...")
            connection = original_connect(*args, **kwargs)
            logger.info("[PATCHED_CONNECT] RECORD mode: Connected to database successfully")

            return connection

        # Apply patch
        module.connect = patched_connect  # type: ignore[attr-defined]
        logger.info(f"psycopg2.connect instrumented. module.connect is now: {getattr(module, 'connect', None)}")

        # Also verify it's actually patched
        import psycopg2

        if psycopg2.connect == patched_connect:
            logger.info("[VERIFY] psycopg2.connect successfully patched!")
        else:
            logger.error(
                f"[VERIFY] psycopg2.connect NOT patched! psycopg2.connect={psycopg2.connect}, patched_connect={patched_connect}"
            )

    def _create_cursor_factory(
        self, sdk: TuskDrift, base_factory: type[BaseCursorType] | None = None
    ) -> type[BaseCursorType]:
        """Create a cursor factory that wraps cursors with instrumentation.

        For real connections: Returns a cursor CLASS (not instance)
        For mock connections: Returns a factory function
        """
        instrumentation = self
        logger.debug(f"[CURSOR_FACTORY] Creating cursor factory, sdk.mode={sdk.mode}")

        # For real connections, psycopg2 expects a cursor CLASS, not a factory function
        from psycopg2.extensions import cursor as BaseCursor

        base: type[BaseCursorType] = base_factory or BaseCursor

        class InstrumentedCursor(base):
            def execute(self, query: QueryType, vars: Any = None) -> Any:
                logger.debug("[INSTRUMENTED_CURSOR] execute() called on instrumented cursor")
                return instrumentation._traced_execute(self, super().execute, sdk, query, vars)

            def executemany(self, query: QueryType, vars_list: Any) -> Any:
                logger.debug("[INSTRUMENTED_CURSOR] executemany() called on instrumented cursor")
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, vars_list)

        return InstrumentedCursor

    def _traced_execute(
        self,
        cursor: Any,
        original_execute: Any,
        sdk: TuskDrift,
        query: QueryType,
        params: Any = None,
    ) -> Any:
        """Traced cursor.execute method."""
        # Convert query to string for logging and storage
        query_str = _query_to_str(query)

        # Pass through if SDK is disabled
        if sdk.mode == TuskDriftMode.DISABLED:
            logger.debug("[PSYCOPG2] _traced_execute: SDK disabled, passing through")
            return original_execute(query, params)

        # Create OpenTelemetry span
        tracer = sdk.get_tracer()
        span = tracer.start_span(
            name="psycopg2.query",
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: "psycopg2.query",
                TdSpanAttributes.PACKAGE_NAME: "psycopg2",
                TdSpanAttributes.INSTRUMENTATION_NAME: "Psycopg2Instrumentation",
                TdSpanAttributes.SUBMODULE_NAME: "query",
                TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        # Make span active
        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        logger.info("[PSYCOPG2] _traced_execute START")
        logger.info(f"  SDK mode: {sdk.mode}")
        logger.info(f"  app_ready: {sdk.app_ready}")
        logger.info(f"  query: {query_str[:200] if len(query_str) > 200 else query_str}")

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

            # REPLAY mode: Mock ALL queries (including pre-app-start)
            if sdk.mode == TuskDriftMode.REPLAY:
                logger.info("[PSYCOPG2_REPLAY] execute() entering REPLAY mode")
                logger.info(f"[PSYCOPG2_REPLAY]   query: {query_str[:100]}")
                logger.info(f"[PSYCOPG2_REPLAY]   parent_span_id: {parent_span_id}")
                logger.info(f"[PSYCOPG2_REPLAY]   app_ready: {sdk.app_ready}")

                # Handle background requests: App is ready + no parent span
                # These are background jobs/health checks that run AFTER app startup
                # They were never recorded, so return empty result
                if sdk.app_ready and not parent_span_id:
                    logger.info("[PSYCOPG2_REPLAY] Background request (app ready, no parent) - returning empty result")
                    # Return empty cursor result
                    cursor.rowcount = 0
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return None

                # For all other queries (pre-app-start OR within a request trace), get mock
                replay_trace_id = replay_trace_id_context.get()
                is_pre_app_start = not sdk.app_ready
                logger.info(f"[PSYCOPG2_REPLAY] replay_trace_id={replay_trace_id}")
                logger.info(f"[PSYCOPG2_REPLAY] is_pre_app_start={is_pre_app_start}")
                logger.info("[PSYCOPG2_REPLAY] Requesting mock from CLI...")

                # Try to get mock for this query
                mock_result = self._try_get_mock(sdk, query, params, trace_id, span_id, parent_span_id)
                logger.info(f"[PSYCOPG2_REPLAY] Mock result received: {mock_result is not None}")

                if mock_result is None:
                    # For pre-app-start queries without a mock, return empty result
                    # This allows Django migrations and startup queries to proceed
                    # even if they weren't recorded during trace capture
                    if is_pre_app_start:
                        logger.warning(
                            "[PSYCOPG2_REPLAY] No mock found for pre-app-start query, returning empty result"
                        )
                        logger.warning(f"[PSYCOPG2_REPLAY]   query: {query_str[:100]}")
                        logger.warning("[PSYCOPG2_REPLAY]   This query was not recorded during trace capture")
                        # Return empty cursor result using the proper mock method
                        empty_mock = {"rowcount": 0, "rows": [], "description": None}
                        self._mock_execute_with_data(cursor, empty_mock)
                        return None

                    # For in-request queries, we MUST have a mock - this is an error
                    logger.error("[PSYCOPG2_REPLAY] ERROR: No mock found for in-request query")
                    logger.error(f"[PSYCOPG2_REPLAY]   query: {query_str[:100]}")
                    logger.error(f"[PSYCOPG2_REPLAY]   trace_id: {trace_id}")
                    logger.error(f"[PSYCOPG2_REPLAY]   span_id: {span_id}")
                    logger.error(f"[PSYCOPG2_REPLAY]   parent_span_id: {parent_span_id}")
                    logger.error(f"[PSYCOPG2_REPLAY]   replay_trace_id: {replay_trace_id}")
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg2 execute query. "
                        f"This query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )

                logger.info("[PSYCOPG2_REPLAY] Applying mock to cursor...")
                # Mock execute by setting cursor internal state
                self._mock_execute_with_data(cursor, mock_result)
                logger.info("[PSYCOPG2_REPLAY] Mock applied successfully, returning None")
                return None  # execute() returns None

            # RECORD mode: Execute real query and record span
            error = None

            logger.info("[PSYCOPG2_RECORD] execute() entering RECORD mode")
            logger.info(f"[PSYCOPG2_RECORD]   query: {query_str[:100]}")
            logger.info(f"[PSYCOPG2_RECORD]   app_ready: {sdk.app_ready}")
            logger.info(f"[PSYCOPG2_RECORD]   is_pre_app_start: {not sdk.app_ready}")

            try:
                logger.debug("[PSYCOPG2_RECORD] Executing query...")
                result = original_execute(query, params)
                logger.info("[PSYCOPG2_RECORD] Query executed successfully")
                return result
            except Exception as e:
                error = e
                logger.error(f"[PSYCOPG2_RECORD] Query failed with error: {e}")
                raise
            finally:
                # Always finalize span in RECORD mode (including pre-app-start queries)
                if sdk.mode == TuskDriftMode.RECORD:
                    logger.info("[PSYCOPG2_RECORD] Finalizing span for query")
                    self._finalize_query_span(
                        span,
                        cursor,
                        query,
                        params,
                        error,
                    )
                    logger.info("[PSYCOPG2_RECORD] Span finalized successfully")
        finally:
            otel_context.detach(token)
            span.end()

    def _traced_executemany(
        self,
        cursor: Any,
        original_executemany: Any,
        sdk: TuskDrift,
        query: QueryType,
        params_list: Any,
    ) -> Any:
        """Traced cursor.executemany method."""
        # Convert query to string for logging and storage
        query_str = _query_to_str(query)

        # Pass through if SDK is disabled
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_executemany(query, params_list)

        # Create OpenTelemetry span
        tracer = sdk.get_tracer()
        span = tracer.start_span(
            name="psycopg2.query",
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: "psycopg2.query",
                TdSpanAttributes.PACKAGE_NAME: "psycopg2",
                TdSpanAttributes.INSTRUMENTATION_NAME: "Psycopg2Instrumentation",
                TdSpanAttributes.SUBMODULE_NAME: "query",
                TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
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

            # For executemany, we'll treat each parameter set as a batch
            # REPLAY mode: Mock ALL queries (including pre-app-start)
            if sdk.mode == TuskDriftMode.REPLAY:
                # Handle background requests: App is ready + no parent span
                # These are background jobs/health checks that run AFTER app startup
                # They were never recorded, so return empty result
                if sdk.app_ready and not parent_span_id:
                    logger.debug("Background executemany request (app ready, no parent) - returning empty result")
                    # Return empty cursor result
                    cursor.rowcount = 0
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return None

                # For all other queries (pre-app-start OR within a request trace), get mock
                # Wrap in {"_batch": ...} to match the recording format
                is_pre_app_start = not sdk.app_ready
                mock_result = self._try_get_mock(
                    sdk,
                    query,
                    {"_batch": params_list},
                    trace_id,
                    span_id,
                    parent_span_id,
                )
                if mock_result is None:
                    # For pre-app-start queries without a mock, return empty result
                    if is_pre_app_start:
                        logger.warning(
                            "[PSYCOPG2_REPLAY] No mock found for pre-app-start executemany query, returning empty result"
                        )
                        logger.warning(f"[PSYCOPG2_REPLAY]   query: {query_str[:100]}")
                        # Return empty cursor result using the proper mock method
                        empty_mock = {"rowcount": 0, "rows": [], "description": None}
                        self._mock_execute_with_data(cursor, empty_mock)
                        return None

                    # For in-request queries, we MUST have a mock - this is an error
                    logger.error(f"[PSYCOPG2_REPLAY] No mock found for in-request executemany query: {query_str[:100]}")
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg2 executemany query. "
                        f"This query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )

                # Instead of modifying cursor, create a mock execute that sets internal state
                # We need to set the cursor's internal state that gets populated during execute()
                self._mock_execute_with_data(cursor, mock_result)
                return None  # executemany() returns None

            # RECORD mode: Execute real query and record span
            error = None

            try:
                result = original_executemany(query, params_list)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                # Always finalize span in RECORD mode (including pre-app-start queries)
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_query_span(
                        span,
                        cursor,
                        query,
                        {"_batch": list(params_list)},
                        error,
                    )
        finally:
            otel_context.detach(token)
            span.end()

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        query: QueryType,
        params: Any,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
    ) -> dict[str, Any] | None:
        """Try to get a mocked response from CLI.

        Returns:
            Mocked response data if found, None otherwise
        """
        try:
            # Build input value
            query_str = _query_to_str(query)
            input_value = {
                "query": query_str.strip(),
            }
            if params is not None:
                input_value["parameters"] = params

            # Generate schema and hashes for CLI matching
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})

            # Create mock span for matching
            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            # Create mock span for matching
            # NOTE: Schemas must be None to avoid betterproto map serialization issues
            # The CLI only needs the hashes for matching anyway, not the full schemas
            mock_span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id or "",
                name="psycopg2.query",
                package_name="psycopg2",
                package_type=PackageType.PG,
                instrumentation_name="Psycopg2Instrumentation",
                submodule_name="query",
                input_value=input_value,
                output_value=None,
                input_schema=None,  # type: ignore[arg-type]
                output_schema=None,  # type: ignore[arg-type]
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

            logger.info("[MOCK_REQUEST] Requesting mock from CLI:")
            logger.info(f"  replay_trace_id={replay_trace_id}")
            logger.info(f"  trace_id={trace_id}")
            logger.info(f"  span_id={span_id}")
            logger.info(f"  parent_span_id={parent_span_id or 'None'}")
            logger.info(f"  package_name={mock_span.package_name}")
            logger.info(f"  package_type={mock_span.package_type}")
            logger.info(f"  instrumentation_name={mock_span.instrumentation_name}")
            logger.info(f"  input_value_hash={mock_span.input_value_hash}")
            logger.info(f"  input_schema_hash={mock_span.input_schema_hash}")
            logger.info(f"  is_pre_app_start={mock_span.is_pre_app_start}")
            logger.info(f"  query={query_str[:100]}")

            # Check if communicator is connected before requesting mock
            if not sdk.communicator or not sdk.communicator.is_connected:
                logger.warning("[MOCK_REQUEST] CLI communicator is not connected yet!")
                logger.warning(f"[MOCK_REQUEST]   is_pre_app_start={mock_span.is_pre_app_start}")

                if mock_span.is_pre_app_start:
                    # For pre-app-start queries, return None (will trigger empty result fallback)
                    logger.warning(
                        "[MOCK_REQUEST] Pre-app-start query and CLI not ready - returning None to use empty result"
                    )
                    return None
                else:
                    # For in-request queries, this is an error but we'll return None to be safe
                    logger.error("[MOCK_REQUEST] In-request query but CLI not connected - returning None")
                    return None

            logger.debug("[MOCK_REQUEST] Calling sdk.request_mock_sync()...")
            mock_response_output = sdk.request_mock_sync(mock_request)
            logger.info(
                f"[MOCK_RESPONSE] CLI returned: found={mock_response_output.found}, response={mock_response_output.response is not None}"
            )

            if mock_response_output.response:
                logger.debug(
                    f"[MOCK_RESPONSE] Response keys: {mock_response_output.response.keys() if isinstance(mock_response_output.response, dict) else 'not a dict'}"
                )

            if not mock_response_output.found:
                logger.error(
                    f"No mock found for psycopg2 query:\n"
                    f"  replay_trace_id={replay_trace_id}\n"
                    f"  span_trace_id={trace_id}\n"
                    f"  span_id={span_id}\n"
                    f"  package_name={mock_span.package_name}\n"
                    f"  package_type={mock_span.package_type}\n"
                    f"  name={mock_span.name}\n"
                    f"  input_value_hash={mock_span.input_value_hash}\n"
                    f"  input_schema_hash={mock_span.input_schema_hash}\n"
                    f"  query={query_str[:100]}"
                )
                return None

            logger.info(f"[MOCK_FOUND] Found mock for psycopg2 query: {query_str[:100]}")
            logger.info(f"[MOCK_FOUND] Mock response type: {type(mock_response_output.response)}")
            logger.info(f"[MOCK_FOUND] Mock response: {mock_response_output.response}")
            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg2 query: {e}")
            return None

    def _mock_execute_with_data(self, cursor: Any, mock_data: dict[str, Any]) -> None:
        """Mock the cursor execute by setting internal state directly.

        In psycopg2, cursor.execute() sets internal C-level attributes that we can't modify.
        Instead, we directly set the private attributes that psycopg2 uses internally.
        """
        # The SDK communicator already extracts response.body from the CLI's MockInteraction.
        # So mock_data should contain: {"rowcount": ..., "description": [...], "rows": [...]}
        actual_data = mock_data
        logger.debug(
            f"Mocking execute with data. Actual data keys: {actual_data.keys() if isinstance(actual_data, dict) else 'not a dict'}"
        )

        # Set internal cursor state that gets populated during execute()
        # These are internal attributes that psycopg2 uses
        try:
            # rowcount: psycopg2 stores this in cursor.rowcount (read-only property)
            # We need to set the internal C attribute directly using object.__setattr__
            object.__setattr__(cursor, "rowcount", actual_data.get("rowcount", -1))
        except (AttributeError, TypeError) as e:
            logger.debug(f"Could not set rowcount via __setattr__: {e}")
            # Try setting the private attribute that backs rowcount
            try:
                cursor._rowcount = actual_data.get("rowcount", -1)
            except AttributeError:
                logger.debug("Could not set _rowcount either")

        # description: psycopg2 description format
        description_data = actual_data.get("description")
        if description_data:
            # Convert to psycopg2 Column format
            desc = [(col["name"], col.get("type_code"), None, None, None, None, None) for col in description_data]
            try:
                object.__setattr__(cursor, "description", desc)
            except (AttributeError, TypeError):
                try:
                    cursor._description = desc
                except AttributeError:
                    logger.debug("Could not set description")

        # Store mock rows for fetching
        mock_rows = actual_data.get("rows", [])
        # Deserialize datetime strings back to datetime objects for consistent Flask/Django serialization
        mock_rows = [deserialize_db_value(row) for row in mock_rows]
        cursor._mock_rows = mock_rows  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        # Patch fetch methods
        cursor.fetchone if hasattr(cursor, "fetchone") else None
        cursor.fetchmany if hasattr(cursor, "fetchmany") else None
        cursor.fetchall if hasattr(cursor, "fetchall") else None

        def mock_fetchone():
            if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                # Convert list to tuple to match psycopg2 behavior
                return tuple(row) if isinstance(row, list) else row
            return None

        def mock_fetchmany(size=cursor.arraysize):
            rows = []
            for _ in range(size):
                row = mock_fetchone()
                if row is None:
                    break
                rows.append(row)
            return rows

        def mock_fetchall():
            logger.debug(f"[MOCK] fetchall called, returning {len(cursor._mock_rows[cursor._mock_index :])} rows")  # pyright: ignore[reportAttributeAccessIssue]
            rows = cursor._mock_rows[cursor._mock_index :]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_index = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
            # Convert lists to tuples to match psycopg2 behavior
            result = [tuple(row) if isinstance(row, list) else row for row in rows]
            logger.debug(f"[MOCK] fetchall returning: {result}")
            return result

        logger.debug(f"[MOCK] Patching cursor fetch methods with mock data ({len(mock_rows)} rows)")
        cursor.fetchone = mock_fetchone  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchmany = mock_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchall = mock_fetchall  # pyright: ignore[reportAttributeAccessIssue]
        logger.debug("[MOCK] Cursor fetch methods patched successfully")

    def _finalize_query_span(
        self,
        span: trace.Span,
        cursor: Any,
        query: QueryType,
        params: Any,
        error: Exception | None,
    ) -> None:
        """Finalize span with query data."""
        try:
            # Helper function to serialize non-JSON types
            import datetime

            def serialize_value(val):
                """Convert non-JSON-serializable values to JSON-compatible types."""
                if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
                    return val.isoformat()
                elif isinstance(val, bytes):
                    return val.decode("utf-8", errors="replace")
                elif isinstance(val, (list, tuple)):
                    return [serialize_value(v) for v in val]
                elif isinstance(val, dict):
                    return {k: serialize_value(v) for k, v in val.items()}
                return val

            # Build input value
            query_str = _query_to_str(query)
            input_value = {
                "query": query_str.strip(),
            }
            if params is not None:
                # Serialize parameters to handle datetime and other non-JSON types
                input_value["parameters"] = serialize_value(params)

            # Build output value
            output_value = {}

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                span.set_status(Status(OTelStatusCode.ERROR, str(error)))
            else:
                # Get query results and capture for replay
                try:
                    rows = []
                    description = None

                    # Try to fetch results if available
                    if hasattr(cursor, "description") and cursor.description:
                        description = [
                            {
                                "name": desc[0],
                                "type_code": desc[1] if len(desc) > 1 else None,
                            }
                            for desc in cursor.description
                        ]

                        # Fetch all rows for recording
                        # We need to capture these for replay mode
                        try:
                            all_rows = cursor.fetchall()
                            # Convert tuples to lists for JSON serialization
                            rows = [list(row) for row in all_rows]

                            # CRITICAL: Re-populate cursor so user code can still fetch
                            # We'll store the rows and patch fetch methods
                            cursor._tusk_rows = all_rows  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                            # Save original fetch methods
                            (cursor.fetchone if hasattr(cursor, "fetchone") else None)
                            (cursor.fetchmany if hasattr(cursor, "fetchmany") else None)
                            (cursor.fetchall if hasattr(cursor, "fetchall") else None)

                            # Replace with our versions that return stored rows
                            def patched_fetchone():
                                if cursor._tusk_index < len(cursor._tusk_rows):  # pyright: ignore[reportAttributeAccessIssue]
                                    row = cursor._tusk_rows[cursor._tusk_index]  # pyright: ignore[reportAttributeAccessIssue]
                                    cursor._tusk_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                                    return row
                                return None

                            def patched_fetchmany(size=cursor.arraysize):
                                result = cursor._tusk_rows[cursor._tusk_index : cursor._tusk_index + size]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_index += len(result)  # pyright: ignore[reportAttributeAccessIssue]
                                return result

                            def patched_fetchall():
                                result = cursor._tusk_rows[cursor._tusk_index :]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_index = len(cursor._tusk_rows)  # pyright: ignore[reportAttributeAccessIssue]
                                return result

                            cursor.fetchone = patched_fetchone  # pyright: ignore[reportAttributeAccessIssue]
                            cursor.fetchmany = patched_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
                            cursor.fetchall = patched_fetchall  # pyright: ignore[reportAttributeAccessIssue]

                        except Exception as fetch_error:
                            logger.debug(f"Could not fetch rows (query might not return rows): {fetch_error}")
                            rows = []

                    output_value = {
                        "rowcount": cursor.rowcount if hasattr(cursor, "rowcount") else -1,
                    }

                    if description:
                        output_value["description"] = description

                    if rows:
                        # Convert rows to JSON-serializable format (handle datetime objects, etc.)
                        serialized_rows = [[serialize_value(col) for col in row] for row in rows]
                        output_value["rows"] = serialized_rows

                except Exception as e:
                    logger.debug(f"Error getting query metadata: {e}")

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

            if not error:
                span.set_status(Status(OTelStatusCode.OK))

            logger.debug("[PSYCOPG2] Span finalized successfully")

        except Exception as e:
            logger.error(f"Error creating query span: {e}")
