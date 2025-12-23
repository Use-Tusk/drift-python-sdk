"""Instrumentation for psycopg (psycopg3) PostgreSQL client library."""

from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Dict, Optional
from types import ModuleType

from ..base import InstrumentationBase
from ...core.communication.types import MockRequestInput
from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchema, JsonSchemaHelper, SchemaMerge
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

# Module-level variable to store the instrumentation instance
# This allows Django instrumentation to access it
_instance: Optional["PsycopgInstrumentation"] = None


class PsycopgInstrumentation(InstrumentationBase):
    """Instrumentation for the psycopg (psycopg3) PostgreSQL client library.

    Patches psycopg cursor methods to:
    - Intercept SQL queries in REPLAY mode and return mocked responses
    - Capture query/response data as CLIENT spans in RECORD mode
    
    This implementation uses psycopg's cursor_factory feature to wrap cursors.
    psycopg (v3) is a pure Python implementation, making it easier to instrument
    than psycopg2's C extension.
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
        _instance = self  # Store instance for Django instrumentation to access

    def patch(self, module: ModuleType) -> None:
        """Patch the psycopg module."""
        if not hasattr(module, "connect"):
            logger.warning("psycopg.connect not found, skipping instrumentation")
            return

        # Store original connect function
        self._original_connect = module.connect
        
        # Capture self and original_connect in the closure
        instrumentation = self
        original_connect = self._original_connect

        def patched_connect(*args, **kwargs):
            """Patched psycopg.connect method."""
            logger.debug(f"[PATCHED_CONNECT] called with args={args[:2] if args else 'none'}, mode={TuskDrift.get_instance().mode}")
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled or original connect is missing
            if sdk.mode == "DISABLED" or original_connect is None:
                if original_connect is None:
                    raise RuntimeError("Original psycopg.connect not found")
                return original_connect(*args, **kwargs)

            # Use cursor_factory to wrap cursors
            # Save any user-provided cursor_factory
            user_cursor_factory = kwargs.pop("cursor_factory", None)
            
            # Create our instrumented cursor factory
            kwargs["cursor_factory"] = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # Create real connection with our cursor factory
            # NOTE: Even in REPLAY mode, we create a real database connection
            # The database queries are still mocked at the cursor.execute() level
            connection = original_connect(*args, **kwargs)
            logger.debug(f"[PATCHED_CONNECT] returning connection with instrumented cursor factory")

            return connection

        # Apply patch
        setattr(module, "connect", patched_connect)  # pyright: ignore[reportAttributeAccessIssue]
        logger.info(f"psycopg.connect instrumented. module.connect is now: {module.connect}")
        
        # Also verify it's actually patched
        import psycopg
        if psycopg.connect == patched_connect:
            logger.info("[VERIFY] psycopg.connect successfully patched!")
        else:
            logger.error(f"[VERIFY] psycopg.connect NOT patched! psycopg.connect={psycopg.connect}, patched_connect={patched_connect}")

    def _create_cursor_factory(self, sdk: TuskDrift, base_factory=None):
        """Create a cursor factory that wraps cursors with instrumentation."""
        from psycopg import Cursor as BaseCursor
        
        base = base_factory or BaseCursor
        instrumentation = self
        logger.debug(f"[CURSOR_FACTORY] Creating InstrumentedCursor class, base={base}, sdk.mode={sdk.mode}")
        
        class InstrumentedCursor(base):  # type: ignore
            def execute(self, query, params=None, **kwargs):
                logger.debug(f"[INSTRUMENTED_CURSOR] execute() called on instrumented cursor")
                return instrumentation._traced_execute(self, super().execute, sdk, query, params, **kwargs)
            
            def executemany(self, query, params_seq, **kwargs):
                logger.debug(f"[INSTRUMENTED_CURSOR] executemany() called on instrumented cursor")
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, params_seq, **kwargs)
        
        return InstrumentedCursor

    def _traced_execute(
        self, cursor: Any, original_execute: Any, sdk: TuskDrift, query: str, params=None, **kwargs
    ) -> Any:
        """Traced cursor.execute method."""
        logger.debug(f"[PSYCOPG] _traced_execute called. SDK mode: {sdk.mode}, query: {query[:100] if isinstance(query, str) else str(query)[:100]}")
        # Pass through if SDK is disabled
        if sdk.mode == "DISABLED":
            return original_execute(query, params, **kwargs)

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

            # REPLAY mode: Mock ALL queries (including pre-app-start)
            if sdk.mode == "REPLAY":
                print(f"[PSYCOPG_REPLAY] execute() called, query: {query_str[:100]}", flush=True)
                print(f"[PSYCOPG_REPLAY] parent_span_id: {parent_span_id}, app_ready: {sdk.app_ready}", flush=True)
                
                # Handle background requests: App is ready + no parent span
                # These are background jobs/health checks that run AFTER app startup
                # They were never recorded, so return empty result
                if sdk.app_ready and not parent_span_id:
                    print(f"[PSYCOPG_REPLAY] Background request (app ready, no parent) - returning empty result", flush=True)
                    # Return the cursor (psycopg3 execute returns cursor, not None)
                    cursor._mock_rows = []  # pyright: ignore
                    cursor._mock_index = 0  # pyright: ignore
                    return cursor
                
                # For all other queries (pre-app-start OR within a request trace), get mock
                replay_trace_id = replay_trace_id_context.get()
                print(f"[PSYCOPG_REPLAY] replay_trace_id={replay_trace_id}, is_pre_app_start={not sdk.app_ready}", flush=True)
                
                # Try to get mock for this query
                mock_result = self._try_get_mock(
                    sdk, query_str, params, trace_id, span_id, parent_span_id, stack_trace
                )
                print(f"[PSYCOPG_REPLAY] Mock result: {mock_result is not None}", flush=True)
                
                if mock_result is None:
                    # In REPLAY mode, we MUST have a mock for ALL queries
                    is_pre_app_start = not sdk.app_ready
                    print(f"[PSYCOPG_REPLAY] ERROR: No mock found for {'pre-app-start ' if is_pre_app_start else ''}query: {query_str[:100]}", flush=True)
                    raise RuntimeError(
                        f"[Tusk REPLAY] No mock found for psycopg execute query. "
                        f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                        f"Query: {query_str[:100]}..."
                    )
                
                print(f"[PSYCOPG_REPLAY] Got mock, applying to cursor", flush=True)
                # Mock execute by setting cursor internal state
                self._mock_execute_with_data(cursor, mock_result)
                print(f"[PSYCOPG_REPLAY] Mock applied successfully", flush=True)
                return cursor  # psycopg3 execute() returns cursor

            # RECORD mode: Execute real query and record span
            start_time = time.time()
            error = None
            
            print(f"[PSYCOPG_RECORD] execute() called, query: {query_str[:100]}", flush=True)
            print(f"[PSYCOPG_RECORD] parent_span_id: {parent_span_id}, app_ready: {sdk.app_ready}", flush=True)

            try:
                result = original_execute(query, params, **kwargs)
                print(f"[PSYCOPG_RECORD] Query executed successfully", flush=True)
                return result
            except Exception as e:
                error = e
                print(f"[PSYCOPG_RECORD] Query failed with error: {e}", flush=True)
                raise
            finally:
                # Always create span in RECORD mode (including pre-app-start queries)
                if sdk.mode == "RECORD":
                    print(f"[PSYCOPG_RECORD] Creating span for query (is_pre_app_start={not sdk.app_ready})", flush=True)
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
            # Reset only span context (trace context is owned by parent)
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
        """Convert query to string, handling psycopg3's Composed SQL objects."""
        try:
            from psycopg.sql import Composed
            if isinstance(query, Composed):
                # Convert Composed SQL to string using cursor context
                return query.as_string(cursor)
        except ImportError:
            pass
        
        # If it's already a string or not a Composed object, just stringify it
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

            logger.info(f"[MOCK_REQUEST] Requesting mock from CLI:")
            logger.info(f"  replay_trace_id={replay_trace_id}")
            logger.info(f"  span_id={span_id}")
            logger.info(f"  package_name={mock_span.package_name}")
            logger.info(f"  input_value_hash={mock_span.input_value_hash}")
            logger.info(f"  input_schema_hash={mock_span.input_schema_hash}")
            logger.info(f"  query={query[:100]}")
            mock_response_output = sdk.request_mock_sync(mock_request)
            logger.info(f"[MOCK_RESPONSE] CLI returned: found={mock_response_output.found}, response={mock_response_output.response is not None}")

            if not mock_response_output.found:
                logger.error(
                    f"No mock found for psycopg query:\n"
                    f"  replay_trace_id={replay_trace_id}\n"
                    f"  span_trace_id={trace_id}\n"
                    f"  span_id={span_id}\n"
                    f"  package_name={mock_span.package_name}\n"
                    f"  package_type={mock_span.package_type}\n"
                    f"  name={mock_span.name}\n"
                    f"  input_value_hash={mock_span.input_value_hash}\n"
                    f"  input_schema_hash={mock_span.input_schema_hash}\n"
                    f"  query={query[:100]}"
                )
                return None

            logger.info(f"[MOCK_FOUND] Found mock for psycopg query: {query[:100]}")
            logger.info(f"[MOCK_FOUND] Mock response type: {type(mock_response_output.response)}")
            logger.info(f"[MOCK_FOUND] Mock response: {mock_response_output.response}")
            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg query: {e}")
            return None

    def _mock_execute_with_data(self, cursor: Any, mock_data: Dict[str, Any]) -> None:
        """Mock the cursor execute by setting internal state directly.
        
        In psycopg3, cursor.execute() sets Python-level attributes that we can modify.
        This is easier than psycopg2 since there are no C-level attributes.
        """
        logger.debug(f"Mocking execute with data. Mock data keys: {mock_data.keys() if isinstance(mock_data, dict) else 'not a dict'}")
        
        # Set rowcount (psycopg3 uses standard Python attribute)
        try:
            cursor._rowcount = mock_data.get("rowcount", -1)
        except AttributeError:
            # Fallback to object.__setattr__ if attribute is read-only
            object.__setattr__(cursor, 'rowcount', mock_data.get("rowcount", -1))
        
        # description: psycopg3 description format (similar to psycopg2)
        description_data = mock_data.get("description")
        if description_data:
            # Convert to psycopg Column-like format
            # psycopg3 uses Column objects, but we can use tuples for compatibility
            desc = [
                (col["name"], col.get("type_code"), None, None, None, None, None)
                for col in description_data
            ]
            try:
                cursor._description = desc
            except AttributeError:
                object.__setattr__(cursor, 'description', desc)

        # Store mock rows for fetching
        mock_rows = mock_data.get("rows", [])
        cursor._mock_rows = mock_rows  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        # Patch fetch methods
        def mock_fetchone():
            if cursor._mock_index < len(cursor._mock_rows):  # pyright: ignore[reportAttributeAccessIssue]
                row = cursor._mock_rows[cursor._mock_index]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._mock_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                # Convert list to tuple to match psycopg3 behavior
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
            logger.debug(f"[MOCK] fetchall called, returning {len(cursor._mock_rows[cursor._mock_index:])} rows")  # pyright: ignore[reportAttributeAccessIssue]
            rows = cursor._mock_rows[cursor._mock_index :]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._mock_index = len(cursor._mock_rows)  # pyright: ignore[reportAttributeAccessIssue]
            # Convert lists to tuples to match psycopg3 behavior
            result = [tuple(row) if isinstance(row, list) else row for row in rows]
            logger.debug(f"[MOCK] fetchall returning: {result}")
            return result

        logger.debug(f"[MOCK] Patching cursor fetch methods with mock data ({len(mock_rows)} rows)")
        cursor.fetchone = mock_fetchone  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchmany = mock_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
        cursor.fetchall = mock_fetchall  # pyright: ignore[reportAttributeAccessIssue]
        logger.debug(f"[MOCK] Cursor fetch methods patched successfully")

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
