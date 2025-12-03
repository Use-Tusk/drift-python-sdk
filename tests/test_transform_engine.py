import base64
import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from drift.instrumentation.http import HttpSpanData, HttpTransformEngine
from drift.core.types import SpanKind
from drift.instrumentation.http import transform_engine as te


class HttpTransformEngineTests(unittest.TestCase):
    def test_should_drop_inbound_request_and_sanitize_span(self) -> None:
        engine = HttpTransformEngine(
            [
                {
                    "matcher": {
                        "direction": "inbound",
                        "pathPattern": "/private/*",
                        "fullBody": True,
                    },
                    "action": {"type": "drop"},
                }
            ]
        )

        self.assertTrue(
            engine.should_drop_inbound_request("GET", "/private/123", {"Host": "example.com"})
        )

        span = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value={
                "method": "GET",
                "target": "/private/123",
                "headers": {"Host": "example.com"},
                "body": base64.b64encode(b"secret").decode("ascii"),
                "bodySize": 6,
            },
            output_value={"statusCode": 200, "body": base64.b64encode(b"reply").decode("ascii")},
        )

        metadata = engine.apply_transforms(span)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.actions[0].type, "drop")
        self.assertEqual(span.input_value["bodySize"], 0)
        self.assertEqual(span.output_value["bodySize"], 0)

    def test_jsonpath_mask_transform_updates_body_and_metadata(self) -> None:
        engine = HttpTransformEngine(
            [
                {
                    "matcher": {
                        "direction": "inbound",
                        "jsonPath": "$.password",
                    },
                    "action": {"type": "mask", "maskChar": "#"},
                }
            ]
        )

        body = json.dumps({"username": "alice", "password": "hunter2"}).encode("utf-8")
        span = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value={
                "method": "POST",
                "target": "/login",
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(body).decode("ascii"),
                "bodySize": len(body),
            },
            output_value={},
        )

        metadata = engine.apply_transforms(span)
        self.assertIsNotNone(metadata)
        self.assertTrue(metadata.transformed)
        self.assertTrue(metadata.actions[0].field.startswith("jsonPath"))

        masked_body = json.loads(base64.b64decode(span.input_value["body"].encode("ascii")))
        self.assertEqual(masked_body["password"], "#" * len("hunter2"))
        expected_size = len(json.dumps(masked_body, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(span.input_value["bodySize"], expected_size)

    def test_python_jsonpath_stub_is_used_when_available(self) -> None:
        class FakeJSONPath:
            def __init__(self, expression: str) -> None:
                self.expression = expression

            def find(self, data: Any) -> list[dict[str, Any]]:  # type: ignore[override]
                return [
                    {
                        "path": "$.password",
                        "value": data.get("password"),
                    }
                ]

        original_jsonpath = te.JSONPath
        te.JSONPath = FakeJSONPath  # type: ignore[assignment]
        try:
            engine = HttpTransformEngine(
                [
                    {
                        "matcher": {
                            "direction": "inbound",
                            "jsonPath": "$.password",
                        },
                        "action": {"type": "replace", "replaceWith": "redacted"},
                    }
                ]
            )
        finally:
            te.JSONPath = original_jsonpath  # type: ignore[assignment]

        body = json.dumps({"password": "secret", "username": "alice"}).encode("utf-8")
        span = HttpSpanData(
            kind=SpanKind.SERVER,
            input_value={
                "method": "POST",
                "target": "/login",
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(body).decode("ascii"),
                "bodySize": len(body),
            },
            output_value={},
        )

        metadata = engine.apply_transforms(span)
        self.assertIsNotNone(metadata)
        masked_body = json.loads(base64.b64decode(span.input_value["body"].encode("ascii")))
        self.assertEqual(masked_body["password"], "redacted")


if __name__ == "__main__":
    unittest.main()
