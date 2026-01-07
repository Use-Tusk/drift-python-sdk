"""FastAPI test app for e2e tests - HTTP instrumentation."""

import asyncio
import os
import traceback
from concurrent.futures import ThreadPoolExecutor

import httpx
import requests
from fastapi import FastAPI, Request
from opentelemetry import context as otel_context
from opentelemetry import trace
from pydantic import BaseModel

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = FastAPI(title="FastAPI E2E Test App")


def _run_with_context(ctx, fn, *args, **kwargs):
    """Helper to run a function with OpenTelemetry context in a thread pool."""
    token = otel_context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        otel_context.detach(token)


# Health check endpoint
@app.get("/health")
async def health():
    return {"status": "healthy"}


# GET /api/weather - Get weather from external API
@app.get("/api/weather")
async def get_weather():
    """Fetch weather data from external API."""
    try:
        # Using httpx for async HTTP client
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": 40.7128,
                    "longitude": -74.0060,
                    "current_weather": "true",
                },
            )
            weather = response.json()

        return {
            "location": "New York",
            "weather": weather.get("current_weather", {}),
        }
    except Exception as e:
        return {"error": f"Failed to fetch weather: {str(e)}"}


# GET /api/user/{user_id} - Get user from external API
@app.get("/api/user/{user_id}")
async def get_user(user_id: str):
    """Fetch user data from external API with seed."""
    try:
        response = requests.get(f"https://randomuser.me/api/?seed={user_id}")
        return response.json()
    except Exception as e:
        return {"error": f"Failed to fetch user: {str(e)}"}


class CreatePostRequest(BaseModel):
    title: str
    body: str
    userId: int = 1


# POST /api/post - Create new post
@app.post("/api/post")
async def create_post(post: CreatePostRequest):
    """Create a new post via external API."""
    try:
        response = requests.post(
            "https://jsonplaceholder.typicode.com/posts",
            json=post.model_dump(),
        )
        return response.json()
    except Exception as e:
        return {"error": f"Failed to create post: {str(e)}"}


# GET /api/post/{post_id} - Get post with comments (parallel)
@app.get("/api/post/{post_id}")
async def get_post(post_id: int):
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

    return {
        "post": post_response.json(),
        "comments": comments_response.json(),
    }


# DELETE /api/post/{post_id} - Delete post
@app.delete("/api/post/{post_id}")
async def delete_post(post_id: int):
    """Delete a post via external API."""
    try:
        requests.delete(f"https://jsonplaceholder.typicode.com/posts/{post_id}")
        return {"message": f"Post {post_id} deleted successfully"}
    except Exception as e:
        return {"error": f"Failed to delete post: {str(e)}"}


# GET /api/activity - Get random activity
@app.get("/api/activity")
async def get_activity():
    """Fetch a random activity suggestion."""
    try:
        response = requests.get("https://bored-api.appbrewery.com/random")
        return response.json()
    except Exception as e:
        return {"error": f"Failed to fetch activity: {str(e)}"}


# GET /api/test-async-context - Test async context propagation
@app.get("/api/test-async-context")
async def test_async_context():
    """Test that OpenTelemetry context propagates correctly across async boundaries.

    This endpoint:
    1. Captures the parent trace context
    2. Makes concurrent async HTTP calls
    3. Verifies child operations have the same trace context
    4. Returns verification results (without dynamic trace IDs to ensure replay matches)
    """
    # Get current span info
    current_span = trace.get_current_span()
    span_context = current_span.get_span_context() if current_span else None
    parent_trace_id = format(span_context.trace_id, "032x") if span_context else None

    results = []

    async def make_nested_call(call_id: int):
        """Make an async HTTP call and verify context is preserved."""
        # Capture span info inside the nested async call
        inner_span = trace.get_current_span()
        inner_context = inner_span.get_span_context() if inner_span else None
        inner_trace_id = format(inner_context.trace_id, "032x") if inner_context else None

        # Capture stack trace for debugging
        stack = traceback.format_stack()
        # Filter to show relevant frames
        filtered_stack = [frame for frame in stack if "app.py" in frame or "asyncio" in frame][
            -5:
        ]  # Last 5 relevant frames

        # Make an actual HTTP call to verify instrumentation captures it
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    "https://httpbin.org/get",
                    params={"call_id": call_id},
                    timeout=10.0,
                )
                http_status = response.status_code
            except Exception as e:
                http_status = f"error: {e}"

        # Only return deterministic fields (no trace IDs which change per run)
        return {
            "call_id": call_id,
            "trace_preserved": inner_trace_id == parent_trace_id,
            "http_status": http_status,
            "stack_frames": len(filtered_stack),
        }

    # Run concurrent async tasks
    tasks = [make_nested_call(i) for i in range(3)]
    results = await asyncio.gather(*tasks)

    all_preserved = all(r["trace_preserved"] for r in results)

    # Only return deterministic fields for replay matching
    return {
        "results": results,
        "all_context_preserved": all_preserved,
        "test_status": "PASS" if all_preserved else "FAIL",
    }


# GET /api/test-thread-context - Test thread pool context propagation
@app.get("/api/test-thread-context")
async def test_thread_context():
    """Test that context propagates correctly to ThreadPoolExecutor.

    This tests the explicit context propagation pattern used in get_post().
    """
    ctx = otel_context.get_current()
    current_span = trace.get_current_span()
    span_context = current_span.get_span_context() if current_span else None
    parent_trace_id = format(span_context.trace_id, "032x") if span_context else None

    def thread_task(task_id: int):
        """Task that runs in thread pool with explicit context."""
        inner_span = trace.get_current_span()
        inner_context = inner_span.get_span_context() if inner_span else None
        inner_trace_id = format(inner_context.trace_id, "032x") if inner_context else None

        # Make HTTP call in thread
        response = requests.get(
            "https://httpbin.org/get",
            params={"task_id": task_id},
            timeout=10,
        )

        # Only return deterministic fields (no trace IDs which change per run)
        return {
            "task_id": task_id,
            "trace_preserved": inner_trace_id == parent_trace_id,
            "http_status": response.status_code,
        }

    results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_run_with_context, ctx, thread_task, i) for i in range(3)]
        results = [f.result() for f in futures]

    all_preserved = all(r["trace_preserved"] for r in results)

    # Only return deterministic fields for replay matching
    return {
        "results": results,
        "all_context_preserved": all_preserved,
        "test_status": "PASS" if all_preserved else "FAIL",
    }


if __name__ == "__main__":
    import uvicorn

    sdk.mark_app_as_ready()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
