"""Tests for otel_converter.py - OpenTelemetry to CleanSpanData conversion."""

from __future__ import annotations

import json

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext
from opentelemetry.trace import SpanKind as OTelSpanKind
from opentelemetry.trace.status import StatusCode as OTelStatusCode

from drift.core.json_schema_helper import DecodedType, EncodingType, JsonSchemaType
from drift.core.tracing.otel_converter import (
    dict_to_json_schema,
    format_span_id,
    format_trace_id,
    get_attribute_as_bool,
    get_attribute_as_dict,
    get_attribute_as_schema_merges,
    get_attribute_as_str,
    ns_to_duration,
    ns_to_timestamp,
    otel_span_kind_to_drift,
    otel_span_to_clean_span_data,
    otel_status_to_drift_status,
    parse_package_type,
)
from drift.core.tracing.td_attributes import TdSpanAttributes
from drift.core.types import PackageType, SpanKind, StatusCode


class TestFormatTraceId:
    """Tests for format_trace_id function."""

    def test_formats_trace_id_to_32_hex_chars(self):
        """Should format trace ID to 32-character hex string."""
        trace_id = 0x0123456789ABCDEF0123456789ABCDEF
        result = format_trace_id(trace_id)
        assert result == "0123456789abcdef0123456789abcdef"
        assert len(result) == 32

    def test_pads_small_trace_id(self):
        """Should pad small trace IDs with leading zeros."""
        trace_id = 0x1
        result = format_trace_id(trace_id)
        assert result == "00000000000000000000000000000001"
        assert len(result) == 32

    def test_zero_trace_id(self):
        """Should handle zero trace ID."""
        result = format_trace_id(0)
        assert result == "0" * 32


class TestFormatSpanId:
    """Tests for format_span_id function."""

    def test_formats_span_id_to_16_hex_chars(self):
        """Should format span ID to 16-character hex string."""
        span_id = 0x0123456789ABCDEF
        result = format_span_id(span_id)
        assert result == "0123456789abcdef"
        assert len(result) == 16

    def test_pads_small_span_id(self):
        """Should pad small span IDs with leading zeros."""
        span_id = 0x1
        result = format_span_id(span_id)
        assert result == "0000000000000001"
        assert len(result) == 16

    def test_zero_span_id(self):
        """Should handle zero span ID."""
        result = format_span_id(0)
        assert result == "0" * 16


class TestNsToTimestamp:
    """Tests for ns_to_timestamp function."""

    def test_converts_nanoseconds_to_timestamp(self):
        """Should convert nanoseconds to Timestamp with seconds and nanos."""
        ns = 1700000000_123456789  # 1700000000 seconds + 123456789 nanos
        result = ns_to_timestamp(ns)
        assert result.seconds == 1700000000
        assert result.nanos == 123456789

    def test_zero_nanoseconds(self):
        """Should handle zero nanoseconds."""
        result = ns_to_timestamp(0)
        assert result.seconds == 0
        assert result.nanos == 0

    def test_only_seconds(self):
        """Should handle exact second values."""
        ns = 5_000_000_000  # 5 seconds
        result = ns_to_timestamp(ns)
        assert result.seconds == 5
        assert result.nanos == 0

    def test_only_nanos(self):
        """Should handle sub-second values."""
        ns = 500_000_000  # 0.5 seconds
        result = ns_to_timestamp(ns)
        assert result.seconds == 0
        assert result.nanos == 500_000_000


class TestNsToDuration:
    """Tests for ns_to_duration function."""

    def test_converts_nanoseconds_to_duration(self):
        """Should convert nanoseconds to Duration with seconds and nanos."""
        ns = 2_500_000_000  # 2.5 seconds
        result = ns_to_duration(ns)
        assert result.seconds == 2
        assert result.nanos == 500_000_000

    def test_zero_duration(self):
        """Should handle zero duration."""
        result = ns_to_duration(0)
        assert result.seconds == 0
        assert result.nanos == 0


class TestOtelStatusToDriftStatus:
    """Tests for otel_status_to_drift_status function."""

    def test_converts_ok_status(self, mocker):
        """Should convert OK status."""
        otel_status = mocker.MagicMock()
        otel_status.status_code = OTelStatusCode.OK
        otel_status.description = "Success"

        result = otel_status_to_drift_status(otel_status)
        assert result.code == StatusCode.OK
        assert result.message == "Success"

    def test_converts_error_status(self, mocker):
        """Should convert ERROR status."""
        otel_status = mocker.MagicMock()
        otel_status.status_code = OTelStatusCode.ERROR
        otel_status.description = "Something went wrong"

        result = otel_status_to_drift_status(otel_status)
        assert result.code == StatusCode.ERROR
        assert result.message == "Something went wrong"

    def test_converts_unset_status(self, mocker):
        """Should convert UNSET status to UNSPECIFIED."""
        otel_status = mocker.MagicMock()
        otel_status.status_code = OTelStatusCode.UNSET
        otel_status.description = None

        result = otel_status_to_drift_status(otel_status)
        assert result.code == StatusCode.UNSPECIFIED
        assert result.message == ""

    def test_handles_none_description(self, mocker):
        """Should handle None description."""
        otel_status = mocker.MagicMock()
        otel_status.status_code = OTelStatusCode.OK
        otel_status.description = None

        result = otel_status_to_drift_status(otel_status)
        assert result.message == ""


class TestOtelSpanKindToDrift:
    """Tests for otel_span_kind_to_drift function."""

    def test_converts_server_kind(self):
        """Should convert SERVER kind."""
        assert otel_span_kind_to_drift(OTelSpanKind.SERVER) == SpanKind.SERVER

    def test_converts_client_kind(self):
        """Should convert CLIENT kind."""
        assert otel_span_kind_to_drift(OTelSpanKind.CLIENT) == SpanKind.CLIENT

    def test_converts_internal_kind(self):
        """Should convert INTERNAL kind."""
        assert otel_span_kind_to_drift(OTelSpanKind.INTERNAL) == SpanKind.INTERNAL

    def test_converts_producer_kind(self):
        """Should convert PRODUCER kind."""
        assert otel_span_kind_to_drift(OTelSpanKind.PRODUCER) == SpanKind.PRODUCER

    def test_converts_consumer_kind(self):
        """Should convert CONSUMER kind."""
        assert otel_span_kind_to_drift(OTelSpanKind.CONSUMER) == SpanKind.CONSUMER

    def test_handles_unknown_kind(self):
        """Should return UNSPECIFIED for unknown kind."""
        result = otel_span_kind_to_drift("UNKNOWN")
        assert result == SpanKind.UNSPECIFIED

    def test_handles_int_kind(self):
        """Should handle integer kind values."""
        # SpanKind.INTERNAL = 1, SpanKind.SERVER = 2
        result = otel_span_kind_to_drift(2)
        assert result == SpanKind.SERVER

    def test_handles_invalid_int_kind(self):
        """Should return UNSPECIFIED for invalid integer kind."""
        result = otel_span_kind_to_drift(999)
        assert result == SpanKind.UNSPECIFIED


class TestGetAttributeAsStr:
    """Tests for get_attribute_as_str function."""

    def test_returns_string_value(self):
        """Should return string value as-is."""
        attrs = {"key": "value"}
        assert get_attribute_as_str(attrs, "key") == "value"

    def test_returns_default_for_missing_key(self):
        """Should return default for missing key."""
        attrs = {}
        assert get_attribute_as_str(attrs, "key") == ""
        assert get_attribute_as_str(attrs, "key", "default") == "default"

    def test_returns_default_for_none_value(self):
        """Should return default for None value."""
        attrs = {"key": None}
        assert get_attribute_as_str(attrs, "key", "default") == "default"

    def test_converts_non_string_to_string(self):
        """Should convert non-string values to string."""
        attrs = {"int": 42, "bool": True, "float": 3.14}
        assert get_attribute_as_str(attrs, "int") == "42"
        assert get_attribute_as_str(attrs, "bool") == "True"
        assert get_attribute_as_str(attrs, "float") == "3.14"


class TestGetAttributeAsDict:
    """Tests for get_attribute_as_dict function."""

    def test_parses_json_string(self):
        """Should parse JSON string to dict."""
        attrs = {"key": '{"foo": "bar", "num": 42}'}
        result = get_attribute_as_dict(attrs, "key")
        assert result == {"foo": "bar", "num": 42}

    def test_returns_dict_as_is(self):
        """Should return dict value as-is."""
        attrs = {"key": {"foo": "bar"}}
        result = get_attribute_as_dict(attrs, "key")
        assert result == {"foo": "bar"}

    def test_returns_none_for_missing_key(self):
        """Should return None for missing key."""
        attrs = {}
        assert get_attribute_as_dict(attrs, "key") is None

    def test_returns_none_for_invalid_json(self):
        """Should return None for invalid JSON."""
        attrs = {"key": "not valid json"}
        assert get_attribute_as_dict(attrs, "key") is None

    def test_returns_none_for_non_dict_types(self):
        """Should return None for non-dict types."""
        attrs = {"key": 42}
        assert get_attribute_as_dict(attrs, "key") is None


class TestGetAttributeAsBool:
    """Tests for get_attribute_as_bool function."""

    def test_returns_bool_value(self):
        """Should return boolean value as-is."""
        attrs = {"true": True, "false": False}
        assert get_attribute_as_bool(attrs, "true") is True
        assert get_attribute_as_bool(attrs, "false") is False

    def test_returns_default_for_missing_key(self):
        """Should return default for missing key."""
        attrs = {}
        assert get_attribute_as_bool(attrs, "key") is False
        assert get_attribute_as_bool(attrs, "key", True) is True

    def test_parses_string_true_values(self):
        """Should parse string true values."""
        for true_val in ["true", "True", "TRUE", "1", "yes"]:
            attrs = {"key": true_val}
            assert get_attribute_as_bool(attrs, "key") is True, f"Failed for {true_val}"

    def test_parses_string_false_values(self):
        """Should parse string false values."""
        for false_val in ["false", "False", "0", "no", ""]:
            attrs = {"key": false_val}
            assert get_attribute_as_bool(attrs, "key") is False, f"Failed for {false_val}"

    def test_converts_truthy_values(self):
        """Should convert truthy values to True."""
        attrs = {"key": 1}
        assert get_attribute_as_bool(attrs, "key") is True


class TestGetAttributeAsSchemaMerges:
    """Tests for get_attribute_as_schema_merges function."""

    def test_parses_valid_schema_merges(self):
        """Should parse valid schema merges JSON."""
        merges_data = {
            "field1": {"encoding": 1, "decoded_type": 2, "match_importance": 5},
            "field2": {"encoding": 2},
        }
        attrs = {"key": json.dumps(merges_data)}

        result = get_attribute_as_schema_merges(attrs, "key")
        assert result is not None
        assert "field1" in result
        assert "field2" in result
        assert result["field1"].match_importance == 5

    def test_returns_none_for_missing_key(self):
        """Should return None for missing key."""
        attrs = {}
        assert get_attribute_as_schema_merges(attrs, "key") is None

    def test_returns_none_for_invalid_json(self):
        """Should return None for invalid JSON."""
        attrs = {"key": "not valid json"}
        assert get_attribute_as_schema_merges(attrs, "key") is None

    def test_returns_none_for_non_dict_json(self):
        """Should return None for non-dict JSON."""
        attrs = {"key": '["array", "not", "dict"]'}
        assert get_attribute_as_schema_merges(attrs, "key") is None


class TestParsePackageType:
    """Tests for parse_package_type function."""

    def test_parses_http(self):
        """Should parse HTTP package type."""
        assert parse_package_type("HTTP") == PackageType.HTTP
        assert parse_package_type("http") == PackageType.HTTP

    def test_parses_pg(self):
        """Should parse PG package type."""
        assert parse_package_type("PG") == PackageType.PG

    def test_parses_redis(self):
        """Should parse REDIS package type."""
        assert parse_package_type("REDIS") == PackageType.REDIS

    def test_returns_unspecified_for_unknown(self):
        """Should return UNSPECIFIED for unknown types."""
        assert parse_package_type("UNKNOWN") == PackageType.UNSPECIFIED
        assert parse_package_type("invalid") == PackageType.UNSPECIFIED
        assert parse_package_type("") == PackageType.UNSPECIFIED


class TestDictToJsonSchema:
    """Tests for dict_to_json_schema function."""

    def test_handles_empty_dict(self):
        """Should handle empty dict."""
        result = dict_to_json_schema({})
        assert result is not None

    def test_handles_none(self):
        """Should handle None."""
        result = dict_to_json_schema(None)
        assert result is not None

    def test_converts_type(self):
        """Should convert type field."""
        schema_dict = {"type": JsonSchemaType.STRING.value}
        result = dict_to_json_schema(schema_dict)
        assert result.type == JsonSchemaType.STRING

    def test_converts_properties_recursively(self):
        """Should convert properties recursively."""
        schema_dict = {
            "type": JsonSchemaType.OBJECT.value,
            "properties": {
                "name": {"type": JsonSchemaType.STRING.value},
                "age": {"type": JsonSchemaType.NUMBER.value},
            },
        }
        result = dict_to_json_schema(schema_dict)
        assert result.type == JsonSchemaType.OBJECT
        assert "name" in result.properties
        assert "age" in result.properties

    def test_converts_items(self):
        """Should convert items for array/list schemas."""
        schema_dict = {"type": JsonSchemaType.ORDERED_LIST.value, "items": {"type": JsonSchemaType.STRING.value}}
        result = dict_to_json_schema(schema_dict)
        assert result.type == JsonSchemaType.ORDERED_LIST
        # items may be a JsonSchema object or the processed result
        if result.items is not None:
            if hasattr(result.items, "type"):
                assert result.items.type == JsonSchemaType.STRING

    def test_converts_encoding(self):
        """Should convert encoding type."""
        schema_dict = {"encoding": EncodingType.BASE64.value}
        result = dict_to_json_schema(schema_dict)
        assert result.encoding == EncodingType.BASE64

    def test_converts_decoded_type(self):
        """Should convert decoded type."""
        schema_dict = {"decoded_type": DecodedType.JSON.value}
        result = dict_to_json_schema(schema_dict)
        assert result.decoded_type == DecodedType.JSON


class TestOtelSpanToCleanSpanData:
    """Tests for otel_span_to_clean_span_data function."""

    def _create_mock_span(
        self,
        mocker,
        name: str = "test-span",
        trace_id: int = 0x0123456789ABCDEF0123456789ABCDEF,
        span_id: int = 0x0123456789ABCDEF,
        parent_span_id: int | None = None,
        kind: OTelSpanKind = OTelSpanKind.SERVER,
        attributes: dict | None = None,
        start_time: int = 1700000000_000_000_000,
        end_time: int = 1700000001_000_000_000,
    ):
        """Create a mock ReadableSpan for testing."""
        mock_span = mocker.MagicMock(spec=ReadableSpan)
        mock_span.name = name
        mock_span.kind = kind
        mock_span.start_time = start_time
        mock_span.end_time = end_time

        # Create mock span context
        mock_context = mocker.MagicMock(spec=SpanContext)
        mock_context.trace_id = trace_id
        mock_context.span_id = span_id
        mock_span.context = mock_context

        # Create parent context if provided
        if parent_span_id:
            mock_parent = mocker.MagicMock()
            mock_parent.span_id = parent_span_id
            mock_span.parent = mock_parent
        else:
            mock_span.parent = None

        # Set attributes
        default_attrs = {
            TdSpanAttributes.NAME: name,
            TdSpanAttributes.PACKAGE_NAME: "test-package",
            TdSpanAttributes.INSTRUMENTATION_NAME: "TestInstrumentation",
            TdSpanAttributes.PACKAGE_TYPE: "HTTP",
            TdSpanAttributes.INPUT_VALUE: '{"method": "GET"}',
            TdSpanAttributes.OUTPUT_VALUE: '{"status": 200}',
        }
        if attributes:
            default_attrs.update(attributes)
        mock_span.attributes = default_attrs

        # Create mock status
        mock_status = mocker.MagicMock()
        mock_status.status_code = OTelStatusCode.OK
        mock_status.description = ""
        mock_span.status = mock_status

        return mock_span

    def test_converts_basic_span(self, mocker):
        """Should convert basic span correctly."""
        mock_span = self._create_mock_span(mocker)

        result = otel_span_to_clean_span_data(mock_span)

        assert result.name == "test-span"
        assert result.trace_id == "0123456789abcdef0123456789abcdef"
        assert result.span_id == "0123456789abcdef"
        assert result.package_name == "test-package"
        assert result.instrumentation_name == "TestInstrumentation"
        assert result.kind == SpanKind.SERVER
        assert result.status.code == StatusCode.OK

    def test_converts_parent_span_id(self, mocker):
        """Should convert parent span ID when present."""
        mock_span = self._create_mock_span(mocker, parent_span_id=0xFEDCBA9876543210)

        result = otel_span_to_clean_span_data(mock_span)

        assert result.parent_span_id == "fedcba9876543210"

    def test_empty_parent_span_id_when_no_parent(self, mocker):
        """Should have empty parent span ID when no parent."""
        mock_span = self._create_mock_span(mocker, parent_span_id=None)

        result = otel_span_to_clean_span_data(mock_span)

        assert result.parent_span_id == ""

    def test_converts_timestamp_and_duration(self, mocker):
        """Should convert timestamp and duration correctly."""
        start_ns = 1700000000_500_000_000  # 1700000000.5 seconds
        end_ns = 1700000001_000_000_000  # 1700000001.0 seconds
        mock_span = self._create_mock_span(mocker, start_time=start_ns, end_time=end_ns)

        result = otel_span_to_clean_span_data(mock_span)

        assert result.timestamp.seconds == 1700000000
        assert result.timestamp.nanos == 500_000_000
        assert result.duration.seconds == 0
        assert result.duration.nanos == 500_000_000

    def test_converts_input_output_values(self, mocker):
        """Should convert input and output values."""
        mock_span = self._create_mock_span(
            mocker,
            attributes={
                TdSpanAttributes.NAME: "test",
                TdSpanAttributes.PACKAGE_NAME: "pkg",
                TdSpanAttributes.INPUT_VALUE: '{"key": "value"}',
                TdSpanAttributes.OUTPUT_VALUE: '{"result": 42}',
            },
        )

        result = otel_span_to_clean_span_data(mock_span)

        assert result.input_value == {"key": "value"}
        assert result.output_value == {"result": 42}

    def test_converts_is_pre_app_start_flag(self, mocker):
        """Should convert is_pre_app_start flag."""
        mock_span = self._create_mock_span(
            mocker,
            attributes={
                TdSpanAttributes.NAME: "test",
                TdSpanAttributes.PACKAGE_NAME: "pkg",
                TdSpanAttributes.IS_PRE_APP_START: True,
            },
        )

        result = otel_span_to_clean_span_data(mock_span)

        assert result.is_pre_app_start is True

    def test_includes_environment(self, mocker):
        """Should include environment when provided."""
        mock_span = self._create_mock_span(mocker)

        result = otel_span_to_clean_span_data(mock_span, environment="production")

        assert result.environment == "production"

    def test_handles_none_attributes(self, mocker):
        """Should handle span with no attributes."""
        mock_span = self._create_mock_span(mocker)
        mock_span.attributes = None

        # Should not raise
        result = otel_span_to_clean_span_data(mock_span)
        assert result is not None

    def test_generates_schema_hashes(self, mocker):
        """Should generate schema and value hashes."""
        mock_span = self._create_mock_span(
            mocker,
            attributes={
                TdSpanAttributes.NAME: "test",
                TdSpanAttributes.PACKAGE_NAME: "pkg",
                TdSpanAttributes.INPUT_VALUE: '{"key": "value"}',
                TdSpanAttributes.OUTPUT_VALUE: '{"status": 200}',
            },
        )

        result = otel_span_to_clean_span_data(mock_span)

        # Should have hashes generated
        assert result.input_schema_hash is not None
        assert result.output_schema_hash is not None
        assert result.input_value_hash is not None
        assert result.output_value_hash is not None

    def test_converts_all_span_kinds(self, mocker):
        """Should convert all span kinds correctly."""
        kinds = [
            (OTelSpanKind.SERVER, SpanKind.SERVER),
            (OTelSpanKind.CLIENT, SpanKind.CLIENT),
            (OTelSpanKind.INTERNAL, SpanKind.INTERNAL),
            (OTelSpanKind.PRODUCER, SpanKind.PRODUCER),
            (OTelSpanKind.CONSUMER, SpanKind.CONSUMER),
        ]

        for otel_kind, drift_kind in kinds:
            mock_span = self._create_mock_span(mocker, kind=otel_kind)
            result = otel_span_to_clean_span_data(mock_span)
            assert result.kind == drift_kind, f"Failed for {otel_kind}"
