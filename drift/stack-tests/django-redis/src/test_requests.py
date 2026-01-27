"""Execute test requests against the Django + Redis app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting Django + Redis test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")

    # Cache operations via Django cache framework
    make_request("POST", "/cache/set", json={"key": "test_key", "value": "test_value"})
    make_request("GET", "/cache/get/test_key")

    # Increment counter
    make_request("POST", "/cache/incr/counter")
    make_request("POST", "/cache/incr/counter")
    make_request("GET", "/cache/get/counter")

    # Session operations (Redis-backed)
    resp = make_request("POST", "/session/set", json={"user_name": "Alice", "logged_in": True})
    # Note: Sessions require cookies to persist, so this is a simple test
    make_request("GET", "/session/get")

    # Direct Redis operations via django-redis
    make_request("GET", "/redis/direct")

    # Pipeline operations
    make_request("GET", "/redis/pipeline")

    # Cleanup
    make_request("DELETE", "/cache/delete/test_key")
    make_request("DELETE", "/cache/delete/counter")

    # Session clear
    make_request("POST", "/session/clear")

    print_request_summary()
