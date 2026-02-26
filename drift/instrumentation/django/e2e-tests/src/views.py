"""Django views for e2e test application."""

import json
from concurrent.futures import ThreadPoolExecutor

import requests
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from opentelemetry import context as otel_context

from drift.instrumentation.e2e_common.external_http import (
    external_http_timeout_seconds,
    upstream_url,
)

EXTERNAL_HTTP_TIMEOUT_SECONDS = external_http_timeout_seconds()


def _run_with_context(ctx, fn, *args, **kwargs):
    """Helper to run a function with OpenTelemetry context in a thread pool."""
    token = otel_context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        otel_context.detach(token)


@require_GET
def health(request):
    """Health check endpoint."""
    return JsonResponse({"status": "healthy"})


@require_GET
def get_weather(request):
    """Fetch weather data from external API."""
    try:
        response = requests.get(
            upstream_url("https://api.open-meteo.com/v1/forecast"),
            params={
                "latitude": 40.7128,
                "longitude": -74.0060,
                "current_weather": "true",
            },
            timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
        )
        weather = response.json()

        return JsonResponse(
            {
                "location": "New York",
                "weather": weather.get("current_weather", {}),
            }
        )
    except Exception as e:
        return JsonResponse(
            {
                "location": "New York",
                "weather": {},
                "fallback": True,
                "error": f"Failed to fetch weather: {str(e)}",
            }
        )


@require_GET
def get_user(request, user_id: str):
    """Fetch user data from external API with seed."""
    try:
        response = requests.get(
            upstream_url("https://randomuser.me/api/"),
            params={"seed": user_id},
            timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
        )
        return JsonResponse(response.json())
    except Exception as e:
        return JsonResponse({"results": [], "fallback": True, "error": f"Failed to fetch user: {str(e)}"})


@csrf_exempt
@require_POST
def create_post(request):
    """Create a new post via external API."""
    data = {}
    try:
        data = json.loads(request.body)
        response = requests.post(
            upstream_url("https://jsonplaceholder.typicode.com/posts"),
            json={
                "title": data.get("title"),
                "body": data.get("body"),
                "userId": data.get("userId", 1),
            },
            timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
        )
        return JsonResponse(response.json(), status=201)
    except Exception as e:
        return JsonResponse(
            {
                "id": -1,
                "title": data.get("title", ""),
                "body": data.get("body", ""),
                "userId": data.get("userId", 1),
                "fallback": True,
                "error": f"Failed to create post: {str(e)}",
            },
            status=201,
        )


@require_GET
def get_post(request, post_id: int):
    """Fetch post and comments in parallel using ThreadPoolExecutor."""
    try:
        ctx = otel_context.get_current()

        with ThreadPoolExecutor(max_workers=2) as executor:
            post_future = executor.submit(
                _run_with_context,
                ctx,
                requests.get,
                upstream_url(f"https://jsonplaceholder.typicode.com/posts/{post_id}"),
                timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
            )
            comments_future = executor.submit(
                _run_with_context,
                ctx,
                requests.get,
                upstream_url(f"https://jsonplaceholder.typicode.com/posts/{post_id}/comments"),
                timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
            )

            post_response = post_future.result()
            comments_response = comments_future.result()

        return JsonResponse(
            {
                "post": post_response.json(),
                "comments": comments_response.json(),
            }
        )
    except Exception as e:
        return JsonResponse(
            {
                "post": {},
                "comments": [],
                "fallback": True,
                "error": f"Failed to fetch post: {str(e)}",
            }
        )


@csrf_exempt
@require_http_methods(["DELETE"])
def delete_post(request, post_id: int):
    """Delete a post via external API."""
    try:
        requests.delete(
            upstream_url(f"https://jsonplaceholder.typicode.com/posts/{post_id}"),
            timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
        )
        return JsonResponse({"message": f"Post {post_id} deleted successfully"})
    except Exception as e:
        return JsonResponse(
            {
                "message": f"Post {post_id} delete fallback",
                "fallback": True,
                "error": f"Failed to delete post: {str(e)}",
            }
        )


@require_GET
def get_activity(request):
    """Fetch a random activity suggestion."""
    try:
        response = requests.get(
            upstream_url("https://bored-api.appbrewery.com/random"),
            timeout=EXTERNAL_HTTP_TIMEOUT_SECONDS,
        )
        return JsonResponse(response.json())
    except Exception as e:
        return JsonResponse(
            {
                "activity": "Take a short walk",
                "type": "relaxation",
                "participants": 1,
                "fallback": True,
                "error": f"Failed to fetch activity: {str(e)}",
            }
        )


@require_GET
def csrf_form(request):
    """Return an HTML form with CSRF token for testing CSRF normalization.

    This endpoint tests that CSRF tokens are properly normalized during
    recording so that replay comparisons succeed.
    """
    csrf_token = get_token(request)
    html = f"""<!DOCTYPE html>
<html>
<head><title>CSRF Test Form</title></head>
<body>
    <h1>CSRF Test Form</h1>
    <form method="POST" action="/api/submit">
        <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
        <input type="text" name="message" placeholder="Enter message">
        <button type="submit">Submit</button>
    </form>
</body>
</html>"""
    return HttpResponse(html, content_type="text/html")
