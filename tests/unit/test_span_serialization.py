"""Tests for span serialization to protobuf."""

from tusk.drift.core.v1 import JsonSchemaType as ProtoJsonSchemaType

from drift.core.json_schema_helper import JsonSchema, JsonSchemaHelper, JsonSchemaType
from drift.core.types import (
    CleanSpanData,
    Duration,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
)


class TestSpanSerialization:
    """Tests for span serialization to protobuf."""

    def test_basic_span_serializes_to_proto(self):
        """Test that a basic span serializes to protobuf correctly."""
        input_schema_info = JsonSchemaHelper.generate_schema_and_hash({"method": "GET", "path": "/health"})
        output_schema_info = JsonSchemaHelper.generate_schema_and_hash({"status_code": 200})

        span = CleanSpanData(
            trace_id="0" * 32,
            span_id="1" * 16,
            parent_span_id="",
            name="GET /health",
            package_name="flask",
            instrumentation_name="FlaskInstrumentation",
            submodule_name="GET",
            package_type=PackageType.HTTP,
            kind=SpanKind.SERVER,
            input_value={"method": "GET", "path": "/health"},
            output_value={"status_code": 200},
            input_schema=input_schema_info.schema,
            output_schema=output_schema_info.schema,
            status=SpanStatus(code=StatusCode.OK, message=""),
            timestamp=Timestamp(seconds=1700000000, nanos=5),
            duration=Duration(seconds=0, nanos=1000),
            metadata=None,
            input_value_hash=input_schema_info.decoded_value_hash,
            output_value_hash=output_schema_info.decoded_value_hash,
            input_schema_hash=input_schema_info.decoded_schema_hash,
            output_schema_hash=output_schema_info.decoded_schema_hash,
        )

        proto = span.to_proto()

        assert proto.trace_id == span.trace_id
        assert span.package_type is not None
        assert proto.package_type == span.package_type.value
        assert proto.kind == span.kind.value
        assert proto.status.code == StatusCode.OK.value
        assert proto.input_value.fields["method"].string_value == "GET"
        assert proto.output_value.fields["status_code"].number_value == 200
        assert proto.timestamp.year == 2023
        assert proto.duration.total_seconds() == 0.000001

    def test_schema_serialization_matches_proto_enums(self):
        """Test that schema serialization matches protobuf enums."""
        schema = JsonSchema(
            type=JsonSchemaType.OBJECT,
            properties={
                "name": JsonSchema(type=JsonSchemaType.STRING),
                "count": JsonSchema(type=JsonSchemaType.NUMBER),
            },
        )

        span = CleanSpanData(
            trace_id="2" * 32,
            span_id="3" * 16,
            parent_span_id="",
            name="POST /echo",
            package_name="flask",
            instrumentation_name="FlaskInstrumentation",
            submodule_name="POST",
            package_type=PackageType.HTTP,
            kind=SpanKind.SERVER,
            input_value={},
            output_value={},
            input_schema=schema,
            status=SpanStatus(code=StatusCode.OK, message=""),
            timestamp=Timestamp(seconds=1, nanos=0),
            duration=Duration(seconds=0, nanos=1),
            input_value_hash="abc",
            output_value_hash="def",
            input_schema_hash="ghi",
            output_schema_hash="jkl",
        )

        proto = span.to_proto()

        assert proto.input_schema.type == ProtoJsonSchemaType.OBJECT
        assert proto.input_schema.properties["name"].type == ProtoJsonSchemaType.STRING
        assert proto.input_schema.properties["count"].type == ProtoJsonSchemaType.NUMBER
