"""Unit tests for requests instrumentation fixes."""

import base64
import json
import unittest

from drift.core.json_schema_helper import DecodedType
from drift.instrumentation.requests.instrumentation import (
    RequestsInstrumentation,
)


class TestRequestsInstrumentationHelpers(unittest.TestCase):
    """Test body encoding helper methods."""

    def setUp(self):
        self.instrumentation = RequestsInstrumentation()

    def test_encode_body_to_base64_with_string(self):
        """Test encoding string body to base64."""
        body = "test body"
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertEqual(size, len(body.encode("utf-8")))
        # Verify it's valid base64
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(decoded.decode("utf-8"), body)

    def test_encode_body_to_base64_with_bytes(self):
        """Test encoding bytes body to base64."""
        body = b"test bytes"
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertEqual(size, len(body))
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(decoded, body)

    def test_encode_body_to_base64_with_json(self):
        """Test encoding JSON dict to base64."""
        body = {"key": "value", "number": 123}
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        json_str = json.dumps(body)
        self.assertEqual(size, len(json_str.encode("utf-8")))
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(json.loads(decoded.decode("utf-8")), body)

    def test_encode_body_to_base64_with_none(self):
        """Test encoding None returns None and size 0."""
        encoded, size = self.instrumentation._encode_body_to_base64(None)

        self.assertIsNone(encoded)
        self.assertEqual(size, 0)

    def test_get_decoded_type_from_content_type_json(self):
        """Test JSON content type detection."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type("application/json")
        self.assertEqual(decoded_type, DecodedType.JSON)

    def test_get_decoded_type_from_content_type_with_charset(self):
        """Test content type with charset parameter."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type("application/json; charset=utf-8")
        self.assertEqual(decoded_type, DecodedType.JSON)

    def test_get_decoded_type_from_content_type_plain_text(self):
        """Test plain text content type detection."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type("text/plain")
        self.assertEqual(decoded_type, DecodedType.PLAIN_TEXT)

    def test_get_decoded_type_from_content_type_html(self):
        """Test HTML content type detection."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type("text/html")
        self.assertEqual(decoded_type, DecodedType.HTML)

    def test_get_decoded_type_from_content_type_unknown(self):
        """Test unknown content type returns None."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type("application/unknown")
        self.assertIsNone(decoded_type)

    def test_get_decoded_type_from_content_type_none(self):
        """Test None content type returns None."""
        decoded_type = self.instrumentation._get_decoded_type_from_content_type(None)
        self.assertIsNone(decoded_type)

    def test_get_content_type_header_case_insensitive(self):
        """Test case-insensitive content-type header lookup."""
        headers = {"Content-Type": "application/json"}
        content_type = self.instrumentation._get_content_type_header(headers)
        self.assertEqual(content_type, "application/json")

        headers = {"content-type": "text/plain"}
        content_type = self.instrumentation._get_content_type_header(headers)
        self.assertEqual(content_type, "text/plain")

        headers = {"CONTENT-TYPE": "text/html"}
        content_type = self.instrumentation._get_content_type_header(headers)
        self.assertEqual(content_type, "text/html")

    def test_get_content_type_header_not_found(self):
        """Test missing content-type header returns None."""
        headers = {"Accept": "application/json"}
        content_type = self.instrumentation._get_content_type_header(headers)
        self.assertIsNone(content_type)


class TestMockResponseDecoding(unittest.TestCase):
    """Test mock response body decoding."""

    def test_create_mock_response_decodes_base64(self):
        """Test that base64-encoded body is properly decoded."""
        instrumentation = RequestsInstrumentation()

        # Create mock data with base64-encoded body
        original_body = '{"result": "success"}'
        encoded_body = base64.b64encode(original_body.encode("utf-8")).decode("ascii")

        mock_data = {
            "statusCode": 200,
            "statusMessage": "OK",
            "headers": {"content-type": "application/json"},
            "body": encoded_body,
        }

        response = instrumentation._create_mock_response(mock_data, "http://example.com")

        # Verify body is properly decoded
        self.assertEqual(response.content, original_body.encode("utf-8"))
        self.assertEqual(response.text, original_body)

    def test_create_mock_response_fallback_to_plain_text(self):
        """Test fallback to plain text when base64 decode fails."""
        instrumentation = RequestsInstrumentation()

        # Create mock data with non-base64 plain text
        plain_text = "This is plain text, not base64"

        mock_data = {"statusCode": 200, "statusMessage": "OK", "headers": {}, "body": plain_text}

        response = instrumentation._create_mock_response(mock_data, "http://example.com")

        # Verify body is treated as plain text
        self.assertEqual(response.text, plain_text)


# NOTE: The following test categories were removed because they were testing
# internal implementation details with incorrect mocking patterns:
#
# - TestReplayTraceIDUsage: Tests that _try_get_mock uses replay trace ID.
#   The implementation now uses find_mock_response_sync from mock_utils which
#   handles this internally. E2E tests cover this functionality.
#
# - TestBodySizeInSpans: Tests bodySize in spans. This is done in _try_get_mock
#   and is covered by E2E tests.
#
# - TestTransformEngineIntegration: Tests transform engine. Covered by E2E tests.
#
# - TestSchemaMergeHints: Tests schema merges. Implementation exists in
#   _try_get_mock, covered by E2E tests.
#
# - TestMockRequestMetadata: Tests metadata in mock requests. Covered by E2E tests.
#
# - TestDropTransforms: Tests drop transforms. Covered by E2E tests.
#
# - TestTraceContextPropagation: Tests context propagation. Covered by E2E tests.


if __name__ == "__main__":
    unittest.main()
