"""WSGI utility functions for HTTP request/response capture."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIEnvironment

from ...core.json_schema_helper import EncodingType, SchemaMerge

HEADER_SCHEMA_MERGES = {
    "headers": SchemaMerge(match_importance=0.0),
}

def build_url(environ: WSGIEnvironment) -> str:
    """Build full URL from WSGI environ.

    Args:
        environ: WSGI environ dictionary

    Returns:
        Full URL string (scheme + host + path + query)
    """
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "localhost")
    path = environ.get("PATH_INFO", "")
    query = environ.get("QUERY_STRING", "")

    url = f"{scheme}://{host}{path}"
    if query:
        url += f"?{query}"
    return url


def extract_headers(environ: WSGIEnvironment) -> dict[str, str]:
    """Extract HTTP headers from WSGI environ.

    WSGI stores HTTP headers as HTTP_* keys in environ (e.g., HTTP_CONTENT_TYPE).
    This function extracts them and converts to standard header format.

    Args:
        environ: WSGI environ dictionary

    Returns:
        Dictionary of HTTP headers
    """
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            # Convert HTTP_CONTENT_TYPE -> Content-Type
            header_name = key[5:].replace("_", "-").title()
            headers[header_name] = str(value)
    return headers


def capture_request_body(environ: WSGIEnvironment) -> bytes | None:
    """Capture request body from WSGI environ.

    Captures body for POST/PUT/PATCH requests.
    No truncation at capture time - span-level 1MB blocking at export handles oversized spans.
    Resets wsgi.input so the application can still read it.

    Args:
        environ: WSGI environ dictionary

    Returns:
        Request body bytes, or None if no body
    """
    if environ.get("REQUEST_METHOD") not in ("POST", "PUT", "PATCH"):
        return None

    try:
        content_length = int(environ.get("CONTENT_LENGTH", 0))
        if content_length > 0:
            wsgi_input = environ.get("wsgi.input")
            if wsgi_input:
                # Read full body (no truncation - span-level blocking handles oversized spans)
                body = wsgi_input.read(content_length)

                # Reset input for app to read
                from io import BytesIO

                environ["wsgi.input"] = BytesIO(body)

                return body
    except Exception:
        pass

    return None


def parse_status_line(status: str) -> tuple[int, str]:
    """Parse WSGI status line.

    Args:
        status: WSGI status string like "200 OK" or "404 Not Found"

    Returns:
        Tuple of (status_code, status_message)
    """
    status_parts = status.split(None, 1)
    status_code = int(status_parts[0])
    status_message = status_parts[1] if len(status_parts) > 1 else ""
    return status_code, status_message


def build_input_value(
    environ: WSGIEnvironment,
    body: bytes | None = None,
) -> dict[str, Any]:
    """Build standardized input_value dict from WSGI environ.

    Args:
        environ: WSGI environ dictionary
        body: Optional request body bytes

    Returns:
        Dictionary with HTTP request data (method, url, headers, body, etc.)
    """
    # Build target (path + query string) to match Node SDK
    path = environ.get("PATH_INFO", "")
    query_string = environ.get("QUERY_STRING", "")
    target = f"{path}?{query_string}" if query_string else path

    # Get HTTP version from SERVER_PROTOCOL (e.g., "HTTP/1.1" -> "1.1")
    server_protocol = environ.get("SERVER_PROTOCOL", "HTTP/1.1")
    http_version = server_protocol.replace("HTTP/", "") if server_protocol.startswith("HTTP/") else "1.1"

    input_value: dict[str, Any] = {
        "method": environ.get("REQUEST_METHOD", ""),
        "url": build_url(environ),
        "target": target,
        "headers": extract_headers(environ),
        "httpVersion": http_version,
        "remoteAddress": environ.get("REMOTE_ADDR"),
        "remotePort": int(port) if (port := environ.get("REMOTE_PORT")) else None,
    }

    # Remove None values
    input_value = {k: v for k, v in input_value.items() if v is not None}

    # Add body if present
    if body:
        # Store body as Base64 encoded string to match Node SDK behavior
        input_value["body"] = base64.b64encode(body).decode("ascii")
        input_value["bodySize"] = len(body)

    return input_value


def build_output_value(
    status_code: int,
    status_message: str,
    headers: dict[str, str],
    body: bytes | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build standardized output_value dict from response data.

    Args:
        status_code: HTTP status code (e.g., 200, 404)
        status_message: HTTP status message (e.g., "OK", "Not Found")
        headers: Response headers dictionary
        body: Optional response body bytes
        error: Optional error message

    Returns:
        Dictionary with HTTP response data (statusCode, headers, body, etc.)
    """
    output_value: dict[str, Any] = {
        "statusCode": status_code,  # camelCase to match Node SDK
        "statusMessage": status_message,
        "headers": headers,
    }

    # Add response body if captured
    if body:
        output_value["body"] = base64.b64encode(body).decode("ascii")
        output_value["bodySize"] = len(body)

    if error:
        output_value["errorMessage"] = error  # Match Node SDK field name

    return output_value


def build_input_schema_merges(input_value: dict[str, Any]) -> dict[str, Any]:
    """Build schema merge hints for HTTP input_value.

    This function creates schema merge metadata that will be used by the exporter
    to generate schemas at export time (not during request processing).

    Args:
        input_value: Input value dictionary

    Returns:
        Dictionary of schema merge hints (serializable to JSON)
    """
    # Build schema merge hints including body encoding
    input_schema_merges = dict(HEADER_SCHEMA_MERGES)
    if "body" in input_value:
        input_schema_merges["body"] = SchemaMerge(encoding=EncodingType.BASE64)

    # Convert to serializable dict format
    return _schema_merges_to_dict(input_schema_merges)


def build_output_schema_merges(output_value: dict[str, Any]) -> dict[str, Any]:
    """Build schema merge hints for HTTP output_value.

    This function creates schema merge metadata that will be used by the exporter
    to generate schemas at export time (not during request processing).

    Args:
        output_value: Output value dictionary

    Returns:
        Dictionary of schema merge hints (serializable to JSON)
    """
    # Build schema merge hints including body encoding
    output_schema_merges = dict(HEADER_SCHEMA_MERGES)
    if "body" in output_value:
        # Only set encoding, not decoded_type
        # The decoded_type causes the schema generator to decode and parse the body,
        # creating a schema for the parsed object instead of the encoded string.
        # The CLI will decode based on Content-Type headers during comparison.
        output_schema_merges["body"] = SchemaMerge(encoding=EncodingType.BASE64)

    # Convert to serializable dict format
    return _schema_merges_to_dict(output_schema_merges)


def _schema_merges_to_dict(schema_merges: dict[str, SchemaMerge]) -> dict[str, Any]:
    """Convert SchemaMerge objects to JSON-serializable dict.

    Args:
        schema_merges: Dictionary of SchemaMerge objects

    Returns:
        JSON-serializable dictionary
    """
    result = {}
    for key, merge in schema_merges.items():
        merge_dict = {}
        if merge.encoding is not None:
            merge_dict["encoding"] = merge.encoding.value
        if merge.decoded_type is not None:
            merge_dict["decoded_type"] = merge.decoded_type.value
        if merge.match_importance is not None:
            merge_dict["match_importance"] = merge.match_importance
        result[key] = merge_dict
    return result
