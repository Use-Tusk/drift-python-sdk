"""Unit tests for WSGI utilities."""

import base64
import unittest

from drift.instrumentation.wsgi import (
    build_input_schema_merges,
    build_input_value,
    build_output_schema_merges,
    build_output_value,
    build_url,
    capture_request_body,
    extract_headers,
    parse_status_line,
)


class TestBuildUrl(unittest.TestCase):
    """Test build_url function."""

    def test_builds_basic_url(self):
        """Test basic URL building."""
        environ = {
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "example.com",
            "PATH_INFO": "/users",
            "QUERY_STRING": "",
        }
        url = build_url(environ)
        self.assertEqual(url, "http://example.com/users")

    def test_builds_url_with_query_string(self):
        """Test URL building with query string."""
        environ = {
            "wsgi.url_scheme": "https",
            "HTTP_HOST": "example.com",
            "PATH_INFO": "/search",
            "QUERY_STRING": "q=test&limit=10",
        }
        url = build_url(environ)
        self.assertEqual(url, "https://example.com/search?q=test&limit=10")

    def test_uses_server_name_fallback(self):
        """Test fallback to SERVER_NAME when HTTP_HOST is missing."""
        environ = {
            "wsgi.url_scheme": "http",
            "SERVER_NAME": "localhost",
            "PATH_INFO": "/",
            "QUERY_STRING": "",
        }
        url = build_url(environ)
        self.assertEqual(url, "http://localhost/")

    def test_defaults_to_http(self):
        """Test default scheme is http."""
        environ = {
            "HTTP_HOST": "example.com",
            "PATH_INFO": "/test",
        }
        url = build_url(environ)
        self.assertTrue(url.startswith("http://"))


class TestExtractHeaders(unittest.TestCase):
    """Test extract_headers function."""

    def test_extracts_http_headers(self):
        """Test extraction of HTTP headers."""
        environ = {
            "HTTP_CONTENT_TYPE": "application/json",
            "HTTP_AUTHORIZATION": "Bearer token123",
            "HTTP_X_CUSTOM_HEADER": "custom-value",
            "REQUEST_METHOD": "GET",  # Should be ignored
        }
        headers = extract_headers(environ)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer token123")
        self.assertEqual(headers["X-Custom-Header"], "custom-value")
        self.assertNotIn("Request-Method", headers)

    def test_handles_empty_environ(self):
        """Test with empty environ."""
        headers = extract_headers({})
        self.assertEqual(headers, {})


class TestCaptureRequestBody(unittest.TestCase):
    """Test capture_request_body function."""

    def test_captures_post_body(self):
        """Test capturing POST request body."""
        from io import BytesIO

        body_content = b'{"key": "value"}'
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body_content)),
            "wsgi.input": BytesIO(body_content),
        }
        body = capture_request_body(environ)
        self.assertEqual(body, body_content)

        # Verify input was reset
        from io import BytesIO

        wsgi_input = environ["wsgi.input"]
        assert isinstance(wsgi_input, BytesIO)
        new_body = wsgi_input.read()
        self.assertEqual(new_body, body_content)

    def test_captures_large_body(self):
        """Test capturing large body (no truncation at capture time)."""
        from io import BytesIO

        body_content = b"x" * 15000
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body_content)),
            "wsgi.input": BytesIO(body_content),
        }
        body = capture_request_body(environ)
        # No truncation - span-level blocking handles oversized spans
        assert body is not None
        self.assertEqual(len(body), 15000)

    def test_ignores_get_requests(self):
        """Test that GET requests are ignored."""
        environ = {
            "REQUEST_METHOD": "GET",
        }
        body = capture_request_body(environ)
        self.assertIsNone(body)

    def test_handles_empty_body(self):
        """Test handling of empty body."""
        from io import BytesIO

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": "0",
            "wsgi.input": BytesIO(b""),
        }
        body = capture_request_body(environ)
        self.assertIsNone(body)


class TestParseStatusLine(unittest.TestCase):
    """Test parse_status_line function."""

    def test_parses_standard_status(self):
        """Test parsing standard status line."""
        code, message = parse_status_line("200 OK")
        self.assertEqual(code, 200)
        self.assertEqual(message, "OK")

    def test_parses_status_with_long_message(self):
        """Test parsing status with multi-word message."""
        code, message = parse_status_line("404 Not Found")
        self.assertEqual(code, 404)
        self.assertEqual(message, "Not Found")

    def test_handles_status_without_message(self):
        """Test parsing status without message."""
        code, message = parse_status_line("500")
        self.assertEqual(code, 500)
        self.assertEqual(message, "")


class TestBuildInputValue(unittest.TestCase):
    """Test build_input_value function."""

    def test_builds_basic_input_value(self):
        """Test building basic input value."""
        environ = {
            "REQUEST_METHOD": "GET",
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "example.com",
            "PATH_INFO": "/users",
            "QUERY_STRING": "",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "192.168.1.1",
        }
        input_value = build_input_value(environ)
        self.assertEqual(input_value["method"], "GET")
        self.assertEqual(input_value["url"], "http://example.com/users")
        self.assertEqual(input_value["target"], "/users")
        self.assertEqual(input_value["httpVersion"], "1.1")
        self.assertEqual(input_value["remoteAddress"], "192.168.1.1")

    def test_includes_body_when_present(self):
        """Test including body in input value."""
        environ = {
            "REQUEST_METHOD": "POST",
            "wsgi.url_scheme": "http",
            "HTTP_HOST": "example.com",
            "PATH_INFO": "/api",
            "QUERY_STRING": "",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        body = b'{"key": "value"}'
        input_value = build_input_value(environ, body=body)
        self.assertIn("body", input_value)
        self.assertEqual(input_value["body"], base64.b64encode(body).decode("ascii"))
        self.assertEqual(input_value["bodySize"], len(body))


class TestBuildOutputValue(unittest.TestCase):
    """Test build_output_value function."""

    def test_builds_basic_output_value(self):
        """Test building basic output value."""
        output_value = build_output_value(200, "OK", {"Content-Type": "application/json"})
        self.assertEqual(output_value["statusCode"], 200)
        self.assertEqual(output_value["statusMessage"], "OK")
        self.assertEqual(output_value["headers"]["Content-Type"], "application/json")

    def test_includes_body_when_present(self):
        """Test including body in output value."""
        body = b'{"result": "success"}'
        output_value = build_output_value(200, "OK", {}, body=body)
        self.assertIn("body", output_value)
        self.assertEqual(output_value["body"], base64.b64encode(body).decode("ascii"))
        self.assertEqual(output_value["bodySize"], len(body))

    def test_includes_error_when_present(self):
        """Test including error in output value."""
        output_value = build_output_value(500, "Internal Server Error", {}, error="Database connection failed")
        self.assertEqual(output_value["errorMessage"], "Database connection failed")


class TestBuildSchemaMerges(unittest.TestCase):
    """Test schema merge builder functions."""

    def test_builds_input_schema_merges(self):
        """Test input schema merge building."""
        input_value = {
            "method": "GET",
            "url": "http://example.com/test",
            "headers": {"Content-Type": "application/json"},
        }
        schema_merges = build_input_schema_merges(input_value)

        # Should have headers merge
        self.assertIn("headers", schema_merges)
        self.assertEqual(schema_merges["headers"]["match_importance"], 0.0)

        # Should not have body merge (no body present)
        self.assertNotIn("body", schema_merges)

    def test_builds_input_schema_merges_with_body(self):
        """Test input schema merge building with body."""
        input_value = {
            "method": "POST",
            "url": "http://example.com/api",
            "body": base64.b64encode(b"test data").decode("ascii"),
        }
        schema_merges = build_input_schema_merges(input_value)

        # Should have body merge with BASE64 encoding
        self.assertIn("body", schema_merges)
        self.assertEqual(schema_merges["body"]["encoding"], 1)  # BASE64 = 1

    def test_builds_output_schema_merges(self):
        """Test output schema merge building."""
        output_value = {
            "statusCode": 200,
            "statusMessage": "OK",
            "headers": {"Content-Type": "application/json"},
        }
        schema_merges = build_output_schema_merges(output_value)

        # Should have headers merge
        self.assertIn("headers", schema_merges)
        self.assertEqual(schema_merges["headers"]["match_importance"], 0.0)

    def test_builds_output_schema_merges_with_body(self):
        """Test output schema merge building with body."""
        output_value = {
            "statusCode": 200,
            "statusMessage": "OK",
            "body": base64.b64encode(b"response").decode("ascii"),
        }
        schema_merges = build_output_schema_merges(output_value)

        # Should have body merge with BASE64 encoding
        self.assertIn("body", schema_merges)
        self.assertEqual(schema_merges["body"]["encoding"], 1)  # BASE64 = 1


if __name__ == "__main__":
    unittest.main()
