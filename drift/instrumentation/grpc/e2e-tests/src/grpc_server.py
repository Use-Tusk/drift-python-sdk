"""gRPC server for e2e tests."""

import time
from concurrent import futures

# These will be generated from proto file
import greeter_pb2
import greeter_pb2_grpc
import grpc


class GreeterServicer(greeter_pb2_grpc.GreeterServicer):
    """Implementation of the Greeter service."""

    def SayHello(self, request, context):
        """Handle SayHello RPC (unary-unary)."""
        return greeter_pb2.HelloReply(message=f"Hello, {request.name}!")

    def SayHelloWithInfo(self, request, context):
        """Handle SayHelloWithInfo RPC (unary-unary)."""
        # Use deterministic values for testing (no dynamic UUIDs or timestamps)
        return greeter_pb2.HelloReplyWithInfo(
            message=f"Hello, {request.name} from {request.city}! You are {request.age} years old.",
            greeting_id="test-greeting-id-12345",
            timestamp=1234567890000,
        )

    def SayHelloStream(self, request, context):
        """Handle SayHelloStream RPC - server streaming (unary-stream)."""
        greetings = [
            f"Hello, {request.name}!",
            f"Welcome, {request.name}!",
            f"Greetings, {request.name}!",
        ]
        for greeting in greetings:
            yield greeter_pb2.HelloReply(message=greeting)
            time.sleep(0.1)  # Small delay between messages

    def SayHelloToMany(self, request_iterator, context):
        """Handle SayHelloToMany RPC - client streaming (stream-unary).

        This endpoint exposes BUG #3: Channel.stream_unary is NOT patched.
        """
        names = []
        for request in request_iterator:
            names.append(request.name)

        combined_greeting = f"Hello to all: {', '.join(names)}!"
        return greeter_pb2.HelloReply(message=combined_greeting)

    def Chat(self, request_iterator, context):
        """Handle Chat RPC - bidirectional streaming (stream-stream).

        This endpoint exposes BUG #4: Channel.stream_stream is NOT patched.
        """
        for request in request_iterator:
            response_message = f"Echo: {request.name}"
            yield greeter_pb2.HelloReply(message=response_message)
            time.sleep(0.05)  # Small delay between responses


def serve(port: int = 50051):
    """Start the gRPC server."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    greeter_pb2_grpc.add_GreeterServicer_to_server(GreeterServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"gRPC server started on port {port}")
    return server


if __name__ == "__main__":
    server = serve()
    server.wait_for_termination()
