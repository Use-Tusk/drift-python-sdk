"""Instrumentation for psycopg2 PostgreSQL client library."""

from __future__ import annotations

import json
import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from psycopg2.extensions import cursor as BaseCursorType
    from psycopg2.sql import Composable

    QueryType = Union[str, bytes, Composable]

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
from ..utils.serialization import serialize_value

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


class InstrumentedConnection:
    """Wraps a real psycopg2 connection to intercept cursor() calls.

    This ensures that even when users pass cursor_factory to cursor() instead of
    connect(), the cursor is still instrumented for tracing.
    """

    def __init__(self, connection: Any, instrumentation: Psycopg2Instrumentation, sdk: TuskDrift) -> None:
        # Use object.__setattr__ to avoid triggering __getattr__
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_instrumentation", instrumentation)
        object.__setattr__(self, "_sdk", sdk)
        # Preserve the connection's default cursor_factory (set at connect() time)
        object.__setattr__(self, "_default_cursor_factory", getattr(connection, "cursor_factory", None))

    def cursor(self, name: str | None = None, cursor_factory: Any = None, *args: Any, **kwargs: Any) -> Any:
        """Intercept cursor creation to wrap user-provided cursor_factory."""
        # Use cursor_factory from cursor() call, or fall back to connection's default
        base_factory = cursor_factory if cursor_factory is not None else self._default_cursor_factory
        # Create instrumented cursor factory (wrapping the base factory)
        wrapped_factory = self._instrumentation._create_cursor_factory(
            self._sdk,
            base_factory,
        )
        return self._connection.cursor(*args, name=name, cursor_factory=wrapped_factory, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Proxy all other methods/attributes to the real connection."""
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Proxy attribute setting to the real connection."""
        setattr(self._connection, name, value)

    def __enter__(self) -> InstrumentedConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._connection.__exit__(*args)


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

            # In REPLAY mode, try to connect but fall back to mock connection if DB is unavailable
            if sdk.mode == TuskDriftMode.REPLAY:
                try:
                    logger.debug("[PATCHED_CONNECT] REPLAY mode: Attempting real DB connection...")
                    connection = original_connect(*args, **kwargs)
                    logger.info("[PATCHED_CONNECT] REPLAY mode: Successfully connected to real database")
                    # Wrap connection to intercept cursor() calls
                    return InstrumentedConnection(connection, instrumentation, sdk)
                except Exception as e:
                    logger.info(
                        f"[PATCHED_CONNECT] REPLAY mode: Database connection failed ({e}), using mock connection"
                    )
                    # Return mock connection that doesn't require a real database
                    # MockConnection already handles cursor_factory correctly in its cursor() method
                    return MockConnection(sdk, instrumentation, None)

            # In RECORD mode, always require real connection
            logger.debug("[PATCHED_CONNECT] RECORD mode: Connecting to database...")
            connection = original_connect(*args, **kwargs)
            logger.info("[PATCHED_CONNECT] RECORD mode: Connected to database successfully")
            # Wrap connection to intercept cursor() calls
            return InstrumentedConnection(connection, instrumentation, sdk)

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
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_execute(query, params)

        query_str = _query_to_str(query)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_execute(cursor, sdk, query_str, params),
                no_op_request_handler=lambda: self._noop_execute(cursor),
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_execute(query, params),
            record_mode_handler=lambda is_pre_app_start: self._record_execute(
                cursor, original_execute, sdk, query, query_str, params, is_pre_app_start
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _noop_execute(self, cursor: Any) -> None:
        """Handle background requests in REPLAY mode - return None with empty cursor state."""
        cursor.rowcount = 0
        cursor._mock_rows = []  # pyright: ignore
        cursor._mock_index = 0  # pyright: ignore
        return None

    def _replay_execute(self, cursor: Any, sdk: TuskDrift, query_str: str, params: Any) -> None:
        """Handle REPLAY mode for execute - fetch mock from CLI."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
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
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError("Error creating span in replay mode")

        with SpanUtils.with_span(span_info):
            mock_result = self._try_get_mock(sdk, query_str, params, span_info.trace_id, span_info.span_id)

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                if is_pre_app_start:
                    logger.warning("[PSYCOPG2_REPLAY] No mock found for pre-app-start query, returning empty result")
                    empty_mock = {"rowcount": 0, "rows": [], "description": None}
                    self._mock_execute_with_data(cursor, empty_mock)
                    span_info.span.end()
                    return None

                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg2 execute query. "
                    f"This query was not recorded during the trace capture. "
                    f"Query: {query_str[:100]}..."
                )

            self._mock_execute_with_data(cursor, mock_result)
            span_info.span.end()
            return None

    def _record_execute(
        self,
        cursor: Any,
        original_execute: Any,
        sdk: TuskDrift,
        query: QueryType,
        query_str: str,
        params: Any,
        is_pre_app_start: bool,
    ) -> Any:
        """Handle RECORD mode for execute - create span and execute query."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg2.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg2.query",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg2",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "Psycopg2Instrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            return original_execute(query, params)

        error = None

        with SpanUtils.with_span(span_info):
            try:
                result = original_execute(query, params)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                self._finalize_query_span(
                    span_info.span,
                    cursor,
                    query,
                    params,
                    error,
                )
                span_info.span.end()

    def _traced_executemany(
        self,
        cursor: Any,
        original_executemany: Any,
        sdk: TuskDrift,
        query: QueryType,
        params_list: Any,
    ) -> Any:
        """Traced cursor.executemany method."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_executemany(query, params_list)

        query_str = _query_to_str(query)
        # Convert to list BEFORE executing to avoid iterator exhaustion
        params_as_list = list(params_list)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_executemany(cursor, sdk, query_str, params_as_list),
                no_op_request_handler=lambda: self._noop_execute(cursor),
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_executemany(query, params_as_list),
            record_mode_handler=lambda is_pre_app_start: self._record_executemany(
                cursor, original_executemany, sdk, query, query_str, params_as_list, is_pre_app_start
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _replay_executemany(self, cursor: Any, sdk: TuskDrift, query_str: str, params_list: list) -> None:
        """Handle REPLAY mode for executemany - fetch mock from CLI."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
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
                if is_pre_app_start:
                    logger.warning(
                        "[PSYCOPG2_REPLAY] No mock found for pre-app-start executemany query, returning empty result"
                    )
                    empty_mock = {"rowcount": 0, "rows": [], "description": None}
                    self._mock_execute_with_data(cursor, empty_mock)
                    span_info.span.end()
                    return None

                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg2 executemany query. "
                    f"This query was not recorded during the trace capture. "
                    f"Query: {query_str[:100]}..."
                )

            self._mock_execute_with_data(cursor, mock_result)
            span_info.span.end()
            return None

    def _record_executemany(
        self,
        cursor: Any,
        original_executemany: Any,
        sdk: TuskDrift,
        query: QueryType,
        query_str: str,
        params_list: list,
        is_pre_app_start: bool,
    ) -> Any:
        """Handle RECORD mode for executemany - create span and execute query."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg2.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg2.query",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg2",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "Psycopg2Instrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            return original_executemany(query, params_list)

        error = None

        with SpanUtils.with_span(span_info):
            try:
                result = original_executemany(query, params_list)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                self._finalize_query_span(
                    span_info.span,
                    cursor,
                    query,
                    {"_batch": params_list},
                    error,
                )
                span_info.span.end()

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        query: QueryType,
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
            query_str = _query_to_str(query)
            input_value = {
                "query": query_str.strip(),
            }
            if params is not None:
                input_value["parameters"] = params

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name="psycopg2.query",
                package_name="psycopg2",
                package_type=PackageType.PG,
                instrumentation_name="Psycopg2Instrumentation",
                submodule_name="query",
                input_value=input_value,
                kind=SpanKind.CLIENT,
                is_pre_app_start=not sdk.app_ready,
            )

            if not mock_response_output or not mock_response_output.found:
                logger.debug(f"No mock found for psycopg2 query: {query_str[:100]}")
                return None

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

        # Check if this is a dict-cursor (like RealDictCursor) by checking if cursor class
        # inherits from a dict-returning cursor type
        is_dict_cursor = False
        try:
            import psycopg2.extras

            is_dict_cursor = isinstance(cursor, (psycopg2.extras.RealDictCursor, psycopg2.extras.DictCursor))
        except (ImportError, AttributeError):
            pass

        # If it's a dict cursor and we have description, convert rows to dicts
        if is_dict_cursor and description_data:
            column_names = [col["name"] for col in description_data]
            converted_rows = []
            for row in mock_rows:
                if len(column_names) != len(row):
                    raise ValueError(
                        f"Column count mismatch: {len(column_names)} columns but row has {len(row)} values"
                    )
                converted_rows.append(dict(zip(column_names, row)))
            mock_rows = converted_rows

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
                # Return as-is for dict cursors, convert to tuple for regular cursors
                if isinstance(row, dict):
                    return row
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
            # Return as-is for dict rows, convert lists to tuples for regular cursors
            result = []
            for row in rows:
                if isinstance(row, dict):
                    result.append(row)
                elif isinstance(row, list):
                    result.append(tuple(row))
                else:
                    result.append(row)
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
                            # Convert rows to lists for JSON serialization
                            # Handle both tuple rows (regular cursor) and dict rows (RealDictCursor)
                            rows = []
                            for row in all_rows:
                                if isinstance(row, dict):
                                    # RealDictCursor returns dict-like rows - extract values in column order
                                    rows.append([row[desc[0]] for desc in cursor.description])
                                else:
                                    # Regular cursor returns tuples
                                    rows.append(list(row))

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
