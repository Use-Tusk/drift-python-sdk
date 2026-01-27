"""Django views for Redis test."""

import json

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST


@require_GET
def health(request):
    """Health check endpoint."""
    return JsonResponse({"status": "healthy"})


# ============================================================================
# Django Cache Framework Operations (using django-redis backend)
# ============================================================================


@csrf_exempt
@require_POST
def cache_set(request):
    """Set a value in Django cache (backed by Redis)."""
    try:
        data = json.loads(request.body)
        key = data.get("key")
        value = data.get("value")
        timeout = data.get("timeout")  # Optional timeout in seconds

        if not key or value is None:
            return JsonResponse({"error": "key and value are required"}, status=400)

        if timeout:
            cache.set(key, value, timeout=timeout)
        else:
            cache.set(key, value)

        return JsonResponse({"key": key, "value": value, "success": True})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
def cache_get(request, key):
    """Get a value from Django cache by key."""
    try:
        value = cache.get(key)
        return JsonResponse({"key": key, "value": value, "exists": value is not None})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["DELETE"])
def cache_delete(request, key):
    """Delete a key from Django cache."""
    try:
        # cache.delete returns True if key existed, but we can't rely on this
        # across all cache backends, so we check first
        exists = cache.get(key) is not None
        cache.delete(key)
        return JsonResponse({"key": key, "deleted": exists})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def cache_incr(request, key):
    """Increment a counter in Django cache."""
    try:
        # First set if doesn't exist
        if cache.get(key) is None:
            cache.set(key, 0)
        value = cache.incr(key)
        return JsonResponse({"key": key, "value": value})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ============================================================================
# Session Operations (using Redis-backed sessions)
# ============================================================================


@csrf_exempt
@require_POST
def session_set(request):
    """Set values in the session (stored in Redis)."""
    try:
        data = json.loads(request.body)

        for key, value in data.items():
            request.session[key] = value

        # Force save
        request.session.save()

        return JsonResponse(
            {
                "success": True,
                "session_key": request.session.session_key,
                "data": data,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_GET
def session_get(request):
    """Get all session values."""
    try:
        # Get all session data
        session_data = dict(request.session.items())

        return JsonResponse(
            {
                "session_key": request.session.session_key,
                "data": session_data,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def session_clear(request):
    """Clear the session."""
    try:
        session_key = request.session.session_key
        request.session.flush()

        return JsonResponse(
            {
                "success": True,
                "cleared_session_key": session_key,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ============================================================================
# Direct Redis Operations (using django-redis's raw client)
# ============================================================================


@require_GET
def redis_direct(request):
    """Test direct Redis operations using django-redis's raw client.

    This tests that both the Django cache integration and direct Redis
    client access work correctly with the SDK's Redis instrumentation.
    """
    try:
        from django_redis import get_redis_connection

        # Get the raw Redis client from django-redis
        redis_client = get_redis_connection("default")

        # Perform direct Redis operations
        redis_client.set("django:direct:test", "direct_value")
        value = redis_client.get("django:direct:test")

        # Decode bytes to string if necessary
        if isinstance(value, bytes):
            value = value.decode("utf-8")

        # Cleanup
        redis_client.delete("django:direct:test")

        return JsonResponse(
            {
                "status": "success",
                "value": value,
                "client_type": type(redis_client).__name__,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e), "error_type": type(e).__name__}, status=500)


@require_GET
def redis_pipeline(request):
    """Test Redis pipeline operations via django-redis.

    This tests that pipeline operations work correctly with the SDK's
    Redis instrumentation when using django-redis.
    """
    try:
        from django_redis import get_redis_connection

        redis_client = get_redis_connection("default")

        # Use pipeline for batched operations
        pipe = redis_client.pipeline()
        pipe.set("django:pipe:key1", "value1")
        pipe.set("django:pipe:key2", "value2")
        pipe.get("django:pipe:key1")
        pipe.get("django:pipe:key2")
        results = pipe.execute()

        # Decode bytes to strings
        decoded_results = []
        for r in results:
            if isinstance(r, bytes):
                decoded_results.append(r.decode("utf-8"))
            else:
                decoded_results.append(r)

        # Cleanup
        redis_client.delete("django:pipe:key1", "django:pipe:key2")

        return JsonResponse(
            {
                "status": "success",
                "results": decoded_results,
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e), "error_type": type(e).__name__}, status=500)
