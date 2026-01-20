"""Execute test requests against the Flask app to exercise the gRPC instrumentation."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting test request sequence for gRPC instrumentation...\n")

    # Health check
    make_request("GET", "/health")

    # Simple unary gRPC call
    make_request("GET", "/api/greet?name=TestUser")

    # Unary gRPC call with different name
    make_request("GET", "/api/greet?name=AnotherUser")

    # Unary gRPC call with complex request
    make_request(
        "POST",
        "/api/greet-with-info",
        json={"name": "John", "age": 30, "city": "San Francisco"},
    )

    # Server streaming gRPC call
    make_request("GET", "/api/greet-stream?name=StreamUser")

    # Multiple sequential gRPC calls
    make_request("GET", "/api/greet-chain")

    # Test with_call method
    make_request("GET", "/api/greet-with-call?name=CallUser")

    # Future calls (async unary)
    make_request("GET", "/test/future-call?name=FutureTest")

    # Client streaming (stream-unary)
    make_request("GET", "/test/stream-unary")

    # Bidirectional streaming (stream-stream)
    make_request("GET", "/test/stream-stream")

    print_request_summary()
