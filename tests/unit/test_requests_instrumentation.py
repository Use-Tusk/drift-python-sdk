"""Unit tests for requests instrumentation fixes."""

import base64
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from drift.instrumentation.requests.instrumentation import RequestsInstrumentation
from drift.core.json_schema_helper import EncodingType, DecodedType


class TestRequestsInstrumentationHelpers(unittest.TestCase):
    """Test body encoding helper methods."""

    def setUp(self):
        self.instrumentation = RequestsInstrumentation()

    def test_encode_body_to_base64_with_string(self):
        """Test encoding string body to base64."""
        body = "test body"
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
        self.assertEqual(size, len(body.encode("utf-8")))
        # Verify it's valid base64
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(decoded.decode("utf-8"), body)

    def test_encode_body_to_base64_with_bytes(self):
        """Test encoding bytes body to base64."""
        body = b"test bytes"
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
        self.assertEqual(size, len(body))
        decoded = base64.b64decode(encoded.encode("ascii"))
        self.assertEqual(decoded, body)

    def test_encode_body_to_base64_with_json(self):
        """Test encoding JSON dict to base64."""
        body = {"key": "value", "number": 123}
        encoded, size = self.instrumentation._encode_body_to_base64(body)

        self.assertIsNotNone(encoded)
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


class TestReplayTraceIDUsage(unittest.TestCase):
    """Test replay trace ID context usage in mock requests."""

    @patch('drift.instrumentation.requests.instrumentation.replay_trace_id_context')
    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    def test_try_get_mock_uses_replay_trace_id(self, mock_tusk_drift, mock_replay_context):
        """Test that _try_get_mock uses replay trace ID from context."""
        # Setup
        instrumentation = RequestsInstrumentation()
        replay_trace_id = "recorded-trace-id-12345"
        mock_replay_context.get.return_value = replay_trace_id

        mock_sdk = MagicMock()
        mock_sdk.request_mock_sync.return_value = Mock(found=False)

        # Call _try_get_mock
        result = instrumentation._try_get_mock(
            mock_sdk,
            "GET",
            "http://example.com/api",
            trace_id="new-random-trace-id",  # This should NOT be used
            span_id="span-123",
            stack_trace="",
        )

        # Verify replay trace ID was retrieved from context
        mock_replay_context.get.assert_called_once()

        # Verify test_id in mock request matches replay trace ID, not random trace ID
        mock_sdk.request_mock_sync.assert_called_once()
        call_args = mock_sdk.request_mock_sync.call_args[0][0]
        self.assertEqual(call_args.test_id, replay_trace_id)


class TestBodySizeInSpans(unittest.TestCase):
    """Test that bodySize field is included when body exists."""

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    def test_create_span_includes_bodysize_for_request(self, mock_tusk_drift):
        """Test that request bodySize is included when body exists."""
        instrumentation = RequestsInstrumentation()
        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Create a mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"content-type": "application/json"}
        mock_response.content = b'{"result": "success"}'

        # Call _create_span with a request body
        request_kwargs = {
            "headers": {"content-type": "application/json"},
            "params": {},
            "json": {"test": "data"}
        }

        instrumentation._create_span(
            mock_sdk,
            "POST",
            "http://example.com/api",
            "trace-123",
            "span-456",
            None,  # parent_span_id
            100.0,
            mock_response,
            None,
            request_kwargs
        )

        # Verify span was collected
        mock_sdk.collect_span.assert_called_once()
        span = mock_sdk.collect_span.call_args[0][0]

        # Verify bodySize is present in input_value
        self.assertIn("bodySize", span.input_value)
        self.assertGreater(span.input_value["bodySize"], 0)

        # Verify bodySize is present in output_value
        self.assertIn("bodySize", span.output_value)
        self.assertGreater(span.output_value["bodySize"], 0)

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    def test_create_span_no_bodysize_without_body(self, mock_tusk_drift):
        """Test that bodySize is not included when no body exists."""
        instrumentation = RequestsInstrumentation()
        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Create a mock response with no body
        mock_response = Mock()
        mock_response.status_code = 204
        mock_response.reason = "No Content"
        mock_response.headers = {}
        mock_response.content = b''

        # Call _create_span with no request body
        request_kwargs = {
            "headers": {},
            "params": {},
        }

        instrumentation._create_span(
            mock_sdk,
            "GET",
            "http://example.com/api",
            "trace-123",
            "span-456",
            None,  # parent_span_id
            50.0,
            mock_response,
            None,
            request_kwargs
        )

        # Verify span was collected
        mock_sdk.collect_span.assert_called_once()
        span = mock_sdk.collect_span.call_args[0][0]

        # Verify body and bodySize are not in input_value (no request body)
        self.assertNotIn("body", span.input_value)
        self.assertNotIn("bodySize", span.input_value)


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
            "body": encoded_body
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

        mock_data = {
            "statusCode": 200,
            "statusMessage": "OK",
            "headers": {},
            "body": plain_text
        }

        response = instrumentation._create_mock_response(mock_data, "http://example.com")

        # Verify body is treated as plain text
        self.assertEqual(response.text, plain_text)


class TestTransformEngineIntegration(unittest.TestCase):
    """Test transform engine integration."""

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    def test_create_span_applies_transforms(self, mock_tusk_drift):
        """Test that transforms are applied to span data."""
        # Create instrumentation with mocked transform engine
        instrumentation = RequestsInstrumentation()
        mock_transform_engine = Mock()
        mock_transform_engine.apply_transforms = Mock()
        instrumentation._transform_engine = mock_transform_engine

        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Create a mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.content = b'test'

        # Call _create_span
        request_kwargs = {
            "headers": {},
            "params": {},
            "data": "test data"
        }

        instrumentation._create_span(
            mock_sdk,
            "POST",
            "http://example.com/api",
            "trace-123",
            "span-456",
            None,  # parent_span_id
            100.0,
            mock_response,
            None,
            request_kwargs
        )

        # Verify apply_transforms was called
        mock_transform_engine.apply_transforms.assert_called_once()

        # Verify the span data passed to apply_transforms
        call_args = mock_transform_engine.apply_transforms.call_args[0][0]
        from drift.core.types import SpanKind
        self.assertEqual(call_args.kind, SpanKind.CLIENT)
        self.assertIsNotNone(call_args.input_value)
        self.assertIsNotNone(call_args.output_value)


class TestSchemaMergeHints(unittest.TestCase):
    """Test schema merge hints for base64 encoding."""

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    @patch('drift.instrumentation.requests.instrumentation.JsonSchemaHelper')
    def test_schema_merges_include_base64_encoding(self, mock_schema_helper, mock_tusk_drift):
        """Test that schema merges include BASE64 encoding hint."""
        instrumentation = RequestsInstrumentation()
        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Mock schema helper to capture merge hints
        mock_schema_result = Mock()
        mock_schema_result.schema = {}
        mock_schema_result.decoded_schema_hash = "hash1"
        mock_schema_result.decoded_value_hash = "hash2"
        mock_schema_helper.generate_schema_and_hash.return_value = mock_schema_result

        # Create a mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"content-type": "application/json"}
        mock_response.content = b'{"result": "ok"}'

        # Call _create_span with JSON body
        request_kwargs = {
            "headers": {"content-type": "application/json"},
            "params": {},
            "json": {"test": "data"}
        }

        instrumentation._create_span(
            mock_sdk,
            "POST",
            "http://example.com/api",
            "trace-123",
            "span-456",
            None,  # parent_span_id
            100.0,
            mock_response,
            None,
            request_kwargs
        )

        # Verify generate_schema_and_hash was called twice (input and output)
        self.assertEqual(mock_schema_helper.generate_schema_and_hash.call_count, 2)

        # Check input schema merges
        input_call = mock_schema_helper.generate_schema_and_hash.call_args_list[0]
        input_merges = input_call[0][1]  # Second argument

        # Verify headers have match_importance=0.0
        self.assertIn("headers", input_merges)
        self.assertEqual(input_merges["headers"].match_importance, 0.0)

        # Verify body has BASE64 encoding
        self.assertIn("body", input_merges)
        self.assertEqual(input_merges["body"].encoding, EncodingType.BASE64)
        self.assertEqual(input_merges["body"].decoded_type, DecodedType.JSON)


class TestTraceContextPropagation(unittest.TestCase):
    """Test trace context propagation from parent to child spans."""

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    @patch('drift.instrumentation.requests.instrumentation.current_trace_id_context')
    @patch('drift.instrumentation.requests.instrumentation.current_span_id_context')
    def test_child_span_inherits_parent_trace_id(self, mock_span_context, mock_trace_context, mock_tusk_drift):
        """Test that CLIENT span inherits trace_id from parent context."""
        # Setup - simulate parent span context
        parent_trace_id = "parent-trace-abc123"
        parent_span_id = "parent-span-xyz789"
        mock_trace_context.get.return_value = parent_trace_id
        mock_span_context.get.return_value = parent_span_id

        instrumentation = RequestsInstrumentation()
        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Create mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.content = b'test'

        # Call _create_span
        instrumentation._create_span(
            mock_sdk,
            "GET",
            "http://api.example.com/users",
            parent_trace_id,  # trace_id should match parent
            "new-span-123",   # new span_id for this child
            parent_span_id,   # parent_span_id from context
            50.0,
            mock_response,
            None,
            {}
        )

        # Verify span was created with parent context
        mock_sdk.collect_span.assert_called_once()
        span = mock_sdk.collect_span.call_args[0][0]

        # Verify trace_id matches parent
        self.assertEqual(span.trace_id, parent_trace_id)
        # Verify parent_span_id is set
        self.assertEqual(span.parent_span_id, parent_span_id)
        # Verify span_id is new (not parent's)
        self.assertEqual(span.span_id, "new-span-123")

    @patch('drift.instrumentation.requests.instrumentation.TuskDrift')
    @patch('drift.instrumentation.requests.instrumentation.current_trace_id_context')
    @patch('drift.instrumentation.requests.instrumentation.current_span_id_context')
    def test_root_span_has_no_parent(self, mock_span_context, mock_trace_context, mock_tusk_drift):
        """Test that root span (no parent context) has parent_span_id=None."""
        # Setup - no parent context
        mock_trace_context.get.return_value = None
        mock_span_context.get.return_value = None

        instrumentation = RequestsInstrumentation()
        mock_sdk = MagicMock()
        mock_sdk.app_ready = True

        # Create mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.content = b'test'

        # Call _create_span for root span
        root_trace_id = "root-trace-123"
        root_span_id = "root-span-456"

        instrumentation._create_span(
            mock_sdk,
            "GET",
            "http://api.example.com/users",
            root_trace_id,
            root_span_id,
            None,  # No parent for root span
            50.0,
            mock_response,
            None,
            {}
        )

        # Verify span was created as root
        mock_sdk.collect_span.assert_called_once()
        span = mock_sdk.collect_span.call_args[0][0]

        # Verify it's a root span (no parent)
        self.assertIsNone(span.parent_span_id)
        self.assertEqual(span.trace_id, root_trace_id)
        self.assertEqual(span.span_id, root_span_id)


if __name__ == "__main__":
    unittest.main()
