import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tusk.drift.core.v1 import JsonSchemaType as ProtoJsonSchemaType

from drift.core.json_schema_helper import JsonSchema, JsonSchemaHelper, JsonSchemaType
from drift.core.types import (
    CleanSpanData,
    Duration,
    MetadataObject,
    PackageType,
    SpanKind,
    SpanStatus,
    StatusCode,
    Timestamp,
)


class SpanSerializationTests(unittest.TestCase):
    def test_basic_span_serializes_to_proto(self):
        input_schema_info = JsonSchemaHelper.generate_schema_and_hash(
            {"method": "GET", "path": "/health"}
        )
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
            metadata=MetadataObject(ENV_VARS={"APP_ENV": "test"}),
            input_value_hash=input_schema_info.decoded_value_hash,
            output_value_hash=output_schema_info.decoded_value_hash,
            input_schema_hash=input_schema_info.decoded_schema_hash,
            output_schema_hash=output_schema_info.decoded_schema_hash,
        )

        proto = span.to_proto()

        self.assertEqual(proto.trace_id, span.trace_id)
        self.assertEqual(proto.package_type.value, span.package_type.value)
        self.assertEqual(proto.kind.value, span.kind.value)
        self.assertEqual(proto.status.code.value, StatusCode.OK.value)
        self.assertEqual(proto.input_value["method"], "GET")
        self.assertEqual(proto.output_value["status_code"], 200)
        self.assertEqual(proto.metadata["ENV_VARS"]["APP_ENV"], "test")
        self.assertEqual(proto.timestamp.year, 2023)
        self.assertEqual(proto.duration.total_seconds(), 0.000001)

    def test_schema_serialization_matches_proto_enums(self):
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

        self.assertEqual(proto.input_schema.type, ProtoJsonSchemaType.OBJECT)
        self.assertEqual(proto.input_schema.properties["name"].type, ProtoJsonSchemaType.STRING)
        self.assertEqual(proto.input_schema.properties["count"].type, ProtoJsonSchemaType.NUMBER)


if __name__ == "__main__":
    unittest.main()
