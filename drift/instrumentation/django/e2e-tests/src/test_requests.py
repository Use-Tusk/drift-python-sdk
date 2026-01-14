"""Execute test requests against the Django app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting Django test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")
    make_request("GET", "/api/weather")
    make_request("GET", "/api/user/test123")
    make_request("GET", "/api/activity")
    make_request("GET", "/api/post/1")
    make_request(
        "POST",
        "/api/post",
        json={
            "title": "Test Post",
            "body": "This is a test post body",
            "userId": 1,
        },
    )
    make_request("DELETE", "/api/post/1/delete")

    print_request_summary()
