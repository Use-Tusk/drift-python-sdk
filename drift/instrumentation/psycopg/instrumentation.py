from __future__ import annotations

import json
import logging
import weakref
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Iterator

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

    def __init__(self, sdk: TuskDrift, instrumentation: PsycopgInstrumentation, cursor_factory, row_factory=None):
        self.sdk = sdk
        self.instrumentation = instrumentation
        self.cursor_factory = cursor_factory
        self.row_factory = row_factory  # Store row_factory for cursor creation
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

        def mock_copy(query, params=None, **kwargs):
            # For mock cursor, original_copy is a no-op context manager
            @contextmanager
            def noop_copy(q, p=None, **kw):
                yield MockCopy([])

            return instrumentation._traced_copy(cursor, noop_copy, sdk, query, params, **kwargs)

        # Monkey-patch mock functions onto cursor
        cursor.execute = mock_execute  # type: ignore[method-assign]
        cursor.executemany = mock_executemany  # type: ignore[method-assign]
        cursor.stream = mock_stream  # type: ignore[method-assign]
        cursor.copy = mock_copy  # type: ignore[method-assign]

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

    def pipeline(self):
        """Return a mock pipeline context manager for REPLAY mode."""
        return MockPipeline(self)


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
        # Support for multiple result sets (executemany with returning=True)
        self._mock_result_sets = []
        self._mock_result_set_index = 0
        self.adapters = MockAdapters()  # Django needs this
        logger.debug("[MOCK_CURSOR] Created fallback mock cursor (psycopg3)")

    @property
    def description(self):
        return self._tusk_description

    @property
    def rownumber(self):
        """Return the index of the next row to fetch, or None if no result."""
        if self._mock_rows:
            return self._mock_index
        return None

    @property
    def statusmessage(self):
        """Return the mock status message if set, otherwise None."""
        return getattr(self, '_mock_statusmessage', None)

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

    def results(self):
        """Iterate over result sets for executemany with returning=True.

        This method is patched by _mock_executemany_returning_with_data
        when replaying executemany with returning=True.
        Default implementation yields self once for backward compatibility.
        """
        yield self

    def nextset(self):
        """Move to the next result set.

        Returns True if there is another result set, None otherwise.
        This method is patched during replay for executemany with returning=True.
        """
        return None

    def stream(self, query, params=None, **kwargs):
        """Will be replaced by instrumentation."""
        return iter([])

    def __iter__(self):
        """Support direct cursor iteration (for row in cursor)."""
        return self

    def __next__(self):
        """Return next row for iteration."""
        if self._mock_index < len(self._mock_rows):
            row = self._mock_rows[self._mock_index]
            self._mock_index += 1
            return tuple(row) if isinstance(row, list) else row
        raise StopIteration

    def scroll(self, value: int, mode: str = "relative") -> None:
        """Scroll the cursor to a new position in the result set."""
        if mode == "relative":
            newpos = self._mock_index + value
        elif mode == "absolute":
            newpos = value
        else:
            raise ValueError(f"bad mode: {mode}. It should be 'relative' or 'absolute'")

        num_rows = len(self._mock_rows)
        if num_rows > 0:
            if not (0 <= newpos < num_rows):
                raise IndexError("position out of bound")
        elif newpos != 0:
            raise IndexError("position out of bound")

        self._mock_index = newpos

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class MockCopy:
    """Mock Copy object for REPLAY mode.

    Provides a minimal interface compatible with psycopg's Copy object
    for COPY TO operations (iteration) and COPY FROM operations (write).
    """

    def __init__(self, data: list):
        """Initialize MockCopy with recorded data.

        Args:
            data: For COPY TO - list of data chunks (as strings from JSON, will be encoded to bytes)
        """
        self._data = data
        self._index = 0

    def __iter__(self) -> Iterator[bytes]:
        """Iterate over COPY TO data chunks."""
        for item in self._data:
            # Data was stored as string in JSON, convert back to bytes
            if isinstance(item, str):
                yield item.encode("utf-8")
            elif isinstance(item, bytes):
                yield item
            else:
                yield str(item).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self) -> bytes:
        """Read next data chunk for COPY TO."""
        if self._index < len(self._data):
            item = self._data[self._index]
            self._index += 1
            if isinstance(item, str):
                return item.encode("utf-8")
            elif isinstance(item, bytes):
                return item
            return str(item).encode("utf-8")
        return b""

    def rows(self) -> Iterator[tuple]:
        """Iterate over rows for COPY TO (parsed format)."""
        for item in self._data:
            yield tuple(item) if isinstance(item, list) else item

    def write(self, buffer) -> None:
        """No-op for COPY FROM in replay mode."""
        pass

    def write_row(self, row) -> None:
        """No-op for COPY FROM in replay mode."""
        pass

    def set_types(self, types) -> None:
        """No-op for replay mode."""
        pass


class MockPipeline:
    """Mock Pipeline for REPLAY mode.

    In REPLAY mode, pipeline operations are no-ops since queries
    return mocked data immediately.
    """

    def __init__(self, connection: "MockConnection"):
        self._conn = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def sync(self):
        """No-op sync for mock pipeline."""
        pass


class _TracedCopyWrapper:
    """Wrapper around psycopg's Copy object to capture data in RECORD mode.

    Intercepts all data operations to record them for replay.
    """

    def __init__(self, copy: Any, data_collected: list):
        """Initialize wrapper.

        Args:
            copy: The real psycopg Copy object
            data_collected: List to append captured data chunks to
        """
        self._copy = copy
        self._data_collected = data_collected

    def __iter__(self) -> Iterator[bytes]:
        """Iterate over COPY TO data, capturing each chunk."""
        for data in self._copy:
            # Handle both bytes and memoryview
            if isinstance(data, memoryview):
                data = bytes(data)
            self._data_collected.append(data)
            yield data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self) -> bytes:
        """Read raw data from COPY TO, capturing it."""
        data = self._copy.read()
        if data:
            if isinstance(data, memoryview):
                data = bytes(data)
            self._data_collected.append(data)
        return data

    def read_row(self):
        """Read a parsed row from COPY TO."""
        row = self._copy.read_row()
        if row is not None:
            self._data_collected.append(row)
        return row

    def rows(self):
        """Iterate over parsed rows from COPY TO."""
        for row in self._copy.rows():
            self._data_collected.append(row)
            yield row

    def write(self, buffer):
        """Write raw data for COPY FROM."""
        self._data_collected.append(buffer)
        return self._copy.write(buffer)

    def write_row(self, row):
        """Write a row for COPY FROM."""
        self._data_collected.append(row)
        return self._copy.write_row(row)

    def set_types(self, types):
        """Proxy set_types to the underlying Copy object."""
        return self._copy.set_types(types)

    def __getattr__(self, name):
        """Proxy any other attributes to the underlying copy object."""
        return getattr(self._copy, name)


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
        # Track pending pipeline spans per connection for deferred finalization
        self._pending_pipeline_spans: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
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
            user_row_factory = kwargs.pop("row_factory", None)
            cursor_factory = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # Create server cursor factory for named cursors (conn.cursor(name="..."))
            server_cursor_factory = instrumentation._create_server_cursor_factory(sdk)

            # In REPLAY mode, try to connect but fall back to mock connection if DB is unavailable
            if sdk.mode == TuskDriftMode.REPLAY:
                try:
                    kwargs["cursor_factory"] = cursor_factory
                    if user_row_factory is not None:
                        kwargs["row_factory"] = user_row_factory
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
                    return MockConnection(sdk, instrumentation, cursor_factory, row_factory=user_row_factory)

            # In RECORD mode, always require real connection
            kwargs["cursor_factory"] = cursor_factory
            if user_row_factory is not None:
                kwargs["row_factory"] = user_row_factory
            connection = original_connect(*args, **kwargs)
            # Set server cursor factory on the connection for named cursors
            if server_cursor_factory:
                connection.server_cursor_factory = server_cursor_factory
            logger.debug("[PATCHED_CONNECT] RECORD mode: Connected to database (psycopg3)")
            return connection

        module.connect = patched_connect  # type: ignore[attr-defined]
        logger.debug("psycopg.connect instrumented")

        # Patch Pipeline class for pipeline mode support
        self._patch_pipeline_class(module)

    def _patch_pipeline_class(self, module: ModuleType) -> None:
        """Patch psycopg.Pipeline to finalize spans on sync/exit."""
        try:
            from psycopg import Pipeline
        except ImportError:
            logger.debug("psycopg.Pipeline not available, skipping pipeline instrumentation")
            return

        instrumentation = self

        # Store originals for potential unpatch
        self._original_pipeline_sync = getattr(Pipeline, 'sync', None)
        self._original_pipeline_exit = getattr(Pipeline, '__exit__', None)

        if self._original_pipeline_sync:
            def patched_sync(pipeline_self):
                """Patched Pipeline.sync that finalizes pending spans."""
                result = instrumentation._original_pipeline_sync(pipeline_self)
                # _conn is the connection associated with the pipeline
                conn = getattr(pipeline_self, '_conn', None)
                if conn:
                    instrumentation._finalize_pending_pipeline_spans(conn)
                return result

            Pipeline.sync = patched_sync
            logger.debug("psycopg.Pipeline.sync instrumented")

        if self._original_pipeline_exit:
            def patched_exit(pipeline_self, exc_type, exc_val, exc_tb):
                """Patched Pipeline.__exit__ that finalizes any remaining spans."""
                result = instrumentation._original_pipeline_exit(
                    pipeline_self, exc_type, exc_val, exc_tb
                )
                # Finalize any remaining pending spans (handles implicit sync on exit)
                conn = getattr(pipeline_self, '_conn', None)
                if conn:
                    instrumentation._finalize_pending_pipeline_spans(conn)
                return result

            Pipeline.__exit__ = patched_exit
            logger.debug("psycopg.Pipeline.__exit__ instrumented")

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

            @property
            def rownumber(self):
                # In captured mode (after fetchall in _finalize_query_span), return tracked index
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    return self._tusk_index
                # In replay mode with mock data, return mock index
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    return self._mock_index
                # Otherwise, return real cursor's rownumber
                return super().rownumber

            @property
            def statusmessage(self):
                # In replay mode with mock data, return mock statusmessage
                if hasattr(self, '_mock_statusmessage'):
                    return self._mock_statusmessage
                # Otherwise, return real cursor's statusmessage
                return super().statusmessage

            def execute(self, query, params=None, **kwargs):
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)

            def executemany(self, query, params_seq, **kwargs):
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, params_seq, **kwargs)

            def stream(self, query, params=None, **kwargs):
                return instrumentation._traced_stream(self, super().stream, sdk, query, params, **kwargs)

            def copy(self, query, params=None, **kwargs):
                return instrumentation._traced_copy(self, super().copy, sdk, query, params, **kwargs)

            def __iter__(self):
                # Support direct cursor iteration (for row in cursor)
                # In replay mode with mock data (_mock_rows) or record mode with captured data (_tusk_rows)
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    return self
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    return self
                return super().__iter__()

            def __next__(self):
                # In replay mode with mock data, iterate over mock rows
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    if self._mock_index < len(self._mock_rows):
                        row = self._mock_rows[self._mock_index]
                        self._mock_index += 1
                        # Apply row transformation if fetchone is patched
                        if hasattr(self, 'fetchone') and callable(self.fetchone):
                            # Reset index, get transformed row, restore index
                            self._mock_index -= 1
                            result = self.fetchone()
                            return result
                        return tuple(row) if isinstance(row, list) else row
                    raise StopIteration
                # In record mode with captured data, iterate over stored rows
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    if self._tusk_index < len(self._tusk_rows):
                        row = self._tusk_rows[self._tusk_index]
                        self._tusk_index += 1
                        return row
                    raise StopIteration
                return super().__next__()

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

            @property
            def rownumber(self):
                # In captured mode (after fetchall in _finalize_query_span), return tracked index
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    return self._tusk_index
                # In replay mode with mock data, return mock index
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    return self._mock_index
                # Otherwise, return real cursor's rownumber
                return super().rownumber

            @property
            def statusmessage(self):
                # In replay mode with mock data, return mock statusmessage
                if hasattr(self, '_mock_statusmessage'):
                    return self._mock_statusmessage
                # Otherwise, return real cursor's statusmessage
                return super().statusmessage

            def execute(self, query, params=None, **kwargs):
                # Note: ServerCursor.execute() doesn't support 'prepare' parameter
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)

            # Note: ServerCursor doesn't support executemany()
            # Note: ServerCursor has stream-like iteration via fetchmany/itersize

            def __iter__(self):
                # Support direct cursor iteration (for row in cursor)
                # In replay mode with mock data (_mock_rows) or record mode with captured data (_tusk_rows)
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    return self
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    return self
                return super().__iter__()

            def __next__(self):
                # In replay mode with mock data, iterate over mock rows
                if hasattr(self, '_mock_rows') and self._mock_rows is not None:
                    if self._mock_index < len(self._mock_rows):
                        row = self._mock_rows[self._mock_index]
                        self._mock_index += 1
                        # Apply row transformation if fetchone is patched
                        if hasattr(self, 'fetchone') and callable(self.fetchone):
                            # Reset index, get transformed row, restore index
                            self._mock_index -= 1
                            result = self.fetchone()
                            return result
                        return tuple(row) if isinstance(row, list) else row
                    raise StopIteration
                # In record mode with captured data, iterate over stored rows
                if hasattr(self, '_tusk_rows') and self._tusk_rows is not None:
                    if self._tusk_index < len(self._tusk_rows):
                        row = self._tusk_rows[self._tusk_index]
                        self._tusk_index += 1
                        return row
                    raise StopIteration
                return super().__next__()

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
        # Reset cursor state from any previous execute() on this cursor.
        # This ensures fetch methods work correctly for multiple queries on the same cursor.
        # We must restore original fetch methods so _finalize_query_span can call the real
        # psycopg fetchall() method, not our patched version from a previous query.
        if hasattr(cursor, '_tusk_original_fetchone'):
            cursor.fetchone = cursor._tusk_original_fetchone
            cursor.fetchmany = cursor._tusk_original_fetchmany
            cursor.fetchall = cursor._tusk_original_fetchall
            cursor._tusk_rows = None
            cursor._tusk_index = 0

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

        # Check if we're in pipeline mode BEFORE executing
        in_pipeline_mode = self._is_in_pipeline_mode(cursor)

        with SpanUtils.with_span(span_info):
            try:
                result = original_execute(query, params, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if error is not None:
                    # Always finalize immediately on error
                    self._finalize_query_span(span_info.span, cursor, query_str, params, error)
                    span_info.span.end()
                elif in_pipeline_mode:
                    # Defer finalization until pipeline.sync()
                    connection = self._get_connection_from_cursor(cursor)
                    if connection:
                        self._add_pending_pipeline_span(connection, span_info, cursor, query_str, params)
                        # DON'T end span here - will be ended in _finalize_pending_pipeline_spans
                    else:
                        # Fallback: finalize immediately if we can't get connection
                        self._finalize_query_span(span_info.span, cursor, query_str, params, None)
                        span_info.span.end()
                else:
                    # Normal mode: finalize immediately
                    self._finalize_query_span(span_info.span, cursor, query_str, params, None)
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
        # Detect returning flag for executemany with RETURNING clause
        returning = kwargs.get("returning", False)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_executemany(
                    cursor, sdk, query_str, params_list, returning
                ),
                no_op_request_handler=lambda: self._noop_execute(cursor),
                is_server_request=False,
            )

        # RECORD mode
        return handle_record_mode(
            original_function_call=lambda: original_executemany(query, params_list, **kwargs),
            record_mode_handler=lambda is_pre_app_start: self._record_executemany(
                cursor, original_executemany, sdk, query, query_str, params_list, is_pre_app_start, kwargs, returning
            ),
            span_kind=OTelSpanKind.CLIENT,
        )

    def _replay_executemany(
        self, cursor: Any, sdk: TuskDrift, query_str: str, params_list: list, returning: bool = False
    ) -> Any:
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
            # Include returning flag in parameters for mock matching
            params_for_mock = {"_batch": params_list}
            if returning:
                params_for_mock["_returning"] = True

            mock_result = self._try_get_mock(
                sdk, query_str, params_for_mock, span_info.trace_id, span_info.span_id
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

            # Check if this is executemany_returning format (multiple result sets)
            if mock_result.get("executemany_returning"):
                self._mock_executemany_returning_with_data(cursor, mock_result)
            else:
                # Backward compatible: use existing single result set handling
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
        returning: bool = False,
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
                if returning and error is None:
                    # Use specialized method for executemany with returning=True
                    self._finalize_executemany_returning_span(
                        span_info.span,
                        cursor,
                        query_str,
                        {"_batch": params_list, "_returning": True},
                        error,
                    )
                else:
                    # Existing behavior for executemany without returning
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

    def _traced_copy(
        self, cursor: Any, original_copy: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.copy method - returns a context manager."""
        if sdk.mode == TuskDriftMode.DISABLED:
            return original_copy(query, params, **kwargs)

        query_str = self._query_to_string(query, cursor)

        if sdk.mode == TuskDriftMode.REPLAY:
            return handle_replay_mode(
                replay_mode_handler=lambda: self._replay_copy(cursor, sdk, query_str),
                no_op_request_handler=lambda: self._noop_copy(),
                is_server_request=False,
            )

        # RECORD mode - return a context manager that wraps the copy operation
        return self._record_copy(cursor, original_copy, sdk, query, query_str, params, kwargs)

    @contextmanager
    def _noop_copy(self) -> Iterator[MockCopy]:
        """Handle background requests in REPLAY mode - return empty mock."""
        yield MockCopy(data=[])

    @contextmanager
    def _record_copy(
        self,
        cursor: Any,
        original_copy: Any,
        sdk: TuskDrift,
        query: str,
        query_str: str,
        params: Any,
        kwargs: dict,
    ) -> Iterator[_TracedCopyWrapper]:
        """Handle RECORD mode for copy - wrap Copy object with tracing."""
        is_pre_app_start = not sdk.app_ready

        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg.copy",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg.copy",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "copy",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

        if not span_info:
            # Fallback to original if span creation fails
            with original_copy(query, params, **kwargs) as copy:
                yield copy
            return

        error = None
        data_collected: list = []

        try:
            with SpanUtils.with_span(span_info):
                with original_copy(query, params, **kwargs) as copy:
                    # Wrap the Copy object to capture data
                    wrapped_copy = _TracedCopyWrapper(copy, data_collected)
                    yield wrapped_copy
        except Exception as e:
            error = e
            raise
        finally:
            self._finalize_copy_span(
                span_info.span,
                query_str,
                data_collected,
                error,
            )
            span_info.span.end()

    @contextmanager
    def _replay_copy(self, cursor: Any, sdk: TuskDrift, query_str: str) -> Iterator[MockCopy]:
        """Handle REPLAY mode for copy - return mock Copy object."""
        span_info = SpanUtils.create_span(
            CreateSpanOptions(
                name="psycopg.copy",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "psycopg.copy",
                    TdSpanAttributes.PACKAGE_NAME: "psycopg",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "PsycopgInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "copy",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: not sdk.app_ready,
                },
                is_pre_app_start=not sdk.app_ready,
            )
        )

        if not span_info:
            raise RuntimeError("Error creating span in replay mode")

        with SpanUtils.with_span(span_info):
            mock_result = self._try_get_copy_mock(
                sdk, query_str, span_info.trace_id, span_info.span_id
            )

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for psycopg copy operation. "
                    f"This {'pre-app-start ' if is_pre_app_start else ''}copy was not recorded. "
                    f"Query: {query_str[:100]}..."
                )

            # Yield a mock copy object with recorded data
            mock_copy = MockCopy(mock_result.get("data", []))
            yield mock_copy

            span_info.span.end()

    def _try_get_copy_mock(
        self,
        sdk: TuskDrift,
        query: str,
        trace_id: str,
        span_id: str,
    ) -> dict[str, Any] | None:
        """Try to get a mocked response for copy operation from CLI."""
        try:
            # Determine operation type from query
            query_upper = query.upper()
            is_copy_to = "TO" in query_upper and "STDOUT" in query_upper
            is_copy_from = "FROM" in query_upper and "STDIN" in query_upper

            input_value = {
                "query": query.strip(),
                "operation": "COPY_TO" if is_copy_to else "COPY_FROM" if is_copy_from else "COPY",
            }

            # Use centralized mock finding utility
            from ...core.mock_utils import find_mock_response_sync

            mock_response_output = find_mock_response_sync(
                sdk=sdk,
                trace_id=trace_id,
                span_id=span_id,
                name="psycopg.copy",
                package_name="psycopg",
                package_type=PackageType.PG,
                instrumentation_name="PsycopgInstrumentation",
                submodule_name="copy",
                input_value=input_value,
                kind=SpanKind.CLIENT,
                is_pre_app_start=not sdk.app_ready,
            )

            if not mock_response_output or not mock_response_output.found:
                logger.debug(f"No mock found for psycopg copy: {query[:100]}")
                return None

            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg copy: {e}")
            return None

    def _finalize_copy_span(
        self,
        span: trace.Span,
        query: str,
        data_collected: list,
        error: Exception | None,
    ) -> None:
        """Finalize span for copy operation."""
        try:
            import datetime

            def serialize_value(val):
                """Convert non-JSON-serializable values to JSON-compatible types."""
                if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
                    return val.isoformat()
                elif isinstance(val, bytes):
                    return val.decode("utf-8", errors="replace")
                elif isinstance(val, memoryview):
                    return bytes(val).decode("utf-8", errors="replace")
                elif isinstance(val, (list, tuple)):
                    return [serialize_value(v) for v in val]
                elif isinstance(val, dict):
                    return {k: serialize_value(v) for k, v in val.items()}
                return val

            # Determine operation type from query
            query_upper = query.upper()
            is_copy_to = "TO" in query_upper and "STDOUT" in query_upper
            is_copy_from = "FROM" in query_upper and "STDIN" in query_upper

            # Build input value
            input_value = {
                "query": query.strip(),
                "operation": "COPY_TO" if is_copy_to else "COPY_FROM" if is_copy_from else "COPY",
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
                # Serialize the captured data
                serialized_data = [serialize_value(d) for d in data_collected]
                output_value = {
                    "data": serialized_data,
                    "chunk_count": len(data_collected),
                }

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

            logger.debug("[PSYCOPG] Copy span finalized successfully")

        except Exception as e:
            logger.error(f"Error finalizing copy span: {e}")

    def _query_to_string(self, query: Any, cursor: Any) -> str:
        """Convert query to string."""
        try:
            from psycopg.sql import Composed

            if isinstance(query, Composed):
                return query.as_string(cursor)
        except ImportError:
            pass

        return str(query) if not isinstance(query, str) else query

    def _detect_row_factory_type(self, row_factory: Any) -> str:
        """Detect the type of row factory for mock transformations.

        Returns:
            "dict" for dict_row, "namedtuple" for namedtuple_row, "tuple" otherwise
        """
        if row_factory is None:
            return "tuple"

        # Check by function/class name
        factory_name = getattr(row_factory, '__name__', '')
        if not factory_name:
            factory_name = str(type(row_factory).__name__)

        factory_name_lower = factory_name.lower()
        if 'dict' in factory_name_lower:
            return "dict"
        elif 'namedtuple' in factory_name_lower:
            return "namedtuple"

        return "tuple"

    def _is_in_pipeline_mode(self, cursor: Any) -> bool:
        """Check if the cursor's connection is currently in pipeline mode.

        In psycopg3, when conn.pipeline() is active, connection._pipeline is set.
        """
        try:
            conn = getattr(cursor, 'connection', None)
            if conn is None:
                return False
            # MockConnection doesn't have real pipeline mode
            if isinstance(conn, MockConnection):
                return False
            pipeline = getattr(conn, '_pipeline', None)
            return pipeline is not None
        except Exception:
            return False

    def _get_connection_from_cursor(self, cursor: Any) -> Any:
        """Get the connection object from a cursor."""
        return getattr(cursor, 'connection', None)

    def _add_pending_pipeline_span(
        self,
        connection: Any,
        span_info: Any,
        cursor: Any,
        query: str,
        params: Any,
    ) -> None:
        """Add a pending span to be finalized when pipeline syncs."""
        if connection not in self._pending_pipeline_spans:
            self._pending_pipeline_spans[connection] = []

        self._pending_pipeline_spans[connection].append({
            'span_info': span_info,
            'cursor': cursor,
            'query': query,
            'params': params,
        })
        logger.debug(f"[PIPELINE] Deferred span for query: {query[:50]}...")

    def _finalize_pending_pipeline_spans(self, connection: Any) -> None:
        """Finalize all pending spans for a connection after pipeline sync."""
        if connection not in self._pending_pipeline_spans:
            return

        pending = self._pending_pipeline_spans.pop(connection, [])
        logger.debug(f"[PIPELINE] Finalizing {len(pending)} pending pipeline spans")

        for item in pending:
            span_info = item['span_info']
            cursor = item['cursor']
            query = item['query']
            params = item['params']

            try:
                self._finalize_query_span(span_info.span, cursor, query, params, error=None)
                span_info.span.end()
            except Exception as e:
                logger.error(f"[PIPELINE] Error finalizing deferred span: {e}")
                try:
                    span_info.span.end()
                except Exception:
                    pass

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

        # Set mock statusmessage for replay
        statusmessage = actual_data.get("statusmessage")
        if statusmessage is not None:
            cursor._mock_statusmessage = statusmessage

        # Get row_factory from cursor or connection for row transformation
        row_factory = getattr(cursor, 'row_factory', None)
        if row_factory is None:
            conn = getattr(cursor, 'connection', None)
            if conn:
                row_factory = getattr(conn, 'row_factory', None)

        # Extract column names from description for row factory transformations
        column_names = None
        if description_data:
            column_names = [col["name"] for col in description_data]

        # Detect row factory type for transformation
        row_factory_type = self._detect_row_factory_type(row_factory)

        # Create namedtuple class once if needed (avoid recreating for each row)
        RowClass = None
        if row_factory_type == "namedtuple" and column_names:
            from collections import namedtuple
            RowClass = namedtuple('Row', column_names)

        def transform_row(row):
            """Transform raw row data according to row factory type."""
            values = tuple(row) if isinstance(row, list) else row
            if row_factory_type == "dict" and column_names:
                return dict(zip(column_names, values))
            elif row_factory_type == "namedtuple" and RowClass is not None:
                return RowClass(*values)
            return values

        mock_rows = actual_data.get("rows", [])
        # Deserialize datetime strings back to datetime objects for consistent Flask serialization
        mock_rows = [deserialize_db_value(row) for row in mock_rows]
        cursor._mock_rows = mock_rows  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        def mock_fetchone():
            if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                return transform_row(row)
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
            return [transform_row(row) for row in rows]

        cursor.fetchone = mock_fetchone  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchmany = mock_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchall = mock_fetchall  # pyright: ignore[reportAttributeAccessIssue]

        def mock_scroll(value: int, mode: str = "relative") -> None:
            """Scroll the cursor to a new position in the mock result set."""
            if mode == "relative":
                newpos = cursor._mock_index + value  # pyright: ignore[reportAttributeAccessIssue]
            elif mode == "absolute":
                newpos = value
            else:
                raise ValueError(f"bad mode: {mode}. It should be 'relative' or 'absolute'")

            num_rows = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
            if num_rows > 0:
                if not (0 <= newpos < num_rows):
                    raise IndexError("position out of bound")
            elif newpos != 0:
                raise IndexError("position out of bound")

            cursor._mock_index = newpos  # pyright: ignore[reportAttributeAccessIssue]

        cursor.scroll = mock_scroll  # pyright: ignore[reportAttributeAccessIssue]

        # Note: __iter__ and __next__ are handled at the class level in InstrumentedCursor
        # and MockCursor classes, as Python looks up special methods on the type, not instance

    def _mock_executemany_returning_with_data(self, cursor: Any, mock_data: dict[str, Any]) -> None:
        """Mock cursor for executemany with returning=True - supports multiple result sets.

        This method sets up the cursor to replay multiple result sets captured during
        executemany with returning=True. It patches the cursor's results() method to
        yield the cursor for each result set, allowing iteration.
        """
        result_sets = mock_data.get("result_sets", [])

        if not result_sets:
            # Fallback to empty result
            cursor._mock_rows = []  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]
            return

        # Get row_factory from cursor or connection
        row_factory = getattr(cursor, "row_factory", None)
        if row_factory is None:
            conn = getattr(cursor, "connection", None)
            if conn:
                row_factory = getattr(conn, "row_factory", None)

        row_factory_type = self._detect_row_factory_type(row_factory)

        # Store all result sets for iteration
        cursor._mock_result_sets = []  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_result_set_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        for result_set in result_sets:
            description_data = result_set.get("description")
            column_names = None
            if description_data:
                column_names = [col["name"] for col in description_data]

            # Deserialize rows
            mock_rows = result_set.get("rows", [])
            mock_rows = [deserialize_db_value(row) for row in mock_rows]

            cursor._mock_result_sets.append(  # pyright: ignore[reportAttributeAccessIssue]
                {
                    "description": description_data,
                    "column_names": column_names,
                    "rows": mock_rows,
                    "rowcount": result_set.get("rowcount", -1),
                }
            )

        # Create row transformation helper
        def create_row_class(col_names):
            if row_factory_type == "namedtuple" and col_names:
                from collections import namedtuple

                return namedtuple("Row", col_names)
            return None

        def transform_row(row, col_names, RowClass):
            """Transform raw row data according to row factory type."""
            values = tuple(row) if isinstance(row, list) else row
            if row_factory_type == "dict" and col_names:
                return dict(zip(col_names, values))
            elif row_factory_type == "namedtuple" and RowClass is not None:
                return RowClass(*values)
            return values

        def mock_results():
            """Generator that yields cursor for each result set."""
            while cursor._mock_result_set_index < len(cursor._mock_result_sets):  # pyright: ignore[reportAttributeAccessIssue]
                result_set = cursor._mock_result_sets[cursor._mock_result_set_index]  # pyright: ignore[reportAttributeAccessIssue]

                # Set up cursor state for this result set
                cursor._mock_rows = result_set["rows"]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                # Set description
                description_data = result_set.get("description")
                if description_data:
                    desc = [
                        (col["name"], col.get("type_code"), None, None, None, None, None)
                        for col in description_data
                    ]
                    try:
                        cursor._tusk_description = desc  # pyright: ignore[reportAttributeAccessIssue]
                    except AttributeError:
                        try:
                            cursor.description = desc  # pyright: ignore[reportAttributeAccessIssue]
                        except AttributeError:
                            pass

                # Set rowcount
                try:
                    cursor._rowcount = result_set.get("rowcount", -1)  # pyright: ignore[reportAttributeAccessIssue]
                except AttributeError:
                    pass

                column_names = result_set.get("column_names")
                RowClass = create_row_class(column_names)

                # Create fetch methods for this result set with closures capturing current values
                def make_fetchone(cn, RC):
                    def fetchone():
                        if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                            row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                            return transform_row(row, cn, RC)
                        return None

                    return fetchone

                def make_fetchmany(cn, RC):
                    def fetchmany(size=cursor.arraysize):
                        rows = []
                        for _ in range(size):
                            if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                                row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                                rows.append(transform_row(row, cn, RC))
                            else:
                                break
                        return rows

                    return fetchmany

                def make_fetchall(cn, RC):
                    def fetchall():
                        rows = cursor._mock_rows[cursor._mock_index :]  # pyright: ignore[reportAttributeAccessIssue]
                        cursor._mock_index = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
                        return [transform_row(row, cn, RC) for row in rows]

                    return fetchall

                cursor.fetchone = make_fetchone(column_names, RowClass)  # pyright: ignore[reportAttributeAccessIssue]
                cursor.fetchmany = make_fetchmany(column_names, RowClass)  # pyright: ignore[reportAttributeAccessIssue]
                cursor.fetchall = make_fetchall(column_names, RowClass)  # pyright: ignore[reportAttributeAccessIssue]

                cursor._mock_result_set_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                yield cursor

        # Patch results() method onto cursor
        cursor.results = mock_results  # pyright: ignore[reportAttributeAccessIssue]

        # Also set up initial state for the first result set (in case user calls fetch without results())
        if cursor._mock_result_sets:  # pyright: ignore[reportAttributeAccessIssue]
            first_set = cursor._mock_result_sets[0]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_rows = first_set["rows"]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

            # Set description for first result set
            description_data = first_set.get("description")
            if description_data:
                desc = [
                    (col["name"], col.get("type_code"), None, None, None, None, None)
                    for col in description_data
                ]
                try:
                    cursor._tusk_description = desc  # pyright: ignore[reportAttributeAccessIssue]
                except AttributeError:
                    try:
                        cursor.description = desc  # pyright: ignore[reportAttributeAccessIssue]
                    except AttributeError:
                        pass

            # Set up initial fetch methods for the first result set (for code that uses nextset() instead of results())
            first_column_names = first_set.get("column_names")
            FirstRowClass = create_row_class(first_column_names)

            def make_fetchone_replay(cn, RC):
                def fetchone():
                    if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                        row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                        cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                        return transform_row(row, cn, RC)
                    return None
                return fetchone

            def make_fetchmany_replay(cn, RC):
                def fetchmany(size=cursor.arraysize):
                    rows = []
                    for _ in range(size):
                        if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                            row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                            rows.append(transform_row(row, cn, RC))
                        else:
                            break
                    return rows
                return fetchmany

            def make_fetchall_replay(cn, RC):
                def fetchall():
                    rows = cursor._mock_rows[cursor._mock_index:]  # pyright: ignore[reportAttributeAccessIssue]
                    cursor._mock_index = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
                    return [transform_row(row, cn, RC) for row in rows]
                return fetchall

            cursor.fetchone = make_fetchone_replay(first_column_names, FirstRowClass)  # pyright: ignore[reportAttributeAccessIssue]
            cursor.fetchmany = make_fetchmany_replay(first_column_names, FirstRowClass)  # pyright: ignore[reportAttributeAccessIssue]
            cursor.fetchall = make_fetchall_replay(first_column_names, FirstRowClass)  # pyright: ignore[reportAttributeAccessIssue]

            # Patch nextset() to work with _mock_result_sets
            def patched_nextset():
                """Move to the next result set in _mock_result_sets."""
                next_index = cursor._mock_result_set_index + 1  # pyright: ignore[reportAttributeAccessIssue]
                if next_index < len(cursor._mock_result_sets):  # pyright: ignore[reportAttributeAccessIssue]
                    cursor._mock_result_set_index = next_index  # pyright: ignore[reportAttributeAccessIssue]
                    next_set = cursor._mock_result_sets[next_index]  # pyright: ignore[reportAttributeAccessIssue]
                    cursor._mock_rows = next_set["rows"]  # pyright: ignore[reportAttributeAccessIssue]
                    cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                    # Update fetch methods for the new result set
                    next_column_names = next_set.get("column_names")
                    NextRowClass = create_row_class(next_column_names)
                    cursor.fetchone = make_fetchone_replay(next_column_names, NextRowClass)  # pyright: ignore[reportAttributeAccessIssue]
                    cursor.fetchmany = make_fetchmany_replay(next_column_names, NextRowClass)  # pyright: ignore[reportAttributeAccessIssue]
                    cursor.fetchall = make_fetchall_replay(next_column_names, NextRowClass)  # pyright: ignore[reportAttributeAccessIssue]

                    # Update description for next result set
                    next_description_data = next_set.get("description")
                    if next_description_data:
                        next_desc = [
                            (col["name"], col.get("type_code"), None, None, None, None, None)
                            for col in next_description_data
                        ]
                        try:
                            cursor._tusk_description = next_desc  # pyright: ignore[reportAttributeAccessIssue]
                        except AttributeError:
                            try:
                                cursor.description = next_desc  # pyright: ignore[reportAttributeAccessIssue]
                            except AttributeError:
                                pass

                    return True
                return None

            cursor.nextset = patched_nextset  # pyright: ignore[reportAttributeAccessIssue]

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
                            # Convert rows to lists for JSON serialization
                            # Handle dict_row (returns dicts) and namedtuple_row (returns namedtuples)
                            column_names = [d["name"] for d in description]
                            rows = []
                            for row in all_rows:
                                if isinstance(row, dict):
                                    # dict_row: extract values in column order
                                    rows.append([row.get(col) for col in column_names])
                                elif hasattr(row, '_fields'):
                                    # namedtuple: extract values in column order
                                    rows.append([getattr(row, col, None) for col in column_names])
                                else:
                                    # tuple or list: convert directly
                                    rows.append(list(row))

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

                            # Save original fetch methods before patching (only if not already saved)
                            # These will be restored at the start of the next execute() call
                            if not hasattr(cursor, '_tusk_original_fetchone'):
                                cursor._tusk_original_fetchone = cursor.fetchone  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_original_fetchmany = cursor.fetchmany  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_original_fetchall = cursor.fetchall  # pyright: ignore[reportAttributeAccessIssue]

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

                    # Capture statusmessage for replay
                    if hasattr(cursor, 'statusmessage') and cursor.statusmessage is not None:
                        output_value["statusmessage"] = cursor.statusmessage

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

    def _finalize_executemany_returning_span(
        self,
        span: trace.Span,
        cursor: Any,
        query: str,
        params: Any,
        error: Exception | None,
    ) -> None:
        """Finalize span for executemany with returning=True - captures multiple result sets.

        This method iterates through cursor.results() to capture all result sets
        from executemany with returning=True, storing them in a format that can
        be replayed with multiple result set iteration.
        """
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
                # Iterate through cursor.results() to capture all result sets
                result_sets = []
                all_rows_collected = []  # For re-populating cursor

                try:
                    # cursor.results() yields the cursor itself for each result set
                    for result_cursor in cursor.results():
                        result_set_data = {}

                        # Capture description for this result set
                        if hasattr(result_cursor, "description") and result_cursor.description:
                            description = [
                                {
                                    "name": desc[0] if hasattr(desc, "__getitem__") else desc.name,
                                    "type_code": desc[1]
                                    if hasattr(desc, "__getitem__") and len(desc) > 1
                                    else getattr(desc, "type_code", None),
                                }
                                for desc in result_cursor.description
                            ]
                            result_set_data["description"] = description
                            column_names = [d["name"] for d in description]
                        else:
                            description = None
                            column_names = None

                        # Fetch all rows for this result set
                        rows = []
                        raw_rows = result_cursor.fetchall()
                        all_rows_collected.append(raw_rows)

                        for row in raw_rows:
                            if isinstance(row, dict):
                                rows.append([row.get(col) for col in column_names] if column_names else list(row.values()))
                            elif hasattr(row, "_fields"):
                                rows.append(
                                    [getattr(row, col, None) for col in column_names] if column_names else list(row)
                                )
                            else:
                                rows.append(list(row))

                        result_set_data["rowcount"] = (
                            result_cursor.rowcount if hasattr(result_cursor, "rowcount") else len(rows)
                        )
                        result_set_data["rows"] = [[serialize_value(col) for col in row] for row in rows]

                        result_sets.append(result_set_data)

                except Exception as results_error:
                    logger.debug(f"Could not iterate results(): {results_error}")
                    # Fallback: treat as single result set
                    result_sets = []

                if result_sets:
                    output_value = {
                        "executemany_returning": True,
                        "result_sets": result_sets,
                    }

                    # Re-populate cursor for user code
                    # Store all collected rows for replay via results()
                    cursor._tusk_result_sets = all_rows_collected  # pyright: ignore[reportAttributeAccessIssue]
                    cursor._tusk_result_set_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                    # Patch results() method to iterate stored result sets
                    def patched_results():
                        while cursor._tusk_result_set_index < len(cursor._tusk_result_sets):  # pyright: ignore[reportAttributeAccessIssue]
                            rows = cursor._tusk_result_sets[cursor._tusk_result_set_index]  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_rows = rows  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_index = 0  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_result_set_index += 1  # pyright: ignore[reportAttributeAccessIssue]

                            # Patch fetch methods for this result set
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

                            yield cursor

                    cursor.results = patched_results  # pyright: ignore[reportAttributeAccessIssue]

                    # Set up the first result set immediately for user code that uses nextset() instead of results()
                    if all_rows_collected:
                        cursor._tusk_rows = all_rows_collected[0]  # pyright: ignore[reportAttributeAccessIssue]
                        cursor._tusk_index = 0  # pyright: ignore[reportAttributeAccessIssue]
                        cursor._tusk_result_set_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                        # Create initial fetch methods for the first result set
                        def make_patched_fetchone_record():
                            def patched_fetchone():
                                if cursor._tusk_index < len(cursor._tusk_rows):  # pyright: ignore[reportAttributeAccessIssue]
                                    row = cursor._tusk_rows[cursor._tusk_index]  # pyright: ignore[reportAttributeAccessIssue]
                                    cursor._tusk_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                                    return row
                                return None
                            return patched_fetchone

                        def make_patched_fetchmany_record():
                            def patched_fetchmany(size=cursor.arraysize):
                                result = cursor._tusk_rows[cursor._tusk_index : cursor._tusk_index + size]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_index += len(result)  # pyright: ignore[reportAttributeAccessIssue]
                                return result
                            return patched_fetchmany

                        def make_patched_fetchall_record():
                            def patched_fetchall():
                                result = cursor._tusk_rows[cursor._tusk_index :]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_index = len(cursor._tusk_rows)  # pyright: ignore[reportAttributeAccessIssue]
                                return result
                            return patched_fetchall

                        cursor.fetchone = make_patched_fetchone_record()  # pyright: ignore[reportAttributeAccessIssue]
                        cursor.fetchmany = make_patched_fetchmany_record()  # pyright: ignore[reportAttributeAccessIssue]
                        cursor.fetchall = make_patched_fetchall_record()  # pyright: ignore[reportAttributeAccessIssue]

                    # Patch nextset() to work with _tusk_result_sets
                    def patched_nextset():
                        """Move to the next result set in _tusk_result_sets."""
                        next_index = cursor._tusk_result_set_index + 1  # pyright: ignore[reportAttributeAccessIssue]
                        if next_index < len(cursor._tusk_result_sets):  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_result_set_index = next_index  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_rows = cursor._tusk_result_sets[next_index]  # pyright: ignore[reportAttributeAccessIssue]
                            cursor._tusk_index = 0  # pyright: ignore[reportAttributeAccessIssue]

                            # Update fetch methods for the new result set
                            cursor.fetchone = make_patched_fetchone_record()  # pyright: ignore[reportAttributeAccessIssue]
                            cursor.fetchmany = make_patched_fetchmany_record()  # pyright: ignore[reportAttributeAccessIssue]
                            cursor.fetchall = make_patched_fetchall_record()  # pyright: ignore[reportAttributeAccessIssue]
                            return True
                        return None

                    cursor.nextset = patched_nextset  # pyright: ignore[reportAttributeAccessIssue]

                else:
                    output_value = {"rowcount": cursor.rowcount if hasattr(cursor, "rowcount") else -1}

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

            logger.debug("[PSYCOPG] Executemany returning span finalized successfully")

        except Exception as e:
            logger.error(f"Error finalizing executemany returning span: {e}")
