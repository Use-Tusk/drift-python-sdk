"""Basic integration tests for Django instrumentation."""

import os
import sys
import unittest

# Must configure Django before importing models or views
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.integration.django_test_app.settings")

import django
from django.conf import settings

# Configure Django with minimal settings
if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="test-secret-key-for-django-instrumentation-tests",
        ROOT_URLCONF="tests.integration.django_test_app.urls",
        MIDDLEWARE=[
            "django.middleware.common.CommonMiddleware",
        ],
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    )
    django.setup()

from django.http import JsonResponse
from django.test import Client
from django.urls import path

from drift import TuskDrift
from drift.core.types import SpanKind
from drift.instrumentation import DjangoInstrumentation
from drift.tracing.adapters import InMemorySpanAdapter, register_in_memory_adapter


# Define test views
def health_view(request):
    """Health check endpoint."""
    return JsonResponse({"status": "healthy"})


def user_view(request, user_id):
    """User detail endpoint with path parameter."""
    return JsonResponse({"user_id": user_id, "name": f"User {user_id}"})


def echo_view(request):
    """Echo POST data."""
    import json
    if request.method == "POST":
        data = json.loads(request.body)
        return JsonResponse({"echoed": data})
    return JsonResponse({"error": "Method not allowed"}, status=405)


def error_view(request):
    """Endpoint that raises an error."""
    raise ValueError("Test error")


# Define URL patterns
urlpatterns = [
    path("health/", health_view, name="health"),
    path("users/<int:user_id>/", user_view, name="user-detail"),
    path("echo/", echo_view, name="echo"),
    path("error/", error_view, name="error"),
]


class TestDjangoBasicSpanCapture(unittest.TestCase):
    """Test that Django instrumentation captures basic spans."""

    @classmethod
    def setUpClass(cls):
        """Set up SDK and Django client once for all tests."""
        # Initialize SDK
        cls.sdk = TuskDrift.initialize()
        cls.adapter = InMemorySpanAdapter()
        register_in_memory_adapter(cls.adapter)

        # Apply Django instrumentation
        cls.instrumentation = DjangoInstrumentation()

        # Update URL patterns
        from django.urls import clear_url_caches
        from importlib import reload
        settings.ROOT_URLCONF = "tests.integration.test_django_basic"
        clear_url_caches()

        cls.sdk.mark_app_as_ready()
        cls.client = Client()

    def setUp(self):
        """Clear spans before each test."""
        self.adapter.clear()

    def test_captures_get_request_span(self):
        """Test that GET requests create spans."""
        response = self.client.get("/health/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")

        # Wait for span processing
        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        self.assertGreaterEqual(len(spans), 1)

        # Find the Django SERVER span
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertEqual(span.package_name, "django")
        self.assertEqual(span.instrumentation_name, "DjangoInstrumentation")
        self.assertIn("GET", span.name)
        self.assertIn("health", span.name)

    def test_uses_route_template_for_span_name(self):
        """Test that Django uses route templates to avoid cardinality explosion."""
        response = self.client.get("/users/123/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user_id"], 123)

        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        # Should use route template, not literal path
        self.assertIn("users/<int:user_id>/", span.name)
        self.assertNotIn("users/123/", span.name)

    def test_captures_post_request_span(self):
        """Test that POST requests create spans."""
        response = self.client.post(
            "/echo/",
            data='{"message": "test"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["echoed"]["message"], "test")

        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertEqual(span.submodule_name, "POST")

    def test_span_has_trace_id(self):
        """Test that spans have valid trace IDs."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertIsNotNone(span.trace_id)
        self.assertIsInstance(span.trace_id, str)
        self.assertGreater(len(span.trace_id), 0)

    def test_span_has_span_id(self):
        """Test that spans have valid span IDs."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertIsNotNone(span.span_id)
        self.assertIsInstance(span.span_id, str)
        self.assertGreater(len(span.span_id), 0)

    def test_span_has_timing_info(self):
        """Test that spans have timing information."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

        import time
        time.sleep(0.1)

        spans = self.adapter.get_all_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        self.assertGreaterEqual(len(server_spans), 1)

        span = server_spans[0]
        self.assertIsNotNone(span.timestamp)
        self.assertIsNotNone(span.duration)
        self.assertGreater(span.duration.seconds + span.duration.nanos / 1e9, 0)


if __name__ == "__main__":
    unittest.main()
