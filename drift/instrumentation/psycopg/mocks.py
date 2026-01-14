"""Mock classes for psycopg3 REPLAY mode.

These mock classes provide a minimal interface for Django/Flask to work
without a real PostgreSQL database connection during replay.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from ...core.drift_sdk import TuskDrift
    from .instrumentation import PsycopgInstrumentation

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        sdk: TuskDrift,
        instrumentation: PsycopgInstrumentation,
        cursor_factory,
        row_factory=None,
    ):
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

    def __init__(self, connection: MockConnection):
        self._conn = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def sync(self):
        """No-op sync for mock pipeline."""
        pass
