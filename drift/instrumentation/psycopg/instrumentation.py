from __future__ import annotations

import json
import logging
from types import ModuleType
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode as OTelStatusCode

from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper
from ...core.mode_utils import handle_record_mode, handle_replay_mode
from ...core.tracing import TdSpanAttributes
from ...core.tracing.span_utils import CreateSpanOptions, SpanUtils
from ...core.types import (
    PackageType,
    SpanKind,
    TuskDriftMode,
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

    def cursor(self, name=None, *, cursor_factory=None, **kwargs):
        """Create a cursor using the instrumented cursor factory.

        Accepts the same parameters as psycopg's Connection.cursor(), including
        server cursor parameters like scrollable and withhold.
        """
        # For mock connections, we create a MockCursor directly
        # The name parameter is accepted but not used since mock cursors
        # behave the same for both regular and server cursors
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

        def mock_stream(query, params=None, **kwargs):
            # For mock cursor, original_stream is just a no-op generator
            def noop_stream(q, p, **kw):
                return iter([])

            return instrumentation._traced_stream(cursor, noop_stream, sdk, query, params, **kwargs)

        # Monkey-patch mock functions onto cursor
        cursor.execute = mock_execute  # type: ignore[method-assign]
        cursor.executemany = mock_executemany  # type: ignore[method-assign]
        cursor.stream = mock_stream  # type: ignore[method-assign]

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

    def stream(self, query, params=None, **kwargs):
        """Will be replaced by instrumentation."""
        return iter([])

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

            # Create server cursor factory for named cursors (conn.cursor(name="..."))
            server_cursor_factory = instrumentation._create_server_cursor_factory(sdk)

            # In REPLAY mode, try to connect but fall back to mock connection if DB is unavailable
            if sdk.mode == TuskDriftMode.REPLAY:
                try:
                    kwargs["cursor_factory"] = cursor_factory
                    connection = original_connect(*args, **kwargs)
                    # Set server cursor factory on the connection for named cursors
                    if server_cursor_factory:
                        connection.server_cursor_factory = server_cursor_factory
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
            # Set server cursor factory on the connection for named cursors
            if server_cursor_factory:
                connection.server_cursor_factory = server_cursor_factory
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

            def stream(self, query, params=None, **kwargs):
                return instrumentation._traced_stream(self, super().stream, sdk, query, params, **kwargs)

        return InstrumentedCursor

    def _create_server_cursor_factory(self, sdk: TuskDrift, base_factory=None):
        """Create a server cursor factory that wraps ServerCursor with instrumentation.

        Returns a cursor CLASS (psycopg3 expects a class, not a function).
        ServerCursor is used when conn.cursor(name="...") is called.
        """
        instrumentation = self
        logger.debug(f"[CURSOR_FACTORY] Creating server cursor factory, sdk.mode={sdk.mode}")

        try:
            from psycopg import ServerCursor as BaseServerCursor
        except ImportError:
            logger.warning("[CURSOR_FACTORY] Could not import psycopg.ServerCursor")
            return None

        base = base_factory or BaseServerCursor

        class InstrumentedServerCursor(base):  # type: ignore
            _tusk_description = None  # Store mock description for replay mode

            @property
            def description(self):
                # In replay mode, return mock description if set; otherwise use base
                if self._tusk_description is not None:
                    return self._tusk_description
                return super().description

            def execute(self, query, params=None, **kwargs):
                # Note: ServerCursor.execute() doesn't support 'prepare' parameter
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)

            # Note: ServerCursor doesn't support executemany()
            # Note: ServerCursor has stream-like iteration via fetchmany/itersize

        return InstrumentedServerCursor

    def _traced_execute(
        self, cursor: Any, original_execute: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.execute method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_execute(query, params, **kwargs)

        query_str = self._query_to_string(query, cursor)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_execute(cursor, sdk, query_str, params),
                no_op_request_handler=lambda: self._noop_execute(cursor),
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_execute(query, params, **kwargs),
            record_mode_handler=lambda is_pre_app_start: self._record_execute(
                cursor, original_execute, sdk, query, query_str, params, is_pre_app_start, kwargs
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _noop_execute(self, cursor: Any) -> Any:
        """Handle background requests in REPLAY mode - return cursor with empty mock data."""
        cursor._mock_rows = []  # pyright: ignore
        cursor._mock_index = 0  # pyright: ignore
        return cursor

    def _replay_execute(self, cursor: Any, sdk: TuskDrift, query_str: str, params: Any) -> Any:
        """Handle REPLAY mode for execute - fetch mock from CLI."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
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
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError("Error creating span in replay mode")

        with SpanUtils.with_span(span_info):
            mock_result = self._try_get_mock(
                sdk, query_str, params, span_info.trace_id, span_info.span_id
            )

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg execute query. "
                    f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                    f"Query: {query_str[:100]}..."
                )

            self._mock_execute_with_data(cursor, mock_result)
            span_info.span.end()
            return cursor

    def _record_execute(
        self,
        cursor: Any,
        original_execute: Any,
        sdk: TuskDrift,
        query: str,
        query_str: str,
        params: Any,
        is_pre_app_start: bool,
        kwargs: dict,
    ) -> Any:
        """Handle RECORD mode for execute - create span and execute query."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg.query",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Fallback to original call if span creation fails
            return original_execute(query, params, **kwargs)

        error = None
        result = None

        with SpanUtils.with_span(span_info):
            try:
                result = original_execute(query, params, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                self._finalize_query_span(
                    span_info.span,
                    cursor,
                    query_str,
                    params,
                    error,
                )
                span_info.span.end()

    def _traced_executemany(
        self, cursor: Any, original_executemany: Any, sdk: TuskDrift, query: str, params_seq, **kwargs
    ) -> Any:
        """Traced cursor.executemany method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_executemany(query, params_seq, **kwargs)

        query_str = self._query_to_string(query, cursor)
        # Convert to list BEFORE executing to avoid iterator exhaustion
        params_list = list(params_seq)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_executemany(cursor, sdk, query_str, params_list),
                no_op_request_handler=lambda: self._noop_execute(cursor),
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_executemany(query, params_list, **kwargs),
            record_mode_handler=lambda is_pre_app_start: self._record_executemany(
                cursor, original_executemany, sdk, query, query_str, params_list, is_pre_app_start, kwargs
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _replay_executemany(self, cursor: Any, sdk: TuskDrift, query_str: str, params_list: list) -> Any:
        """Handle REPLAY mode for executemany - fetch mock from CLI."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
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
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError("Error creating span in replay mode")

        with SpanUtils.with_span(span_info):
            mock_result = self._try_get_mock(
                sdk, query_str, {"_batch": params_list}, span_info.trace_id, span_info.span_id
            )

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                logger.error(
                    f"No mock found for {'pre-app-start ' if is_pre_app_start else ''}psycopg executemany query in REPLAY mode: {query_str[:100]}"
                )
                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg executemany query. "
                    f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                    f"Query: {query_str[:100]}..."
                )

            self._mock_execute_with_data(cursor, mock_result)
            span_info.span.end()
            return cursor

    def _record_executemany(
        self,
        cursor: Any,
        original_executemany: Any,
        sdk: TuskDrift,
        query: str,
        query_str: str,
        params_list: list,
        is_pre_app_start: bool,
        kwargs: dict,
    ) -> Any:
        """Handle RECORD mode for executemany - create span and execute query."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg.query",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Fallback to original call if span creation fails
            return original_executemany(query, params_list, **kwargs)

        error = None
        result = None

        with SpanUtils.with_span(span_info):
            try:
                result = original_executemany(query, params_list, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                self._finalize_query_span(
                    span_info.span,
                    cursor,
                    query_str,
                    {"_batch": params_list},
                    error,
                )
                span_info.span.end()

    def _traced_stream(
        self, cursor: Any, original_stream: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.stream method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_stream(query, params, **kwargs)

        query_str = self._query_to_string(query, cursor)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_stream(cursor, sdk, query_str, params),
                no_op_request_handler=lambda: iter([]),  # Empty iterator for background requests
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_stream(query, params, **kwargs),
            record_mode_handler=lambda is_pre_app_start: self._record_stream(
                cursor, original_stream, sdk, query, query_str, params, is_pre_app_start, kwargs
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _record_stream(
        self,
        cursor: Any,
        original_stream: Any,
        sdk: TuskDrift,
        query: str,
        query_str: str,
        params: Any,
        is_pre_app_start: bool,
        kwargs: dict,
    ):
        """Handle RECORD mode for stream - wrap generator with tracing."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg.query",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            yield from original_stream(query, params, **kwargs)
            return

        rows_collected = []
        error = None

        try:
            with SpanUtils.with_span(span_info):
                for row in original_stream(query, params, **kwargs):
                    rows_collected.append(row)
                    yield row
        except Exception as e:
            error = e
            raise
        finally:
            self._finalize_stream_span(span_info.span, cursor, query_str, params, rows_collected, error)
            span_info.span.end()

    def _replay_stream(self, cursor: Any, sdk: TuskDrift, query_str: str, params: Any):
        """Handle REPLAY mode for stream - return mock generator."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
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
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError("Error creating span in replay mode")

        with SpanUtils.with_span(span_info):
            mock_result = self._try_get_mock(
                sdk, query_str, params, span_info.trace_id, span_info.span_id
            )

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg stream query. "
                    f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded. "
                    f"Query: {query_str[:100]}..."
                )

            # Deserialize and yield rows from mock
            rows = mock_result.get("rows", [])
            for row in rows:
                deserialized = deserialize_db_value(row)
                yield tuple(deserialized) if isinstance(deserialized, list) else deserialized

            span_info.span.end()

    def _finalize_stream_span(
        self,
        span: trace.Span,
        cursor: Any,
        query: str,
        params: Any,
        rows: list,
        error: Exception | None,
    ) -> None:
        """Finalize span for stream operation with collected rows."""
        try:
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
                # Use pre-collected rows (unlike _finalize_query_span which calls fetchall)
                serialized_rows = [[serialize_value(col) for col in row] for row in rows]

                output_value = {
                    "rowcount": len(rows),
                }

                if serialized_rows:
                    output_value["rows"] = serialized_rows

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

            logger.debug("[PSYCOPG] Stream span finalized successfully")

        except Exception as e:
            logger.error(f"Error finalizing stream span: {e}")

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

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name="psycopg.query",
                package_name="psycopg",
                package_type=PackageType.PG,
                instrumentation_name="PsycopgInstrumentation",
                submodule_name="query",
                input_value=input_value,
                kind=SpanKind.CLIENT,
                is_pre_app_start=not sdk.app_ready,
            )

            if not mock_response_output or not mock_response_output.found:
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
