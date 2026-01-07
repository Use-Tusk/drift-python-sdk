"""Django views for e2e test application."""

import json
from concurrent.futures import ThreadPoolExecutor

import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from opentelemetry import context as otel_context


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
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": 40.7128,
                "longitude": -74.0060,
                "current_weather": "true",
            },
        )
        weather = response.json()

        return JsonResponse({
            "location": "New York",
            "weather": weather.get("current_weather", {}),
        })
    except Exception as e:
        return JsonResponse({"error": f"Failed to fetch weather: {str(e)}"}, status=500)


@require_GET
def get_user(request, user_id: str):
    """Fetch user data from external API with seed."""
    try:
        response = requests.get(f"https://randomuser.me/api/?seed={user_id}")
        return JsonResponse(response.json())
    except Exception as e:
        return JsonResponse({"error": f"Failed to fetch user: {str(e)}"}, status=500)


@csrf_exempt
@require_POST
def create_post(request):
    """Create a new post via external API."""
    try:
        data = json.loads(request.body)
        response = requests.post(
            "https://jsonplaceholder.typicode.com/posts",
            json={
                "title": data.get("title"),
                "body": data.get("body"),
                "userId": data.get("userId", 1),
            },
        )
        return JsonResponse(response.json(), status=201)
    except Exception as e:
        return JsonResponse({"error": f"Failed to create post: {str(e)}"}, status=500)


@require_GET
def get_post(request, post_id: int):
    """Fetch post and comments in parallel using ThreadPoolExecutor."""
    ctx = otel_context.get_current()

    with ThreadPoolExecutor(max_workers=2) as executor:
        post_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            f"https://jsonplaceholder.typicode.com/posts/{post_id}",
        )
        comments_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            f"https://jsonplaceholder.typicode.com/posts/{post_id}/comments",
        )

        post_response = post_future.result()
        comments_response = comments_future.result()

    return JsonResponse({
        "post": post_response.json(),
        "comments": comments_response.json(),
    })


@csrf_exempt
@require_http_methods(["DELETE"])
def delete_post(request, post_id: int):
    """Delete a post via external API."""
    try:
        requests.delete(f"https://jsonplaceholder.typicode.com/posts/{post_id}")
        return JsonResponse({"message": f"Post {post_id} deleted successfully"})
    except Exception as e:
        return JsonResponse({"error": f"Failed to delete post: {str(e)}"}, status=500)


@require_GET
def get_activity(request):
    """Fetch a random activity suggestion."""
    try:
        response = requests.get("https://bored-api.appbrewery.com/random")
        return JsonResponse(response.json())
    except Exception as e:
        return JsonResponse({"error": f"Failed to fetch activity: {str(e)}"}, status=500)

