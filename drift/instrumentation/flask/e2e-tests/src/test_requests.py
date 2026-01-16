"""Execute test requests against the Flask app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting test request sequence...\n")

    # Execute test sequence
    make_request("GET", "/health")
    make_request("GET", "/api/weather-activity")
    make_request("GET", "/api/user/test123")
    make_request("POST", "/api/user")
    make_request("GET", "/api/post/1")
    make_request("POST", "/api/post", json={"title": "Test Post", "body": "This is a test post", "userId": 1})
    make_request("DELETE", "/api/post/1")

    print_request_summary()
