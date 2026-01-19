"""Content type utilities for binary/static payload blocking.

Provides comprehensive content type mapping matching Node SDK's httpBodyEncoder.ts
to enable blocking of traces with non-JSON/text responses (images, PDFs, videos, etc.).
"""

from __future__ import annotations

from .json_schema_helper import DecodedType

# Comprehensive content type mapping (40+ types matching Node SDK)
# Only JSON and PLAIN_TEXT are acceptable - all others will block traces
CONTENT_TYPE_MAPPING: dict[str, DecodedType] = {
    # JSON (ALLOWED)
    "application/json": DecodedType.JSON,
    "application/ld+json": DecodedType.JSON,
    "application/vnd.api+json": DecodedType.JSON,
    # Plain Text (ALLOWED)
    "text/plain": DecodedType.PLAIN_TEXT,
    # HTML
    "text/html": DecodedType.HTML,
    "application/xhtml+xml": DecodedType.HTML,
    # CSS (BLOCKED)
    "text/css": DecodedType.CSS,
    # JavaScript (BLOCKED)
    "text/javascript": DecodedType.JAVASCRIPT,
    "application/javascript": DecodedType.JAVASCRIPT,
    "application/x-javascript": DecodedType.JAVASCRIPT,
    "text/ecmascript": DecodedType.JAVASCRIPT,
    "application/ecmascript": DecodedType.JAVASCRIPT,
    # XML (BLOCKED)
    "text/xml": DecodedType.XML,
    "application/xml": DecodedType.XML,
    "application/rss+xml": DecodedType.XML,
    "application/atom+xml": DecodedType.XML,
    # SVG (BLOCKED)
    "image/svg+xml": DecodedType.SVG,
    # Structured Data (BLOCKED)
    "application/yaml": DecodedType.YAML,
    "text/yaml": DecodedType.YAML,
    "application/x-yaml": DecodedType.YAML,
    "text/x-yaml": DecodedType.YAML,
    "text/markdown": DecodedType.MARKDOWN,
    "text/x-markdown": DecodedType.MARKDOWN,
    "text/csv": DecodedType.CSV,
    "text/tab-separated-values": DecodedType.CSV,
    "application/sql": DecodedType.SQL,
    "application/graphql": DecodedType.GRAPHQL,
    # Form Data (BLOCKED)
    "application/x-www-form-urlencoded": DecodedType.FORM_DATA,
    "multipart/form-data": DecodedType.MULTIPART_FORM,
    # Documents (BLOCKED)
    "application/pdf": DecodedType.PDF,
    "application/msword": DecodedType.BINARY,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DecodedType.BINARY,
    "application/vnd.ms-excel": DecodedType.BINARY,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DecodedType.BINARY,
    "application/vnd.ms-powerpoint": DecodedType.BINARY,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": DecodedType.BINARY,
    # Archives (BLOCKED)
    "application/zip": DecodedType.ZIP,
    "application/x-zip-compressed": DecodedType.ZIP,
    "application/gzip": DecodedType.GZIP,
    "application/x-gzip": DecodedType.GZIP,
    "application/x-tar": DecodedType.BINARY,
    "application/x-7z-compressed": DecodedType.BINARY,
    "application/x-rar-compressed": DecodedType.BINARY,
    # Images (BLOCKED)
    "image/jpeg": DecodedType.JPEG,
    "image/jpg": DecodedType.JPEG,
    "image/png": DecodedType.PNG,
    "image/gif": DecodedType.GIF,
    "image/webp": DecodedType.WEBP,
    "image/bmp": DecodedType.JPEG,  # Using JPEG as generic image type
    "image/x-icon": DecodedType.JPEG,
    "image/vnd.microsoft.icon": DecodedType.JPEG,
    "image/tiff": DecodedType.JPEG,
    "image/x-tiff": DecodedType.JPEG,
    # Audio (BLOCKED)
    "audio/mpeg": DecodedType.AUDIO,
    "audio/mp3": DecodedType.AUDIO,
    "audio/mp4": DecodedType.AUDIO,
    "audio/wav": DecodedType.AUDIO,
    "audio/wave": DecodedType.AUDIO,
    "audio/x-wav": DecodedType.AUDIO,
    "audio/aac": DecodedType.AUDIO,
    "audio/ogg": DecodedType.AUDIO,
    "audio/webm": DecodedType.AUDIO,
    "audio/flac": DecodedType.AUDIO,
    # Video (BLOCKED)
    "video/mp4": DecodedType.VIDEO,
    "video/mpeg": DecodedType.VIDEO,
    "video/webm": DecodedType.VIDEO,
    "video/ogg": DecodedType.VIDEO,
    "video/quicktime": DecodedType.VIDEO,
    "video/x-msvideo": DecodedType.VIDEO,
    "video/x-flv": DecodedType.VIDEO,
    "video/3gpp": DecodedType.VIDEO,
    "video/3gpp2": DecodedType.VIDEO,
    # Fonts (BLOCKED)
    "font/woff": DecodedType.BINARY,
    "font/woff2": DecodedType.BINARY,
    "font/ttf": DecodedType.BINARY,
    "font/otf": DecodedType.BINARY,
    "application/font-woff": DecodedType.BINARY,
    "application/font-woff2": DecodedType.BINARY,
    "application/x-font-ttf": DecodedType.BINARY,
    "application/x-font-otf": DecodedType.BINARY,
    # Binary/Octet Stream (BLOCKED)
    "application/octet-stream": DecodedType.BINARY,
    "application/binary": DecodedType.BINARY,
}

ACCEPTABLE_DECODED_TYPES = {DecodedType.JSON, DecodedType.PLAIN_TEXT, DecodedType.HTML}


def get_decoded_type(content_type: str | None) -> DecodedType | None:
    """
    Parse Content-Type header and return decoded type.

    Handles:
    - Case-insensitive matching
    - Strips charset/boundary parameters (e.g., "application/json; charset=utf-8")
    - Returns None for unknown/unmapped content types

    Args:
        content_type: Content-Type header value (e.g., "application/json; charset=utf-8")

    Returns:
        DecodedType enum value, or None if not mapped

    Examples:
        >>> get_decoded_type("application/json; charset=utf-8")
        DecodedType.JSON
        >>> get_decoded_type("image/png")
        DecodedType.PNG
        >>> get_decoded_type("application/custom-thing")
        None
    """
    if not content_type:
        return None

    # Extract main type (before semicolon), lowercase, and strip whitespace
    main_type = content_type.lower().split(";")[0].strip()
    return CONTENT_TYPE_MAPPING.get(main_type)


def should_block_content_type(decoded_type: DecodedType | None) -> bool:
    """
    Check if content type should block the trace.

    Blocking logic (matches Node SDK):
    - JSON and PLAIN_TEXT: Allowed (not blocked)
    - All other mapped types (HTML, images, PDFs, videos, etc.): Blocked
    - Unknown/unmapped types: Allowed (not blocked)

    Args:
        decoded_type: Decoded content type from get_decoded_type()

    Returns:
        True if trace should be blocked, False otherwise

    Examples:
        >>> should_block_content_type(DecodedType.JSON)
        False  # Allowed
        >>> should_block_content_type(DecodedType.PNG)
        True  # Blocked
        >>> should_block_content_type(None)
        False  # Unknown types allowed
    """
    if decoded_type is None:
        # Unknown content types are allowed (not blocked)
        # This ensures we don't block custom/proprietary content types
        return False

    # Block if not in acceptable types
    return decoded_type not in ACCEPTABLE_DECODED_TYPES
