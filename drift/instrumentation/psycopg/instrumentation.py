from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Dict, Optional
from types import ModuleType

from ..base import InstrumentationBase
from ...core.communication.types import MockRequestInput
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper
from ...core.types import (
    CleanSpanData,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    replay_trace_id_context,
    current_trace_id_context,
    current_span_id_context,
    Timestamp,
    Duration,
)

logger = logging.getLogger(__name__)

_instance: Optional["PsycopgInstrumentation"] = None


class PsycopgInstrumentation(InstrumentationBase):
    """Instrumentation for psycopg (psycopg3) PostgreSQL client library."""

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

            if sdk.mode == "DISABLED" or original_connect is None:
                if original_connect is None:
                    raise RuntimeError("Original psycopg.connect not found")
                return original_connect(*args, **kwargs)

            user_cursor_factory = kwargs.pop("cursor_factory", None)
            kwargs["cursor_factory"] = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # Even in REPLAY mode, create a real database connection
            # Queries are mocked at cursor.execute() level
            connection = original_connect(*args, **kwargs)
            return connection

        setattr(module, "connect", patched_connect)  # pyright: ignore[reportAttributeAccessIssue]
        logger.debug(f"psycopg.connect instrumented")

    def _create_cursor_factory(self, sdk: TuskDrift, base_factory=None):
        """Create a cursor factory that wraps cursors with instrumentation."""
        from psycopg import Cursor as BaseCursor
        
        base = base_factory or BaseCursor
        instrumentation = self
        
        class InstrumentedCursor(base):  # type: ignore
            def execute(self, query, params=None, **kwargs):
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)
            
            def executemany(self, query, params_seq, **kwargs):
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, params_seq, **kwargs)
        
        return InstrumentedCursor

    def _traced_execute(
        self, cursor: Any, original_execute: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.execute method."""
        if sdk.mode == "DISABLED":
            return original_execute(query, params, **kwargs)

        query_str = self._query_to_string(query, cursor)
        parent_trace_id = current_trace_id_context.get()
        parent_span_id = current_span_id_context.get()

        trace_id = parent_trace_id if parent_trace_id else self._generate_trace_id()
        span_id = self._generate_span_id()
        span_token = current_span_id_context.set(span_id)

        try:
            stack_trace = self._get_stack_trace()

            if sdk.mode == "REPLAY":
                # Handle background requests (app ready + no parent span)
                if sdk.app_ready and not parent_span_id:
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return cursor
                
                mock_result = self._try_get_mock(
                    sdk, query_str, params, trace_id, span_id, parent_span_id, stack_trace
                )
                
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
            start_time = time.time()
            error = None

            try:
                result = original_execute(query, params, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                if sdk.mode == "RECORD":
                    duration_ms = (time.time() - start_time) * 1000
                    self._create_query_span(
                        sdk,
                        cursor,
                        query_str,
                        params,
                        trace_id,
                        span_id,
                        parent_span_id,
                        duration_ms,
                        error,
                    )
        finally:
            current_span_id_context.reset(span_token)

    def _traced_executemany(
        self, cursor: Any, original_executemany: Any, sdk: TuskDrift, query: str, params_seq, **kwargs
    ) -> Any:
        """Traced cursor.executemany method."""
        # Pass through if SDK is disabled
        if sdk.mode == "DISABLED":
            return original_executemany(query, params_seq, **kwargs)

        # Convert query to string if it's a Composed SQL object
        query_str = self._query_to_string(query, cursor)

        # Get trace context from parent span
        parent_trace_id = current_trace_id_context.get()
        parent_span_id = current_span_id_context.get()

        # Use parent's trace_id, or generate new one if no parent (shouldn't happen for DB queries)
        if parent_trace_id:
            trace_id = parent_trace_id
        else:
            trace_id = self._generate_trace_id()

        # Generate new span_id for this query span
        span_id = self._generate_span_id()

        # Set ONLY the span_id as current (for any nested children)
        # Do NOT set trace_id again - it's already set by the parent
        span_token = current_span_id_context.set(span_id)

        try:
            stack_trace = self._get_stack_trace()

            # For executemany, we'll treat each parameter set as a batch
            # REPLAY mode: Mock ALL queries (including pre-app-start)
            if sdk.mode == "REPLAY":
                # Handle background requests: App is ready + no parent span
                # These are background jobs/health checks that run AFTER app startup
                # They were never recorded, so return empty result
                if sdk.app_ready and not parent_span_id:
                    logger.debug(f"Background executemany request (app ready, no parent) - returning empty result")
                    # Return the cursor
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return cursor
                
                # For all other queries (pre-app-start OR within a request trace), get mock
                # Convert params_seq to list for serialization
                params_list = list(params_seq)
                mock_result = self._try_get_mock(
                    sdk,
                    query_str,
                    params_list,
                    trace_id,
                    span_id,
                    parent_span_id,
                    stack_trace,
                )
                if mock_result is None:
                    # In REPLAY mode, we MUST have a mock for ALL queries
                    is_pre_app_start = not sdk.app_ready
                    logger.error(f"No mock found for {'pre-app-start ' if is_pre_app_start else ''}psycopg executemany query in REPLAY mode: {query_str[:100]}")
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg executemany query. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )
                
                # Mock execute by setting cursor internal state
                self._mock_execute_with_data(cursor, mock_result)
                return cursor  # psycopg3 executemany() returns cursor

            # RECORD mode: Execute real query and record span
            start_time = time.time()
            error = None

            try:
                result = original_executemany(query, params_seq, **kwargs)
                return result
            except Exception as e:
                error = e
                raise
            finally:
                # Only create span in RECORD mode if we have a parent
                # Database queries without a parent are pre-app startup queries (migrations, etc.)
                # and should not be recorded as traces
                if parent_span_id and sdk.mode == "RECORD":
                    duration_ms = (time.time() - start_time) * 1000
                    params_list = list(params_seq)
                    self._create_query_span(
                        sdk,
                        cursor,
                        query_str,
                        {"_batch": params_list},
                        trace_id,
                        span_id,
                        parent_span_id,
                        duration_ms,
                        error,
                    )
        finally:
            # Reset only span context (trace context is owned by parent)
            current_span_id_context.reset(span_token)

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
        parent_span_id: Optional[str],
        stack_trace: str,
    ) -> Optional[Dict[str, Any]]:
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
                stack_trace=stack_trace,
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
                logger.debug(
                    f"No mock found for psycopg query: {query[:100]}"
                )
                return None

            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg query: {e}")
            return None

    def _mock_execute_with_data(self, cursor: Any, mock_data: Dict[str, Any]) -> None:
        """Mock cursor execute by setting internal state."""
        try:
            cursor._rowcount = mock_data.get("rowcount", -1)
        except AttributeError:
            object.__setattr__(cursor, 'rowcount', mock_data.get("rowcount", -1))
        
        description_data = mock_data.get("description")
        if description_data:
            desc = [
                (col["name"], col.get("type_code"), None, None, None, None, None)
                for col in description_data
            ]
            try:
                cursor._description = desc
            except AttributeError:
                object.__setattr__(cursor, 'description', desc)

        mock_rows = mock_data.get("rows", [])
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

    def _create_query_span(
        self,
        sdk: TuskDrift,
        cursor: Any,
        query: str,
        params: Any,
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        duration_ms: float,
        error: Exception | None,
    ) -> None:
        """Create and collect a CLIENT span for the database query."""
        try:
            # Build input value
            input_value = {
                "query": query.strip(),
            }
            if params is not None:
                input_value["parameters"] = params

            # Build output value
            output_value = {}
            status = SpanStatus(code=StatusCode.OK, message="")

            if error:
                output_value = {
                    "errorName": type(error).__name__,
                    "errorMessage": str(error),
                }
                status = SpanStatus(code=StatusCode.ERROR, message=str(error))
            else:
                # Get query results and capture for replay
                try:
                    rows = []
                    description = None

                    # Try to fetch results if available
                    if hasattr(cursor, "description") and cursor.description:
                        description = [
                            {
                                "name": desc[0] if hasattr(desc, '__getitem__') else desc.name,
                                "type_code": desc[1] if hasattr(desc, '__getitem__') and len(desc) > 1 else getattr(desc, 'type_code', None),
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
                                result = cursor._tusk_rows[cursor._tusk_index:cursor._tusk_index + size]  # pyright: ignore[reportAttributeAccessIssue]
                                cursor._tusk_index += len(result)  # pyright: ignore[reportAttributeAccessIssue]
                                return result
                            
                            def patched_fetchall():
                                result = cursor._tusk_rows[cursor._tusk_index:]  # pyright: ignore[reportAttributeAccessIssue]
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
                        import datetime
                        def serialize_value(val):
                            if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
                                return val.isoformat()
                            elif isinstance(val, bytes):
                                return val.decode('utf-8', errors='replace')
                            return val
                        
                        serialized_rows = [
                            [serialize_value(col) for col in row]
                            for row in rows
                        ]
                        output_value["rows"] = serialized_rows

                except Exception as e:
                    logger.debug(f"Error getting query metadata: {e}")

            # Generate schemas and hashes
            input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})
            output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, {})

            # Create timestamp and duration
            timestamp_ms = time.time() * 1000
            timestamp_seconds = int(timestamp_ms // 1000)
            timestamp_nanos = int((timestamp_ms % 1000) * 1_000_000)

            duration_seconds = int(duration_ms // 1000)
            duration_nanos = int((duration_ms % 1000) * 1_000_000)

            # Create span
            span = CleanSpanData(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id or "",
                name="psycopg.query",
                package_name="psycopg",
                package_type=PackageType.PG,
                instrumentation_name="PsycopgInstrumentation",
                submodule_name="query",
                input_value=input_value,
                output_value=output_value,
                input_schema=input_result.schema,
                output_schema=output_result.schema,
                input_schema_hash=input_result.decoded_schema_hash,
                output_schema_hash=output_result.decoded_schema_hash,
                input_value_hash=input_result.decoded_value_hash,
                output_value_hash=output_result.decoded_value_hash,
                kind=SpanKind.CLIENT,
                status=status,
                timestamp=Timestamp(seconds=timestamp_seconds, nanos=timestamp_nanos),
                duration=Duration(seconds=duration_seconds, nanos=duration_nanos),
                is_root_span=parent_span_id is None,
                is_pre_app_start=not sdk.app_ready,
            )

            sdk.collect_span(span)

        except Exception as e:
            logger.error(f"Error creating query span: {e}")

    def _generate_trace_id(self) -> str:
        """Generate a random trace ID."""
        import secrets

        return secrets.token_hex(16)

    def _generate_span_id(self) -> str:
        """Generate a random span ID."""
        import secrets

        return secrets.token_hex(8)

    def _get_stack_trace(self) -> str:
        """Get the current stack trace."""
        stack = traceback.format_stack()
        # Filter out instrumentation frames
        filtered = [line for line in stack if "instrumentation" not in line and "drift" not in line]
        return "".join(filtered[-10:])  # Last 10 frames
