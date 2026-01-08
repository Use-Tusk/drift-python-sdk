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
from ..utils.psycopg_utils import deserialize_db_value

logger = logging.getLogger(__name__)

_instance: PsycopgInstrumentation | None = None


class MockLoader:
    """Mock loader for psycopg3."""

    def __init__(self):
        self.timezone = None  # Django expects this attribute

    def __call__(self, data):
        """No-op load function."""
        return data


class MockDumper:
    """Mock dumper for psycopg3."""

    def __call__(self, obj):
        """No-op dump function."""
        return str(obj).encode("utf-8")


class MockAdapters:
    """Mock adapters for psycopg3 connection."""

    def get_loader(self, oid, format):
        """Return a mock loader."""
        return MockLoader()

    def get_dumper(self, obj, format):
        """Return a mock dumper."""
        return MockDumper()

    def register_loader(self, oid, loader):
        """No-op register loader for Django compatibility."""
        pass

    def register_dumper(self, oid, dumper):
        """No-op register dumper for Django compatibility."""
        pass


class MockConnection:
    """Mock database connection for REPLAY mode when postgres is not available.

    Provides minimal interface for Django/Flask to work without a real database.
    All queries are mocked at the cursor.execute() level.
    """

    def __init__(self, sdk: TuskDrift, instrumentation: PsycopgInstrumentation, cursor_factory):
        self.sdk = sdk
        self.instrumentation = instrumentation
        self.cursor_factory = cursor_factory
        self.closed = False
        self.autocommit = False

        # Django/psycopg3 requires these for connection initialization
        self.isolation_level = None
        self.encoding = "UTF8"
        self.adapters = MockAdapters()
        self.pgconn = None  # Mock pg connection object

        # Create a comprehensive mock info object for Django
        class MockInfo:
            vendor = "postgresql"
            server_version = 150000  # PostgreSQL 15.0 as integer
            encoding = "UTF8"

            def parameter_status(self, param):
                """Return mock parameter status."""
                if param == "TimeZone":
                    return "UTC"
                elif param == "server_version":
                    return "15.0"
                return None

        self.info = MockInfo()

        logger.debug("[MOCK_CONNECTION] Created mock connection for REPLAY mode (psycopg3)")

    def cursor(self, name=None, cursor_factory=None):
        """Create a cursor using the instrumented cursor factory."""
        # For mock connections, we create a MockCursor directly
        cursor = MockCursor(self)

        # Wrap execute/executemany for mock cursor
        instrumentation = self.instrumentation
        sdk = self.sdk

        def mock_execute(query, params=None, **kwargs):
            # For mock cursor, original_execute is just a no-op
            def noop_execute(q, p, **kw):
                return cursor

            return instrumentation._traced_execute(cursor, noop_execute, sdk, query, params, **kwargs)

        def mock_executemany(query, params_seq, **kwargs):
            # For mock cursor, original_executemany is just a no-op
            def noop_executemany(q, ps, **kw):
                return cursor

            return instrumentation._traced_executemany(cursor, noop_executemany, sdk, query, params_seq, **kwargs)

        # Monkey-patch mock functions onto cursor
        cursor.execute = mock_execute  # type: ignore[method-assign]
        cursor.executemany = mock_executemany  # type: ignore[method-assign]

        logger.debug("[MOCK_CONNECTION] Created cursor (psycopg3)")
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
        self._tusk_description = None  # Store mock description
        self.arraysize = 1
        self._mock_rows = []
        self._mock_index = 0
        self.adapters = MockAdapters()  # Django needs this
        logger.debug("[MOCK_CURSOR] Created fallback mock cursor (psycopg3)")

    @property
    def description(self):
        return self._tusk_description

    def execute(self, query, params=None, **kwargs):
        """Will be replaced by instrumentation."""
        logger.debug(f"[MOCK_CURSOR] execute() called: {query[:100]}")
        return self

    def executemany(self, query, params_seq, **kwargs):
        """Will be replaced by instrumentation."""
        logger.debug(f"[MOCK_CURSOR] executemany() called: {query[:100]}")
        return self

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


class PsycopgInstrumentation(InstrumentationBase):
    """Instrumentation for psycopg (psycopg3) PostgreSQL client library.

    In REPLAY mode, if postgres is not available, a mock connection is used.
    """

    def __init__(self, enabled: bool = True) -> None:
        global _instance
        super().__init__(
            name="PsycopgInstrumentation",
            module_name="psycopg",
            supported_versions=">=3.1.12",
            enabled=enabled,
        )
        self._original_connect = None
        _instance = self

    def patch(self, module: ModuleType) -> None:
        """Patch the psycopg module."""
        if not hasattr(module, "connect"):
            logger.warning("psycopg.connect not found, skipping instrumentation")
            return

        self._original_connect = module.connect
        instrumentation = self
        original_connect = self._original_connect

        def patched_connect(*args, **kwargs):
            """Patched psycopg.connect method."""
            sdk = TuskDrift.get_instance()

            if sdk.mode == TuskDriftMode.DISABLED or original_connect is None:
                if original_connect is None:
                    raise RuntimeError("Original psycopg.connect not found")
                return original_connect(*args, **kwargs)

            user_cursor_factory = kwargs.pop("cursor_factory", None)
            cursor_factory = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # In REPLAY mode, try to connect but fall back to mock connection if DB is unavailable
            if sdk.mode == TuskDriftMode.REPLAY:
                try:
                    kwargs["cursor_factory"] = cursor_factory
                    connection = original_connect(*args, **kwargs)
                    logger.info("[PATCHED_CONNECT] REPLAY mode: Successfully connected to database (psycopg3)")
                    return connection
                except Exception as e:
                    logger.info(
                        f"[PATCHED_CONNECT] REPLAY mode: Database connection failed ({e}), using mock connection (psycopg3)"
                    )
                    # Return mock connection that doesn't require a real database
                    return MockConnection(sdk, instrumentation, cursor_factory)

            # In RECORD mode, always require real connection
            kwargs["cursor_factory"] = cursor_factory
            connection = original_connect(*args, **kwargs)
            logger.debug("[PATCHED_CONNECT] RECORD mode: Connected to database (psycopg3)")
            return connection

        module.connect = patched_connect  # type: ignore[attr-defined]
        logger.debug("psycopg.connect instrumented")

    def _create_cursor_factory(self, sdk: TuskDrift, base_factory=None):
        """Create a cursor factory that wraps cursors with instrumentation.

        Returns a cursor CLASS (psycopg3 expects a class, not a function).
        """
        instrumentation = self
        logger.debug(f"[CURSOR_FACTORY] Creating cursor factory, sdk.mode={sdk.mode}")

        # For real connections, psycopg3 expects a cursor CLASS
        try:
            from psycopg import Cursor as BaseCursor
        except ImportError:
            logger.warning("[CURSOR_FACTORY] Could not import psycopg.Cursor")
            # Return a basic cursor class
            BaseCursor = object  # type: ignore

        base = base_factory or BaseCursor

        class InstrumentedCursor(base):  # type: ignore
            _tusk_description = None  # Store mock description for replay mode

            @property
            def description(self):
                # In replay mode, return mock description if set; otherwise use base
                if self._tusk_description is not None:
                    return self._tusk_description
                return super().description

            def execute(self, query, params=None, **kwargs):
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)

            def executemany(self, query, params_seq, **kwargs):
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, params_seq, **kwargs)

        return InstrumentedCursor

    def _traced_execute(
        self, cursor: Any, original_execute: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.execute method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_execute(query, params, **kwargs)

        query_str = self._query_to_string(query, cursor)

        # Create OpenTelemetry span
        tracer = sdk.get_tracer()
        span = tracer.start_span(
            name="psycopg.query",
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: "psycopg.query",
                TdSpanAttributes.PACKAGE_NAME: "psycopg",
                TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                TdSpanAttributes.SUBMODULE_NAME: "query",
                TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        try:
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")

            parent_span = trace.get_current_span(ctx)
            parent_span_id = None
            if parent_span and parent_span.is_recording():
                parent_ctx = parent_span.get_span_context()
                parent_span_id = format(parent_ctx.span_id, "016x")

            if sdk.mode == TuskDriftMode.REPLAY:
                # Handle background requests (app ready + no parent span)
                if sdk.app_ready and not parent_span_id:
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return cursor

                mock_result = self._try_get_mock(sdk, query_str, params, trace_id, span_id, parent_span_id)

                if mock_result is None:
                    is_pre_app_start = not sdk.app_ready
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg execute query. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )

                self._mock_execute_with_data(cursor, mock_result)
                return cursor

            # RECORD mode
            time.time()
            error = None

            try:
                result = original_execute(query, params, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_query_span(
                        span,
                        cursor,
                        query_str,
                        params,
                        error,
                    )
        finally:
            otel_context.detach(token)
            span.end()

    def _traced_executemany(
        self, cursor: Any, original_executemany: Any, sdk: TuskDrift, query: str, params_seq, **kwargs
    ) -> Any:
        """Traced cursor.executemany method."""
        # Pass through if SDK is disabled
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_executemany(query, params_seq, **kwargs)

        # Convert query to string if it's a Composed SQL object
        query_str = self._query_to_string(query, cursor)

        # Create OpenTelemetry span
        tracer = sdk.get_tracer()
        span = tracer.start_span(
            name="psycopg.query",
            kind=OTelSpanKind.CLIENT,
            attributes={
                TdSpanAttributes.NAME: "psycopg.query",
                TdSpanAttributes.PACKAGE_NAME: "psycopg",
                TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                TdSpanAttributes.SUBMODULE_NAME: "query",
                TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
            },
        )

        ctx = otel_context.get_current()
        ctx_with_span = set_span_in_context(span, ctx)
        token = otel_context.attach(ctx_with_span)

        try:
            span_context = span.get_span_context()
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")

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
                    # Return the cursor
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return cursor

                # For all other queries (pre-app-start OR within a request trace), get mock
                # Convert params_seq to list for serialization
                # Wrap in {"_batch": ...} to match the recording format
                params_list = list(params_seq)
                mock_result = self._try_get_mock(
                    sdk,
                    query_str,
                    {"_batch": params_list},
                    trace_id,
                    span_id,
                    parent_span_id,
                )
                if mock_result is None:
                    # In REPLAY mode, we MUST have a mock for ALL queries
                    is_pre_app_start = not sdk.app_ready
                    logger.error(
                        f"No mock found for {'pre-app-start ' if is_pre_app_start else ''}psycopg executemany query in REPLAY mode: {query_str[:100]}"
                    )
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg executemany query. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )

                # Mock execute by setting cursor internal state
                self._mock_execute_with_data(cursor, mock_result)
                return cursor  # psycopg3 executemany() returns cursor

            # RECORD mode: Execute real query and record span
            time.time()
            error = None
            # Convert to list BEFORE executing to avoid iterator exhaustion
            params_list = list(params_seq)

            try:
                result = original_executemany(query, params_list, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                # Always create span in RECORD mode (including pre-app-start queries)
                # Pre-app-start queries are marked with is_pre_app_start=true flag
                if sdk.mode == TuskDriftMode.RECORD:
                    self._finalize_query_span(
                        span,
                        cursor,
                        query_str,
                        {"_batch": params_list},
                        error,
                    )
        finally:
            # Reset only span context (trace context is owned by parent)
            otel_context.detach(token)
            span.end()

    def _query_to_string(self, query: Any, cursor: Any) -> str:
        """Convert query to string."""
        try:
            from psycopg.sql import Composed

            if isinstance(query, Composed):
                return query.as_string(cursor)
        except ImportError:
            pass

        return str(query) if not isinstance(query, str) else query

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        query: str,
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
            input_value = {
                "query": query.strip(),
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
                name="psycopg.query",
                package_name="psycopg",
                package_type=PackageType.PG,
                instrumentation_name="PsycopgInstrumentation",
                submodule_name="query",
                input_value=input_value,
                output_value=None,
                input_schema=None,  # type: ignore - Must be None to avoid betterproto serialization issues
                output_schema=None,  # type: ignore - Must be None to avoid betterproto serialization issues
                input_schema_hash=input_result.decoded_schema_hash,
                output_schema_hash="",
                input_value_hash=input_result.decoded_value_hash,
                output_value_hash="",
                stack_trace="",  # Empty in REPLAY mode
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

            logger.debug(f"Requesting mock from CLI for query: {query[:50]}...")
            mock_response_output = sdk.request_mock_sync(mock_request)
            logger.debug(f"CLI returned: found={mock_response_output.found}")

            if not mock_response_output.found:
                logger.debug(f"No mock found for psycopg query: {query[:100]}")
                return None

            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg query: {e}")
            return None

    def _mock_execute_with_data(self, cursor: Any, mock_data: dict[str, Any]) -> None:
        """Mock cursor execute by setting internal state."""
        # The SDK communicator already extracts response.body from the CLI's MockInteraction structure
        # So mock_data should already contain: {"rowcount": ..., "description": [...], "rows": [...]}
        actual_data = mock_data
        logger.debug(f"[MOCK_DATA] mock_data: {mock_data}")

        try:
            cursor._rowcount = actual_data.get("rowcount", -1)
        except AttributeError:
            object.__setattr__(cursor, "rowcount", actual_data.get("rowcount", -1))

        description_data = actual_data.get("description")
        if description_data:
            desc = [(col["name"], col.get("type_code"), None, None, None, None, None) for col in description_data]
            # Set mock description - InstrumentedCursor has _tusk_description property
            # MockCursor uses regular description attribute
            try:
                cursor._tusk_description = desc
            except AttributeError:
                # For MockCursor, set description directly
                try:
                    cursor.description = desc
                except AttributeError:
                    pass

        mock_rows = actual_data.get("rows", [])
        # Deserialize datetime strings back to datetime objects for consistent Flask serialization
        mock_rows = [deserialize_db_value(row) for row in mock_rows]
        cursor._mock_rows = mock_rows  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        def mock_fetchone():
            if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
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
            rows = cursor._mock_rows[cursor._mock_index :]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_index = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
            return [tuple(row) if isinstance(row, list) else row for row in rows]

        cursor.fetchone = mock_fetchone  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchmany = mock_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchall = mock_fetchall  # pyright: ignore[reportAttributeAccessIssue]

    def _finalize_query_span(
        self,
        span: trace.Span,
        cursor: Any,
        query: str,
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
            input_value = {
                "query": query.strip(),
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
                                "name": desc[0] if hasattr(desc, "__getitem__") else desc.name,
                                "type_code": desc[1]
                                if hasattr(desc, "__getitem__") and len(desc) > 1
                                else getattr(desc, "type_code", None),
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

            logger.debug("[PSYCOPG] Span finalized successfully")

        except Exception as e:
            logger.error(f"Error creating query span: {e}")
