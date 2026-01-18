"""Flask test app for e2e tests - gRPC instrumentation testing.

This app acts as an HTTP gateway that makes gRPC calls to a backend service.
This pattern is common in microservices architectures.
"""

import sys
import threading
import time

from flask import Flask, jsonify, request

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

# Import gRPC modules (generated from proto)
import grpc

# Add src directory to path for generated proto files
sys.path.insert(0, "/app/src")

import greeter_pb2
import greeter_pb2_grpc

# Import and start the gRPC server
from grpc_server import serve as start_grpc_server

app = Flask(__name__)

# gRPC channel and stub (will be created after server starts)
grpc_channel = None
grpc_stub = None

GRPC_SERVER_PORT = 50051


def init_grpc_client():
    """Initialize gRPC client connection."""
    global grpc_channel, grpc_stub
    grpc_channel = grpc.insecure_channel(f"localhost:{GRPC_SERVER_PORT}")
    grpc_stub = greeter_pb2_grpc.GreeterStub(grpc_channel)


# Health check endpoint
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# Simple unary RPC
@app.route("/api/greet", methods=["GET"])
def greet():
    """Test simple unary gRPC call."""
    name = request.args.get("name", "World")
    try:
        response = grpc_stub.SayHello(greeter_pb2.HelloRequest(name=name))
        return jsonify({"message": response.message})
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500


# Unary RPC with more complex request/response
@app.route("/api/greet-with-info", methods=["POST"])
def greet_with_info():
    """Test unary gRPC call with complex request."""
    data = request.get_json() or {}
    try:
        grpc_request = greeter_pb2.HelloRequestWithInfo(
            name=data.get("name", "World"),
            age=data.get("age", 25),
            city=data.get("city", "Unknown"),
        )
        response = grpc_stub.SayHelloWithInfo(grpc_request)
        return jsonify(
            {
                "message": response.message,
                "greeting_id": response.greeting_id,
                "timestamp": response.timestamp,
            }
        )
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500


# Server streaming RPC
@app.route("/api/greet-stream", methods=["GET"])
def greet_stream():
    """Test server streaming gRPC call."""
    name = request.args.get("name", "World")
    try:
        responses = grpc_stub.SayHelloStream(greeter_pb2.HelloRequest(name=name))
        messages = [r.message for r in responses]
        return jsonify({"messages": messages, "count": len(messages)})
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500


# Multiple sequential gRPC calls
@app.route("/api/greet-chain", methods=["GET"])
def greet_chain():
    """Test multiple sequential gRPC calls."""
    try:
        # First call
        response1 = grpc_stub.SayHello(greeter_pb2.HelloRequest(name="Alice"))

        # Second call
        response2 = grpc_stub.SayHello(greeter_pb2.HelloRequest(name="Bob"))

        # Third call with more info
        response3 = grpc_stub.SayHelloWithInfo(greeter_pb2.HelloRequestWithInfo(name="Charlie", age=30, city="NYC"))

        return jsonify(
            {
                "greeting1": response1.message,
                "greeting2": response2.message,
                "greeting3": {
                    "message": response3.message,
                    "greeting_id": response3.greeting_id,
                },
            }
        )
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500


# Test with_call method
@app.route("/api/greet-with-call", methods=["GET"])
def greet_with_call():
    """Test unary gRPC call using with_call to get metadata."""
    name = request.args.get("name", "World")
    try:
        response, call = grpc_stub.SayHello.with_call(greeter_pb2.HelloRequest(name=name))
        # Get metadata from call
        initial_metadata = dict(call.initial_metadata())
        trailing_metadata = dict(call.trailing_metadata())

        return jsonify(
            {
                "message": response.message,
                "has_initial_metadata": len(initial_metadata) >= 0,
                "has_trailing_metadata": len(trailing_metadata) >= 0,
            }
        )
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/future-call", methods=["GET"])
def test_future_call():
    """Test async future gRPC call."""
    name = request.args.get("name", "FutureUser")
    try:
        # Use .future() for async call
        future = grpc_stub.SayHello.future(greeter_pb2.HelloRequest(name=name))
        # Wait for result
        response = future.result(timeout=5.0)
        return jsonify({"message": response.message, "method": "future"})
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route("/test/stream-unary", methods=["GET"])
def test_stream_unary():
    """Test client streaming gRPC call (stream-unary pattern)."""
    try:
        # Create an iterator of requests
        def request_iterator():
            names = ["Alice", "Bob", "Charlie"]
            for name in names:
                yield greeter_pb2.HelloRequest(name=name)

        # Make stream-unary call
        response = grpc_stub.SayHelloToMany(request_iterator())
        return jsonify({"message": response.message, "method": "stream_unary"})
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route("/test/stream-stream", methods=["GET"])
def test_stream_stream():
    """Test bidirectional streaming gRPC call (stream-stream pattern)."""
    try:
        # Create an iterator of requests
        def request_iterator():
            names = ["Echo1", "Echo2", "Echo3"]
            for name in names:
                yield greeter_pb2.HelloRequest(name=name)

        # Make stream-stream call
        responses = grpc_stub.Chat(request_iterator())
        messages = [r.message for r in responses]
        return jsonify({"messages": messages, "count": len(messages), "method": "stream_stream"})
    except grpc.RpcError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


if __name__ == "__main__":
    # Start gRPC server in background thread
    grpc_server = start_grpc_server(port=GRPC_SERVER_PORT)

    # Wait a moment for server to start
    time.sleep(0.5)

    # Initialize gRPC client
    init_grpc_client()

    # Mark app as ready
    sdk.mark_app_as_ready()

    # Start Flask app
    app.run(host="0.0.0.0", port=8000, debug=False)
