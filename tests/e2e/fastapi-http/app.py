"""FastAPI E2E test application.

This application provides test endpoints that make outbound HTTP requests,
allowing us to test the full SDK instrumentation flow with FastAPI:
1. Inbound HTTP request capture (FastAPI/ASGI)
2. Outbound HTTP request capture (httpx/requests library)
3. CLI communication for mock responses in REPLAY mode
"""

import os
import sys
import time

# Add SDK to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))))

# Initialize SDK before importing FastAPI
from drift import TuskDrift, FastAPIInstrumentation

sdk = TuskDrift.initialize(use_batching=False)
fastapi_instrumentation = FastAPIInstrumentation()

from fastapi import FastAPI, Request
from pydantic import BaseModel
import httpx
import uvicorn

app = FastAPI(title="FastAPI E2E Test")


class PostData(BaseModel):
    title: str = "test"
    body: str = "test body"
    userId: int = 1


class EchoData(BaseModel):
    message: str


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": time.time()}


@app.get("/test-http-get")
async def test_http_get():
    """Test outbound HTTP GET request using httpx."""
    async with httpx.AsyncClient() as client:
        response = await client.get("https://jsonplaceholder.typicode.com/posts/1")
        return {
            "endpoint": "/test-http-get",
            "result": response.json(),
        }


@app.post("/test-http-post")
async def test_http_post(data: PostData | None = None):
    """Test outbound HTTP POST request using httpx."""
    payload = data.model_dump() if data else {"title": "test", "body": "test body", "userId": 1}
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://jsonplaceholder.typicode.com/posts",
            json=payload,
        )
        return {
            "endpoint": "/test-http-post",
            "result": response.json(),
        }


@app.put("/test-http-put")
async def test_http_put():
    """Test outbound HTTP PUT request using httpx."""
    payload = {"id": 1, "title": "updated", "body": "updated body", "userId": 1}
    async with httpx.AsyncClient() as client:
        response = await client.put(
            "https://jsonplaceholder.typicode.com/posts/1",
            json=payload,
        )
        return {
            "endpoint": "/test-http-put",
            "result": response.json(),
        }


@app.delete("/test-http-delete")
async def test_http_delete():
    """Test outbound HTTP DELETE request using httpx."""
    async with httpx.AsyncClient() as client:
        response = await client.delete("https://jsonplaceholder.typicode.com/posts/1")
        return {
            "endpoint": "/test-http-delete",
            "status_code": response.status_code,
        }


@app.get("/test-http-headers")
async def test_http_headers():
    """Test outbound HTTP request with custom headers."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://httpbin.org/headers",
            headers={
                "X-Custom-Header": "test-value",
                "X-Request-Id": "e2e-test-123",
            },
        )
        return {
            "endpoint": "/test-http-headers",
            "result": response.json(),
        }


@app.get("/test-http-query-params")
async def test_http_query_params():
    """Test outbound HTTP request with query parameters."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://jsonplaceholder.typicode.com/posts",
            params={"userId": 1, "_limit": 3},
        )
        return {
            "endpoint": "/test-http-query-params",
            "result": response.json(),
        }


@app.get("/test-http-error")
async def test_http_error():
    """Test outbound HTTP request that returns an error."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://httpbin.org/status/404")
            return {
                "endpoint": "/test-http-error",
                "status_code": response.status_code,
                "error": True,
            }
    except Exception as e:
        return {
            "endpoint": "/test-http-error",
            "error": str(e),
        }


@app.get("/test-chained-requests")
async def test_chained_requests():
    """Test multiple chained outbound requests."""
    async with httpx.AsyncClient() as client:
        # First request: get a user
        user_response = await client.get("https://jsonplaceholder.typicode.com/users/1")
        user = user_response.json()

        # Second request: get posts by that user
        posts_response = await client.get(
            "https://jsonplaceholder.typicode.com/posts",
            params={"userId": user["id"], "_limit": 2},
        )
        posts = posts_response.json()

        return {
            "endpoint": "/test-chained-requests",
            "user": user,
            "posts": posts,
        }


@app.get("/greet/{name}")
def greet(name: str, greeting: str = "Hello"):
    """Greet endpoint with path parameter."""
    return {
        "message": f"{greeting}, {name}!",
        "name": name,
    }


@app.post("/echo")
def echo(data: EchoData):
    """Echo back the request body."""
    return {
        "echoed": data.model_dump(),
        "received_at": time.time(),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    sdk.mark_app_as_ready()
    print(f"Server running on port {port}")
    print("Available endpoints:")
    print("  GET  /health - Health check")
    print("  GET  /test-http-get - Test outbound HTTP GET")
    print("  POST /test-http-post - Test outbound HTTP POST")
    print("  PUT  /test-http-put - Test outbound HTTP PUT")
    print("  DELETE /test-http-delete - Test outbound HTTP DELETE")
    print("  GET  /test-http-headers - Test request with custom headers")
    print("  GET  /test-http-query-params - Test request with query params")
    print("  GET  /test-http-error - Test error handling")
    print("  GET  /test-chained-requests - Test multiple chained requests")
    print("  GET  /greet/{name} - Greet with path param")
    print("  POST /echo - Echo request body")
    uvicorn.run(app, host="0.0.0.0", port=port)
