"""Wrapper classes for psycopg3 instrumentation.

These wrappers intercept operations to capture data for recording.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class TracedCopyWrapper:
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
        # Convert memoryview to bytes to avoid mutation if buffer is reused
        if isinstance(buffer, memoryview):
            buffer = bytes(buffer)
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
