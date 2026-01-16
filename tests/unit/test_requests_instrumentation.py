"""Unit tests for requests instrumentation fixes."""

import base64
import json

import pytest

from drift.core.json_schema_helper import DecodedType
from drift.instrumentation.requests.instrumentation import (
    RequestsInstrumentation,
)


class TestRequestsInstrumentationHelpers:
    """Test body encoding helper methods."""

    @pytest.fixture
    def instrumentation(self):
        """Create instrumentation instance for testing."""
        return RequestsInstrumentation()

    def test_encode_body_to_base64_with_string(self, instrumentation):
        """Test encoding string body to base64."""
        body = "test body"
        encoded, size = instrumentation._encode_body_to_base64(body)

        assert encoded is not None
        assert size == len(body.encode("utf-8"))
        decoded = base64.b64decode(encoded.encode("ascii"))
        assert decoded.decode("utf-8") == body

    def test_encode_body_to_base64_with_bytes(self, instrumentation):
        """Test encoding bytes body to base64."""
        body = b"test bytes"
        encoded, size = instrumentation._encode_body_to_base64(body)

        assert encoded is not None
        assert size == len(body)
        decoded = base64.b64decode(encoded.encode("ascii"))
        assert decoded == body

    def test_encode_body_to_base64_with_json(self, instrumentation):
        """Test encoding JSON dict to base64."""
        body = {"key": "value", "number": 123}
        encoded, size = instrumentation._encode_body_to_base64(body)

        assert encoded is not None
        json_str = json.dumps(body)
        assert size == len(json_str.encode("utf-8"))
        decoded = base64.b64decode(encoded.encode("ascii"))
        assert json.loads(decoded.decode("utf-8")) == body

    def test_encode_body_to_base64_with_none(self, instrumentation):
        """Test encoding None returns None and size 0."""
        encoded, size = instrumentation._encode_body_to_base64(None)

        assert encoded is None
        assert size == 0

    def test_get_decoded_type_from_content_type_json(self, instrumentation):
        """Test JSON content type detection."""
        decoded_type = instrumentation._get_decoded_type_from_content_type("application/json")
        assert decoded_type == DecodedType.JSON

    def test_get_decoded_type_from_content_type_with_charset(self, instrumentation):
        """Test content type with charset parameter."""
        decoded_type = instrumentation._get_decoded_type_from_content_type("application/json; charset=utf-8")
        assert decoded_type == DecodedType.JSON

    def test_get_decoded_type_from_content_type_plain_text(self, instrumentation):
        """Test plain text content type detection."""
        decoded_type = instrumentation._get_decoded_type_from_content_type("text/plain")
        assert decoded_type == DecodedType.PLAIN_TEXT

    def test_get_decoded_type_from_content_type_html(self, instrumentation):
        """Test HTML content type detection."""
        decoded_type = instrumentation._get_decoded_type_from_content_type("text/html")
        assert decoded_type == DecodedType.HTML

    def test_get_decoded_type_from_content_type_unknown(self, instrumentation):
        """Test unknown content type returns None."""
        decoded_type = instrumentation._get_decoded_type_from_content_type("application/unknown")
        assert decoded_type is None

    def test_get_decoded_type_from_content_type_none(self, instrumentation):
        """Test None content type returns None."""
        decoded_type = instrumentation._get_decoded_type_from_content_type(None)
        assert decoded_type is None

    def test_get_content_type_header_case_insensitive(self, instrumentation):
        """Test case-insensitive content-type header lookup."""
        headers = {"Content-Type": "application/json"}
        content_type = instrumentation._get_content_type_header(headers)
        assert content_type == "application/json"

        headers = {"content-type": "text/plain"}
        content_type = instrumentation._get_content_type_header(headers)
        assert content_type == "text/plain"

        headers = {"CONTENT-TYPE": "text/html"}
        content_type = instrumentation._get_content_type_header(headers)
        assert content_type == "text/html"

    def test_get_content_type_header_not_found(self, instrumentation):
        """Test missing content-type header returns None."""
        headers = {"Accept": "application/json"}
        content_type = instrumentation._get_content_type_header(headers)
        assert content_type is None


class TestMockResponseDecoding:
    """Test mock response body decoding."""

    def test_create_mock_response_decodes_base64(self):
        """Test that base64-encoded body is properly decoded."""
        instrumentation = RequestsInstrumentation()

        original_body = '{"result": "success"}'
        encoded_body = base64.b64encode(original_body.encode("utf-8")).decode("ascii")

        mock_data = {
            "statusCode": 200,
            "statusMessage": "OK",
            "headers": {"content-type": "application/json"},
            "body": encoded_body,
        }

        response = instrumentation._create_mock_response(mock_data, "http://example.com")

        assert response.content == original_body.encode("utf-8")
        assert response.text == original_body

    def test_create_mock_response_fallback_to_plain_text(self):
        """Test fallback to plain text when base64 decode fails."""
        instrumentation = RequestsInstrumentation()

        plain_text = "This is plain text, not base64"

        mock_data = {"statusCode": 200, "statusMessage": "OK", "headers": {}, "body": plain_text}

        response = instrumentation._create_mock_response(mock_data, "http://example.com")

        assert response.text == plain_text
