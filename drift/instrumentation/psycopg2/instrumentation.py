"""Instrumentation for psycopg2 PostgreSQL client library."""

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
_instance: Optional["Psycopg2Instrumentation"] = None


class Psycopg2Instrumentation(InstrumentationBase):
    """Instrumentation for the psycopg2 PostgreSQL client library.

    Patches psycopg2 cursor methods to:
    - Intercept SQL queries in REPLAY mode and return mocked responses
    - Capture query/response data as CLIENT spans in RECORD mode
    
    This implementation uses psycopg2's cursor_factory feature to wrap cursors.
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

        def patched_connect(*args, **kwargs):
            """Patched psycopg2.connect method."""
            logger.debug(f"[PATCHED_CONNECT] called with args={args[:2] if args else 'none'}")
            sdk = TuskDrift.get_instance()

            # Pass through if SDK is disabled or original connect is missing
            if sdk.mode == "DISABLED" or original_connect is None:
                if original_connect is None:
                    raise RuntimeError("Original psycopg2.connect not found")
                return original_connect(*args, **kwargs)

            # Use cursor_factory to wrap cursors
            # Save any user-provided cursor_factory
            user_cursor_factory = kwargs.pop("cursor_factory", None)
            
            # Create our instrumented cursor factory
            kwargs["cursor_factory"] = instrumentation._create_cursor_factory(sdk, user_cursor_factory)

            # Create connection with our cursor factory
            connection = original_connect(*args, **kwargs)
            logger.debug(f"[PATCHED_CONNECT] returning connection with instrumented cursor factory")

            return connection

        # Apply patch
        setattr(module, "connect", patched_connect)  # pyright: ignore[reportAttributeAccessIssue]
        logger.info(f"psycopg2.connect instrumented. module.connect is now: {module.connect}")
        
        # Also verify it's actually patched
        import psycopg2
        if psycopg2.connect == patched_connect:
            logger.info("[VERIFY] psycopg2.connect successfully patched!")
        else:
            logger.error(f"[VERIFY] psycopg2.connect NOT patched! psycopg2.connect={psycopg2.connect}, patched_connect={patched_connect}")

    def _create_cursor_factory(self, sdk: TuskDrift, base_factory=None):
        """Create a cursor factory that wraps cursors with instrumentation."""
        from psycopg2.extensions import cursor as BaseCursor
        
        base = base_factory or BaseCursor
        instrumentation = self
        logger.debug(f"[CURSOR_FACTORY] Creating InstrumentedCursor class, base={base}, sdk.mode={sdk.mode}")
        
        class InstrumentedCursor(base):  # type: ignore
            def execute(self, query, vars=None):
                logger.debug(f"[INSTRUMENTED_CURSOR] execute() called on instrumented cursor")
                return instrumentation._traced_execute(self, super().execute, sdk, query, vars)
            
            def executemany(self, query, vars_list):
                logger.debug(f"[INSTRUMENTED_CURSOR] executemany() called on instrumented cursor")
                return instrumentation._traced_executemany(self, super().executemany, sdk, query, vars_list)
        
        return InstrumentedCursor

    def _traced_execute(
        self, cursor: Any, original_execute: Any, sdk: TuskDrift, query: str, params=None
    ) -> Any:
        """Traced cursor.execute method."""
        logger.debug(f"[PSYCOPG2] _traced_execute called. SDK mode: {sdk.mode}, query: {query[:100]}")
        # Pass through if SDK is disabled
        if sdk.mode == "DISABLED":
            return original_execute(query, params)

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

            # REPLAY mode: MUST get mock, no real query execution
            if sdk.mode == "REPLAY":
                print(f"[PSYCOPG2_REPLAY] execute() called, query: {query[:100]}", flush=True)
                print(f"[PSYCOPG2_REPLAY] parent_span_id: {parent_span_id}, app_ready: {sdk.app_ready}", flush=True)
                
                # TODO: HACK - Using replay_trace_id as temporary detection for request context
                # Proper implementation should:
                # 1. Mark spans as isPreAppStart=True during recording  
                # 2. CLI should load and return these pre-app-start mocks
                # 3. Apply mocks even during startup
                # See Node.js implementation for reference
                
                # Get replay trace ID from context
                replay_trace_id = replay_trace_id_context.get()
                
                print(f"[PSYCOPG2_REPLAY] parent_span_id={parent_span_id is not None}, replay_trace_id={replay_trace_id}", flush=True)
                
                # Only try to get mock if we have a replay_trace_id (i.e., we're in an HTTP request)
                # Queries without replay_trace_id (startup/migration queries) execute against real DB for now
                if parent_span_id and replay_trace_id:
                    print(f"[PSYCOPG2_REPLAY] Have parent and replay trace, trying to get mock", flush=True)
                    mock_result = self._try_get_mock(
                        sdk, query, params, trace_id, span_id, parent_span_id, stack_trace
                    )
                    print(f"[PSYCOPG2_REPLAY] Mock result: {mock_result is not None}", flush=True)
                    if mock_result is None:
                        # In REPLAY mode within a request, we MUST have a mock
                        print(f"[PSYCOPG2_REPLAY] ERROR: No mock found for query: {query[:100]}", flush=True)
                        raise RuntimeError(
                            f"[Tusk REPLAY] No mock found for psycopg2 execute query. "
                            f"This query was not recorded during the trace capture. "
                            f"Query: {query[:100]}..."
                        )
                    
                    print(f"[PSYCOPG2_REPLAY] Got mock, applying to cursor", flush=True)
                    # Mock execute by setting cursor internal state
                    self._mock_execute_with_data(cursor, mock_result)
                    print(f"[PSYCOPG2_REPLAY] Mock applied successfully", flush=True)
                    return None  # execute() returns None
                else:
                    # No parent or no replay_trace_id = pre-app-start query (migrations, etc.)
                    # These can execute normally even in REPLAY mode
                    print(f"[PSYCOPG2_REPLAY] No request context, executing query against real DB: {query[:80]}", flush=True)
                    result = original_execute(query, params)
                    print(f"[PSYCOPG2_REPLAY] Real query completed", flush=True)
                    return result

            # RECORD mode: Execute real query and record span
            start_time = time.time()
            error = None
            
            print(f"[PSYCOPG2_RECORD] execute() called, query: {query[:100]}", flush=True)
            print(f"[PSYCOPG2_RECORD] parent_span_id: {parent_span_id}, app_ready: {sdk.app_ready}", flush=True)

            try:
                result = original_execute(query, params)
                print(f"[PSYCOPG2_RECORD] Query executed successfully", flush=True)
                return result
            except Exception as e:
                error = e
                print(f"[PSYCOPG2_RECORD] Query failed with error: {e}", flush=True)
                raise
            finally:
                # Only create span in RECORD mode if we have a parent
                # Database queries without a parent are pre-app startup queries (migrations, etc.)
                # and should not be recorded as traces
                if parent_span_id and sdk.mode == "RECORD":
                    print(f"[PSYCOPG2_RECORD] Creating span for query", flush=True)
                    duration_ms = (time.time() - start_time) * 1000
                    self._create_query_span(
                        sdk,
                        cursor,
                        query,
                        params,
                        trace_id,
                        span_id,
                        parent_span_id,
                        duration_ms,
                        error,
                    )
                elif not parent_span_id:
                    print(f"[PSYCOPG2_RECORD] Skipping span creation (no parent) - pre-app-start query: {query[:80]}", flush=True)
        finally:
            # Reset only span context (trace context is owned by parent)
            current_span_id_context.reset(span_token)

    def _traced_executemany(
        self, cursor: Any, original_executemany: Any, sdk: TuskDrift, query: str, params_list
    ) -> Any:
        """Traced cursor.executemany method."""
        # Pass through if SDK is disabled
        if sdk.mode == "DISABLED":
            return original_executemany(query, params_list)

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
            # REPLAY mode: MUST get mock, no real query execution
            if sdk.mode == "REPLAY":
                # Only try to get mock if we're in a request context (have parent)
                # Pre-app-start queries (no parent) can execute normally since they're not part of traces
                if parent_span_id:
                    mock_result = self._try_get_mock(
                        sdk,
                        query,
                        params_list,
                        trace_id,
                        span_id,
                        parent_span_id,
                        stack_trace,
                    )
                    if mock_result is None:
                        # In REPLAY mode within a request, we MUST have a mock
                        logger.error(f"No mock found for psycopg2 query in REPLAY mode: {query[:100]}")
                        raise RuntimeError(
                            f"[Tusk REPLAY] No mock found for psycopg2 query. "
                            f"This query was not recorded during the trace capture. "
                            f"Query: {query[:100]}..."
                        )
                    
                    # Instead of modifying cursor, create a mock execute that sets internal state
                    # We need to set the cursor's internal state that gets populated during execute()
                    self._mock_execute_with_data(cursor, mock_result)
                    return None  # execute() returns None
                else:
                    # No parent = pre-app-start query (migrations, etc.)
                    # These can execute normally even in REPLAY mode
                    logger.debug(f"Allowing pre-app-start executemany in REPLAY mode: {query[:100]}")
                    return original_executemany(query, params_list)

            # RECORD mode: Execute real query and record span
            start_time = time.time()
            error = None

            try:
                result = original_executemany(query, params_list)
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
                    self._create_query_span(
                        sdk,
                        cursor,
                        query,
                        {"_batch": list(params_list)},
                        trace_id,
                        span_id,
                        parent_span_id,
                        duration_ms,
                        error,
                    )
        finally:
            # Reset only span context (trace context is owned by parent)
            current_span_id_context.reset(span_token)

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
                name="psycopg2.query",
                package_name="psycopg2",
                package_type=PackageType.PG,
                instrumentation_name="Psycopg2Instrumentation",
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
                is_pre_app_start=False,
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
                    f"No mock found for psycopg2 query:\n"
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

            logger.info(f"[MOCK_FOUND] Found mock for psycopg2 query: {query[:100]}")
            logger.info(f"[MOCK_FOUND] Mock response type: {type(mock_response_output.response)}")
            logger.info(f"[MOCK_FOUND] Mock response: {mock_response_output.response}")
            return mock_response_output.response

        except Exception as e:
            logger.error(f"Error getting mock for psycopg2 query: {e}")
            return None

    def _mock_execute_with_data(self, cursor: Any, mock_data: Dict[str, Any]) -> None:
        """Mock the cursor execute by setting internal state directly.
        
        In psycopg2, cursor.execute() sets internal C-level attributes that we can't modify.
        Instead, we directly set the private attributes that psycopg2 uses internally.
        """
        logger.debug(f"Mocking execute with data. Mock data keys: {mock_data.keys() if isinstance(mock_data, dict) else 'not a dict'}")
        
        # Set internal cursor state that gets populated during execute()
        # These are internal attributes that psycopg2 uses
        try:
            # rowcount: psycopg2 stores this in cursor.rowcount (read-only property)
            # We need to set the internal C attribute directly using object.__setattr__
            object.__setattr__(cursor, 'rowcount', mock_data.get("rowcount", -1))
        except (AttributeError, TypeError) as e:
            logger.debug(f"Could not set rowcount via __setattr__: {e}")
            # Try setting the private attribute that backs rowcount
            try:
                cursor._rowcount = mock_data.get("rowcount", -1)
            except AttributeError:
                logger.debug("Could not set _rowcount either")
        
        # description: psycopg2 description format
        description_data = mock_data.get("description")
        if description_data:
            # Convert to psycopg2 Column format
            desc = [
                (col["name"], col.get("type_code"), None, None, None, None, None)
                for col in description_data
            ]
            try:
                object.__setattr__(cursor, 'description', desc)
            except (AttributeError, TypeError):
                try:
                    cursor._description = desc
                except AttributeError:
                    logger.debug("Could not set description")

        # Store mock rows for fetching
        mock_rows = mock_data.get("rows", [])
        cursor._mock_rows = mock_rows  # pyright: ignore[reportAttributeAccessIssue]
        cursor._mock_index = 0  # pyright: ignore[reportAttributeAccessIssue]

        # Patch fetch methods
        original_fetchone = cursor.fetchone if hasattr(cursor, "fetchone") else None
        original_fetchmany = cursor.fetchmany if hasattr(cursor, "fetchmany") else None
        original_fetchall = cursor.fetchall if hasattr(cursor, "fetchall") else None

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
            logger.debug(f"[MOCK] fetchall called, returning {len(cursor._mock_rows[cursor._mock_index:])} rows")  # pyright: ignore[reportAttributeAccessIssue]
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
                            original_fetchone = cursor.fetchone if hasattr(cursor, 'fetchone') else None
                            original_fetchmany = cursor.fetchmany if hasattr(cursor, 'fetchmany') else None
                            original_fetchall = cursor.fetchall if hasattr(cursor, 'fetchall') else None
                            
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
                name="psycopg2.query",
                package_name="psycopg2",
                package_type=PackageType.PG,
                instrumentation_name="Psycopg2Instrumentation",
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
