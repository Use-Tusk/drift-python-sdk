"""Tests for HTTP transform engine."""

import base64
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from drift.core.types import SpanKind
from drift.instrumentation.http import HttpSpanData, HttpTransformEngine
from drift.instrumentation.http import transform_engine as te


class TestHttpTransformEngine:
    """Tests for HttpTransformEngine."""

    def test_should_drop_inbound_request_and_sanitize_span(self) -> None:
        """Test dropping inbound requests and sanitizing the span."""
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

        assert engine.should_drop_inbound_request("GET", "/private/123", {"Host": "example.com"})

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
        assert metadata is not None
        assert span.input_value is not None
        assert span.output_value is not None
        assert metadata.actions[0].type == "drop"
        assert span.input_value["bodySize"] == 0
        assert span.output_value["bodySize"] == 0

    def test_jsonpath_mask_transform_updates_body_and_metadata(self) -> None:
        """Test JSONPath mask transform updates body and metadata."""
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
        assert metadata is not None
        assert span.input_value is not None
        assert metadata.transformed
        assert metadata.actions[0].field.startswith("jsonPath")

        masked_body = json.loads(base64.b64decode(span.input_value["body"].encode("ascii")))
        assert masked_body["password"] == "#" * len("hunter2")
        expected_size = len(json.dumps(masked_body, separators=(",", ":")).encode("utf-8"))
        assert span.input_value["bodySize"] == expected_size

    def test_python_jsonpath_stub_is_used_when_available(self) -> None:
        """Test that Python JSONPath stub is used when available."""

        class FakeJSONPath:
            def __init__(self, expression: str) -> None:
                self.expression = expression

            def find(self, data: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "path": "$.password",
                        "value": data.get("password"),
                    }
                ]

        original_jsonpath = te.JSONPath
        te.JSONPath = FakeJSONPath
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
            te.JSONPath = original_jsonpath

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
        assert metadata is not None
        assert span.input_value is not None
        masked_body = json.loads(base64.b64decode(span.input_value["body"].encode("ascii")))
        assert masked_body["password"] == "redacted"
