"""Unit tests for Django HTML utilities.

Tests for HTML normalization functions including CSRF token and class ordering.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from drift.instrumentation.django.html_utils import (
    CSRF_PLACEHOLDER,
    normalize_csrf_in_body,
    normalize_html_body,
    normalize_html_class_ordering,
    normalize_html_response,
)


class TestNormalizeHtmlClassOrdering:
    """Tests for normalize_html_class_ordering function."""

    def test_sorts_classes_alphabetically(self):
        """Classes within a class attribute should be sorted alphabetically."""
        html = b'<div class="zebra apple banana">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="apple banana zebra">content</div>'

    def test_handles_multiple_class_attributes(self):
        """Multiple class attributes in the same HTML should all be normalized."""
        html = b'<div class="c b a"><span class="z y x">text</span></div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="a b c"><span class="x y z">text</span></div>'

    def test_handles_single_quotes(self):
        """Class attributes with single quotes should be handled."""
        html = b"<div class='zebra apple banana'>content</div>"
        result = normalize_html_class_ordering(html)
        assert result == b"<div class='apple banana zebra'>content</div>"

    def test_preserves_single_class(self):
        """Single class should remain unchanged."""
        html = b'<div class="only-one">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="only-one">content</div>'

    def test_handles_empty_class_attribute(self):
        """Empty class attribute should remain unchanged."""
        html = b'<div class="">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="">content</div>'

    def test_preserves_other_attributes(self):
        """Other attributes should not be affected."""
        html = b'<div id="my-id" class="c b a" data-value="test">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div id="my-id" class="a b c" data-value="test">content</div>'

    def test_handles_none_input(self):
        """None input should return None."""
        assert normalize_html_class_ordering(None) is None

    def test_handles_empty_bytes(self):
        """Empty bytes should return empty bytes."""
        assert normalize_html_class_ordering(b"") == b""

    def test_handles_html_without_class_attributes(self):
        """HTML without class attributes should remain unchanged."""
        html = b"<div id='test'><p>Hello</p></div>"
        result = normalize_html_class_ordering(html)
        assert result == html

    def test_case_insensitive_class_attribute(self):
        """CLASS (uppercase) should also be handled."""
        html = b'<div CLASS="c b a">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div CLASS="a b c">content</div>'

    def test_handles_extra_whitespace_between_classes(self):
        """Extra whitespace between classes should be normalized to single space."""
        html = b'<div class="c   b    a">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="a b c">content</div>'

    def test_handles_tailwind_style_classes(self):
        """Tailwind-style classes with special characters should be sorted correctly."""
        html = b'<div class="text-lg hover:bg-blue-500 bg-white p-4">content</div>'
        result = normalize_html_class_ordering(html)
        assert result == b'<div class="bg-white hover:bg-blue-500 p-4 text-lg">content</div>'

    def test_handles_non_utf8_gracefully(self):
        """Non-UTF8 content should be returned unchanged."""
        # Invalid UTF-8 sequence
        html = b"\xff\xfe invalid utf8"
        result = normalize_html_class_ordering(html)
        assert result == html


class TestNormalizeCsrfInBody:
    """Tests for normalize_csrf_in_body function."""

    def test_normalizes_csrf_token_name_before_value(self):
        """CSRF token with name before value should be normalized."""
        html = b'<input type="hidden" name="csrfmiddlewaretoken" value="abc123xyz">'
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert CSRF_PLACEHOLDER.encode() in result
        assert b"abc123xyz" not in result

    def test_normalizes_csrf_token_value_before_name(self):
        """CSRF token with value before name should be normalized."""
        html = b'<input type="hidden" value="abc123xyz" name="csrfmiddlewaretoken">'
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert CSRF_PLACEHOLDER.encode() in result
        assert b"abc123xyz" not in result

    def test_handles_single_quotes(self):
        """CSRF token with single quotes should be normalized."""
        html = b"<input type='hidden' name='csrfmiddlewaretoken' value='abc123xyz'>"
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert CSRF_PLACEHOLDER.encode() in result
        assert b"abc123xyz" not in result

    def test_preserves_surrounding_html(self):
        """Other HTML content should be preserved."""
        html = b'<form><input name="csrfmiddlewaretoken" value="token123"><input name="username"></form>'
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert b"<form>" in result
        assert b"</form>" in result
        assert b'name="username"' in result

    def test_handles_multiple_csrf_tokens(self):
        """Multiple CSRF tokens in the same HTML should all be normalized."""
        html = b"""
        <form id="form1"><input name="csrfmiddlewaretoken" value="token1"></form>
        <form id="form2"><input name="csrfmiddlewaretoken" value="token2"></form>
        """
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert result.count(CSRF_PLACEHOLDER.encode()) == 2
        assert b"token1" not in result
        assert b"token2" not in result

    def test_handles_none_input(self):
        """None input should return None."""
        assert normalize_csrf_in_body(None) is None

    def test_handles_empty_bytes(self):
        """Empty bytes should return empty bytes."""
        assert normalize_csrf_in_body(b"") == b""

    def test_handles_html_without_csrf(self):
        """HTML without CSRF tokens should remain unchanged."""
        html = b"<form><input name='username' value='john'></form>"
        result = normalize_csrf_in_body(html)
        assert result == html

    def test_case_insensitive_csrf_name(self):
        """CSRF token name matching should be case-insensitive."""
        html = b'<input name="CSRFMIDDLEWARETOKEN" value="abc123">'
        result = normalize_csrf_in_body(html)
        assert result is not None
        assert CSRF_PLACEHOLDER.encode() in result

    def test_handles_non_utf8_gracefully(self):
        """Non-UTF8 content should be returned unchanged."""
        html = b"\xff\xfe invalid utf8"
        result = normalize_csrf_in_body(html)
        assert result == html


class TestNormalizeHtmlBody:
    """Tests for normalize_html_body function."""

    def test_normalizes_html_content(self):
        """HTML content should be normalized."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html")
        assert result == b'<div class="a b">content</div>'

    def test_skips_non_html_content(self):
        """Non-HTML content types should not be modified."""
        json_content = b'{"class": "b a"}'
        result = normalize_html_body(json_content, "application/json")
        assert result == json_content

    def test_skips_compressed_content(self):
        """Compressed content should not be modified."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html", "gzip")
        assert result == html

    def test_allows_identity_encoding(self):
        """Identity encoding should be processed normally."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html", "identity")
        assert result == b'<div class="a b">content</div>'

    def test_handles_none_body(self):
        """None body should return None."""
        assert normalize_html_body(None, "text/html") is None

    def test_handles_empty_body(self):
        """Empty body should return empty body."""
        assert normalize_html_body(b"", "text/html") == b""

    def test_handles_text_html_with_charset(self):
        """text/html with charset should be recognized as HTML."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html; charset=utf-8")
        assert result == b'<div class="a b">content</div>'

    def test_case_insensitive_content_type(self):
        """Content-Type matching should be case-insensitive."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "TEXT/HTML")
        assert result == b'<div class="a b">content</div>'

    def test_skips_deflate_encoding(self):
        """Deflate encoding should be skipped."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html", "deflate")
        assert result == html

    def test_skips_br_encoding(self):
        """Brotli encoding should be skipped."""
        html = b'<div class="b a">content</div>'
        result = normalize_html_body(html, "text/html", "br")
        assert result == html


class TestNormalizeHtmlResponse:
    """Tests for normalize_html_response function."""

    def test_normalizes_html_response(self):
        """HTML response content should be normalized."""
        response = MagicMock()
        response.get.side_effect = lambda key, default="": {
            "Content-Type": "text/html",
            "Content-Encoding": "",
        }.get(key, default)
        response.content = b'<div class="b a">content</div>'
        response.__contains__ = lambda self, key: key == "Content-Length"
        response.__setitem__ = MagicMock()

        result = normalize_html_response(response)

        assert result.content == b'<div class="a b">content</div>'

    def test_updates_content_length(self):
        """Content-Length header should be updated if present."""
        response = MagicMock()
        response.get.side_effect = lambda key, default="": {
            "Content-Type": "text/html",
            "Content-Encoding": "",
        }.get(key, default)
        response.content = b'<div class="b a">content</div>'
        response.__contains__ = lambda self, key: key == "Content-Length"

        normalize_html_response(response)

        response.__setitem__.assert_called_with("Content-Length", len(b'<div class="a b">content</div>'))

    def test_skips_non_html_response(self):
        """Non-HTML responses should not be modified."""
        response = MagicMock()
        response.get.side_effect = lambda key, default="": {
            "Content-Type": "application/json",
            "Content-Encoding": "",
        }.get(key, default)
        original_content = b'{"class": "b a"}'
        response.content = original_content

        result = normalize_html_response(response)

        assert result.content == original_content

    def test_handles_response_without_content(self):
        """Response without content attribute should be returned unchanged."""
        response = MagicMock(spec=["get"])
        response.get.return_value = "text/html"
        del response.content  # Remove content attribute

        result = normalize_html_response(response)

        assert result is response
