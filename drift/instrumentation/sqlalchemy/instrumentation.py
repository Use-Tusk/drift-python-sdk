from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time
from types import ModuleType
from typing import Any

from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode as OTelStatusCode
from typing_extensions import override

from ...core.drift_sdk import TuskDrift
from ...core.json_schema_helper import JsonSchemaHelper
from ...core.mock_utils import find_mock_response_sync
from ...core.tracing import TdSpanAttributes
from ...core.tracing.span_utils import CreateSpanOptions, SpanUtils
from ...core.types import PackageType, SpanKind, TuskDriftMode
from ..base import InstrumentationBase
from ..utils.serialization import serialize_value
from .context import sqlalchemy_execution_active_context, sqlalchemy_replay_mock_context

logger = logging.getLogger(__name__)
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_PCT_BIND_RE = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
_COLON_BIND_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_AUTO_BIND_SUFFIX_RE = re.compile(r"_(\d+)$")


class SqlAlchemyInstrumentation(InstrumentationBase):
    """Instrumentation for SQLAlchemy query execution."""

    def __init__(self, enabled: bool = True) -> None:
        self._listeners_registered = False
        super().__init__(
            name="SqlAlchemyInstrumentation",
            module_name="sqlalchemy",
            supported_versions=">=1.4.0",
            enabled=enabled,
        )

    @override
    def patch(self, module: ModuleType) -> None:
        """Patch SQLAlchemy by registering engine cursor-execution listeners."""
        if self._listeners_registered:
            return

        try:
            from sqlalchemy import event
            from sqlalchemy.engine import Engine
        except Exception as e:  # pragma: no cover - defensive for optional dependency loading
            logger.warning(f"Failed to import SQLAlchemy event API: {e}")
            return

        instrumentation = self

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            sdk = TuskDrift.get_instance()
            if sdk.mode == TuskDriftMode.DISABLED:
                return

            active_token = sqlalchemy_execution_active_context.set(True)
            context._tusk_sa_active_token = active_token

            span_info = instrumentation._create_query_span(sdk, is_pre_app_start=not sdk.app_ready)
            context._tusk_sa_span_info = span_info
            context._tusk_sa_statement = statement
            context._tusk_sa_parameters = parameters
            context._tusk_sa_executemany = executemany

            if not span_info:
                return

            if sdk.mode != TuskDriftMode.REPLAY:
                return

            query_str = str(statement).strip()
            mock_params: Any = {"_batch": list(parameters)} if executemany else parameters
            mock_result = instrumentation._try_get_mock(
                sdk=sdk,
                query_str=query_str,
                params=mock_params,
                trace_id=span_info.trace_id,
                span_id=span_info.span_id,
            )

            if mock_result is None:
                is_pre_app_start = not sdk.app_ready
                span_info.span.set_status(
                    Status(
                        OTelStatusCode.ERROR,
                        "No mock found for sqlalchemy query in replay",
                    )
                )
                span_info.span.end()
                instrumentation._reset_context_state(context)
                raise RuntimeError(
                    f"[Tusk REPLAY] No mock found for sqlalchemy query. "
                    f"This {'pre-app-start ' if is_pre_app_start else ''}query was not recorded during the trace capture. "
                    f"Query: {query_str[:120]}..."
                )

            # Preserve error-path behavior in replay for queries recorded as failures.
            if isinstance(mock_result, dict):
                recorded_error_message = mock_result.get("errorMessage")
                if recorded_error_message:
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, str(recorded_error_message)))
                    span_info.span.end()
                    instrumentation._reset_context_state(context)
                    raise RuntimeError(str(recorded_error_message))
                if mock_result.get("errorName"):
                    span_info.span.set_status(Status(OTelStatusCode.ERROR, str(mock_result["errorName"])))
                    span_info.span.end()
                    instrumentation._reset_context_state(context)
                    raise RuntimeError(str(mock_result["errorName"]))

            replay_token = sqlalchemy_replay_mock_context.set(mock_result)
            context._tusk_sa_mock_token = replay_token
            context._tusk_sa_mock_result = mock_result

        def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            sdk = TuskDrift.get_instance()
            span_info = getattr(context, "_tusk_sa_span_info", None)
            if not span_info:
                instrumentation._reset_context_state(context)
                return

            try:
                if sdk.mode == TuskDriftMode.REPLAY:
                    mock_result = getattr(context, "_tusk_sa_mock_result", None) or {}
                    output_value = instrumentation._build_output_from_mock(mock_result)
                else:
                    output_value = instrumentation._build_output_from_cursor(cursor)

                input_value = instrumentation._build_input_value(
                    statement=getattr(context, "_tusk_sa_statement", statement),
                    parameters=getattr(context, "_tusk_sa_parameters", parameters),
                    executemany=getattr(context, "_tusk_sa_executemany", executemany),
                )
                instrumentation._set_span_attributes(span_info.span, input_value, output_value)
                span_info.span.set_status(Status(OTelStatusCode.OK))
            except Exception as e:
                logger.error(f"Error finalizing SQLAlchemy span: {e}")
                span_info.span.set_status(Status(OTelStatusCode.ERROR, str(e)))
            finally:
                span_info.span.end()
                instrumentation._reset_context_state(context)

        def handle_error(exception_context):
            execution_context = getattr(exception_context, "execution_context", None)
            if execution_context is None:
                return

            span_info = getattr(execution_context, "_tusk_sa_span_info", None)
            if not span_info:
                instrumentation._reset_context_state(execution_context)
                return

            try:
                statement = getattr(execution_context, "_tusk_sa_statement", "")
                parameters = getattr(execution_context, "_tusk_sa_parameters", None)
                executemany = bool(getattr(execution_context, "_tusk_sa_executemany", False))
                error = getattr(exception_context, "original_exception", None) or getattr(
                    exception_context, "sqlalchemy_exception", None
                )

                input_value = instrumentation._build_input_value(
                    statement=statement,
                    parameters=parameters,
                    executemany=executemany,
                )
                output_value = {
                    "errorName": type(error).__name__ if error else "SQLAlchemyError",
                    "errorMessage": str(error) if error else "SQLAlchemy execution failed",
                }
                instrumentation._set_span_attributes(span_info.span, input_value, output_value)
                span_info.span.set_status(Status(OTelStatusCode.ERROR, output_value["errorMessage"]))
            except Exception as e:
                logger.error(f"Error finalizing SQLAlchemy error span: {e}")
                span_info.span.set_status(Status(OTelStatusCode.ERROR, str(e)))
            finally:
                span_info.span.end()
                instrumentation._reset_context_state(execution_context)

        event.listen(Engine, "before_cursor_execute", before_cursor_execute)
        event.listen(Engine, "after_cursor_execute", after_cursor_execute)
        event.listen(Engine, "handle_error", handle_error)
        self._listeners_registered = True
        logger.debug("SQLAlchemy instrumentation applied")

    def _reset_context_state(self, context: Any) -> None:
        """Reset SQLAlchemy execution contextvars and per-execution state."""
        replay_token = getattr(context, "_tusk_sa_mock_token", None)
        if replay_token is not None:
            sqlalchemy_replay_mock_context.reset(replay_token)

        active_token = getattr(context, "_tusk_sa_active_token", None)
        if active_token is not None:
            sqlalchemy_execution_active_context.reset(active_token)

    def _create_query_span(self, sdk: TuskDrift, is_pre_app_start: bool):
        return SpanUtils.create_span(
            CreateSpanOptions(
                name="sqlalchemy.query",
                kind=OTelSpanKind.CLIENT,
                attributes={
                    TdSpanAttributes.NAME: "sqlalchemy.query",
                    TdSpanAttributes.PACKAGE_NAME: "sqlalchemy",
                    TdSpanAttributes.INSTRUMENTATION_NAME: "SqlAlchemyInstrumentation",
                    TdSpanAttributes.SUBMODULE_NAME: "query",
                    TdSpanAttributes.PACKAGE_TYPE: PackageType.PG.name,
                    TdSpanAttributes.IS_PRE_APP_START: is_pre_app_start,
                },
                is_pre_app_start=is_pre_app_start,
            )
        )

    def _build_input_value(self, statement: Any, parameters: Any, executemany: bool) -> dict[str, Any]:
        canonical_query = self._canonicalize_query(str(statement).strip())
        input_value: dict[str, Any] = {"query": canonical_query}
        if parameters is not None:
            params = {"_batch": list(parameters)} if executemany else parameters
            serialized = serialize_value(params)
            normalized = self._normalize_for_matching(serialized)
            input_value["parameters"] = self._canonicalize_parameters(normalized)
        return input_value

    def _build_output_from_mock(self, mock_data: dict[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = {"rowcount": mock_data.get("rowcount", -1)}
        if mock_data.get("lastrowid") is not None:
            output["lastrowid"] = mock_data.get("lastrowid")
        if mock_data.get("description") is not None:
            output["description"] = mock_data.get("description")
        if mock_data.get("rows") is not None:
            output["rows"] = mock_data.get("rows", [])
        if mock_data.get("statusmessage") is not None:
            output["statusmessage"] = mock_data.get("statusmessage")
        return output

    def _build_output_from_cursor(self, cursor: Any) -> dict[str, Any]:
        output: dict[str, Any] = {"rowcount": getattr(cursor, "rowcount", -1)}
        lastrowid = getattr(cursor, "lastrowid", None)
        if lastrowid is not None:
            output["lastrowid"] = serialize_value(lastrowid)

        description = getattr(cursor, "description", None)
        if description:
            output["description"] = [
                {"name": desc[0], "type_code": desc[1] if len(desc) > 1 else None} for desc in description
            ]

        # Prefer rows already captured by driver instrumentation.
        raw_rows = getattr(cursor, "_tusk_rows", None)

        # If driver-level capture wasn't available, capture rows here and patch fetch
        # methods so downstream ORM/application code still sees the same results.
        if raw_rows is None and description:
            raw_rows = self._capture_rows_and_patch_cursor(cursor)

        if raw_rows is not None:
            rows = []
            for row in raw_rows:
                if isinstance(row, dict):
                    if description:
                        rows.append([serialize_value(row.get(desc[0])) for desc in description])
                    else:
                        rows.append([serialize_value(v) for v in row.values()])
                else:
                    rows.append([serialize_value(v) for v in row])
            output["rows"] = rows

        status_message = getattr(cursor, "statusmessage", None)
        if status_message:
            output["statusmessage"] = status_message

        return output

    def _capture_rows_and_patch_cursor(self, cursor: Any) -> Any:
        """Capture cursor rows and patch fetch methods to preserve behavior."""
        try:
            all_rows = cursor.fetchall()
        except Exception:
            return None

        try:
            cursor._tusk_rows = all_rows  # pyright: ignore[reportAttributeAccessIssue]
            cursor._tusk_index = 0  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            return all_rows

        def patched_fetchone():
            if cursor._tusk_index < len(cursor._tusk_rows):  # pyright: ignore[reportAttributeAccessIssue]
                row = cursor._tusk_rows[cursor._tusk_index]  # pyright: ignore[reportAttributeAccessIssue]
                cursor._tusk_index += 1  # pyright: ignore[reportAttributeAccessIssue]
                return row
            return None

        def patched_fetchmany(size=None):
            effective_size = cursor.arraysize if size is None else size
            result = cursor._tusk_rows[cursor._tusk_index : cursor._tusk_index + effective_size]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._tusk_index += len(result)  # pyright: ignore[reportAttributeAccessIssue]
            return result

        def patched_fetchall():
            result = cursor._tusk_rows[cursor._tusk_index :]  # pyright: ignore[reportAttributeAccessIssue]
            cursor._tusk_index = len(cursor._tusk_rows)  # pyright: ignore[reportAttributeAccessIssue]
            return result

        try:
            cursor.fetchone = patched_fetchone  # pyright: ignore[reportAttributeAccessIssue]
            cursor.fetchmany = patched_fetchmany  # pyright: ignore[reportAttributeAccessIssue]
            cursor.fetchall = patched_fetchall  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

        return all_rows

    def _set_span_attributes(self, span: Any, input_value: dict[str, Any], output_value: dict[str, Any]) -> None:
        input_result = JsonSchemaHelper.generate_schema_and_hash(input_value, {})
        output_result = JsonSchemaHelper.generate_schema_and_hash(output_value, {})

        span.set_attribute(TdSpanAttributes.INPUT_VALUE, json.dumps(input_value))
        span.set_attribute(TdSpanAttributes.OUTPUT_VALUE, json.dumps(output_value))
        span.set_attribute(TdSpanAttributes.INPUT_SCHEMA, json.dumps(input_result.schema.to_primitive()))
        span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA, json.dumps(output_result.schema.to_primitive()))
        span.set_attribute(TdSpanAttributes.INPUT_SCHEMA_HASH, input_result.decoded_schema_hash)
        span.set_attribute(TdSpanAttributes.OUTPUT_SCHEMA_HASH, output_result.decoded_schema_hash)
        span.set_attribute(TdSpanAttributes.INPUT_VALUE_HASH, input_result.decoded_value_hash)
        span.set_attribute(TdSpanAttributes.OUTPUT_VALUE_HASH, output_result.decoded_value_hash)

    def _try_get_mock(
        self,
        sdk: TuskDrift,
        query_str: str,
        params: Any,
        trace_id: str,
        span_id: str,
    ) -> dict[str, Any] | None:
        input_value = {"query": self._canonicalize_query(query_str)}
        if params is not None:
            serialized = serialize_value(params)
            normalized = self._normalize_for_matching(serialized)
            input_value["parameters"] = self._canonicalize_parameters(normalized)

        mock_response_output = find_mock_response_sync(
            sdk=sdk,
            trace_id=trace_id,
            span_id=span_id,
            name="sqlalchemy.query",
            package_name="sqlalchemy",
            package_type=PackageType.PG,
            instrumentation_name="SqlAlchemyInstrumentation",
            submodule_name="query",
            input_value=input_value,
            kind=SpanKind.CLIENT,
            is_pre_app_start=not sdk.app_ready,
        )

        if not mock_response_output or not mock_response_output.found:
            logger.debug(f"No mock found for sqlalchemy query: {query_str[:100]}")
            return None

        return mock_response_output.response

    def _normalize_for_matching(self, value: Any, key_name: str | None = None) -> Any:
        """Normalize known non-deterministic timestamp-like values for stable matching."""
        if isinstance(value, (datetime, date, time)):
            return "__td_dynamic_time__"

        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                normalized[key] = self._normalize_for_matching(child, key_name=key)
            return normalized

        if isinstance(value, list):
            return [self._normalize_for_matching(v, key_name=key_name) for v in value]

        if isinstance(value, tuple):
            return [self._normalize_for_matching(v, key_name=key_name) for v in value]

        if isinstance(value, str):
            lowered = (key_name or "").lower()
            if "time" in lowered or "date" in lowered:
                return "__td_dynamic_time__"
            if _ISO_DATETIME_RE.match(value):
                return "__td_dynamic_time__"
            return value

        return value

    def _canonicalize_query(self, query: str) -> str:
        """Normalize SQLAlchemy bind placeholder name churn while preserving SQL shape."""
        query = _PCT_BIND_RE.sub("%(__td_param__)s", query)
        query = _COLON_BIND_RE.sub(":__td_param__", query)
        return query

    def _canonicalize_bind_key(self, key: str) -> str:
        """Canonicalize auto-generated SQLAlchemy bind names."""
        if _AUTO_BIND_SUFFIX_RE.search(key):
            return _AUTO_BIND_SUFFIX_RE.sub("_N", key)
        return key

    def _canonicalize_parameters(self, value: Any) -> Any:
        """Canonicalize parameter containers so auto-generated bind names hash stably."""
        if isinstance(value, dict):
            # Keep deterministic ordering and canonical key names.
            items = []
            for key, child in value.items():
                canonical_key = self._canonicalize_bind_key(str(key))
                items.append((canonical_key, str(key), self._canonicalize_parameters(child)))
            items.sort(key=lambda x: (x[0], x[1]))
            return [{"k": canonical_key, "v": child} for canonical_key, _, child in items]

        if isinstance(value, list):
            return [self._canonicalize_parameters(v) for v in value]

        if isinstance(value, tuple):
            return [self._canonicalize_parameters(v) for v in value]

        return value
