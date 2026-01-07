"""FastAPI test app for e2e tests - HTTP instrumentation."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import requests
from fastapi import FastAPI, Request
from opentelemetry import context as otel_context
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


if __name__ == "__main__":
    import uvicorn

    sdk.mark_app_as_ready()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

