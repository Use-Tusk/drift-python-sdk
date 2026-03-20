"""gRPC server for e2e tests."""

import os
import stat
import time
from concurrent import futures

# These will be generated from proto file
import greeter_pb2
import greeter_pb2_grpc
import grpc


def _log_selected_env():
    """Print relevant environment variables for sandbox debugging."""
    interesting_prefixes = ("TUSK_", "PYTHON", "LD_")
    interesting = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(interesting_prefixes)
    }
    if not interesting:
        print("[grpc probe] interesting env: <none>", flush=True)
        return

    for key in sorted(interesting):
        print(f"[grpc probe] env {key}={interesting[key]}", flush=True)


def _log_mountinfo(prefix: str):
    """Print mountinfo entries rooted at a given mount prefix."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            found = False
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                left, separator, _right = line.partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                if len(fields) < 5:
                    continue
                mount_point = fields[4]
                if mount_point == prefix or mount_point.startswith(f"{prefix}/"):
                    print(f"[grpc probe] mountinfo {mount_point}: {line}", flush=True)
                    found = True
            if not found:
                print(f"[grpc probe] mountinfo: no entries found for {prefix}", flush=True)
    except OSError as exc:
        print(
            f"[grpc probe] read(/proc/self/mountinfo) failed: errno={exc.errno} strerror={exc.strerror}",
            flush=True,
        )


def _log_device_probe(path: str):
    """Print detailed device diagnostics for sandbox debugging."""
    try:
        info = os.stat(path)
        device_kind = "char" if stat.S_ISCHR(info.st_mode) else "other"
        device_id = "-"
        if stat.S_ISCHR(info.st_mode):
            device_id = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        print(
            f"[grpc probe] {path}: mode={oct(stat.S_IMODE(info.st_mode))} "
            f"kind={device_kind} rdev={device_id} "
            f"readable={os.access(path, os.R_OK)} writable={os.access(path, os.W_OK)}",
            flush=True,
        )
    except OSError as exc:
        print(
            f"[grpc probe] stat({path}) failed: errno={exc.errno} strerror={exc.strerror}",
            flush=True,
        )
        return

    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            data = os.read(fd, 1)
            print(f"[grpc probe] read({path}) ok: bytes={len(data)}", flush=True)
        finally:
            os.close(fd)
    except OSError as exc:
        print(
            f"[grpc probe] open/read({path}) failed: errno={exc.errno} strerror={exc.strerror}",
            flush=True,
        )


def _log_startup_probe():
    """Emit diagnostics to pinpoint sandbox/device failures in CI."""
    print(
        f"[grpc probe] pid={os.getpid()} uid={os.getuid()} euid={os.geteuid()} cwd={os.getcwd()}",
        flush=True,
    )
    _log_selected_env()
    _log_mountinfo("/dev")
    try:
        dev_entries = ", ".join(sorted(os.listdir("/dev")))
        print(f"[grpc probe] /dev entries: {dev_entries}", flush=True)
    except OSError as exc:
        print(
            f"[grpc probe] listdir(/dev) failed: errno={exc.errno} strerror={exc.strerror}",
            flush=True,
        )

    for device_path in ("/dev/urandom", "/dev/random", "/dev/null"):
        _log_device_probe(device_path)

    if hasattr(os, "getrandom"):
        try:
            sample = os.getrandom(1)
            print(f"[grpc probe] getrandom() ok: bytes={len(sample)}", flush=True)
        except OSError as exc:
            print(
                f"[grpc probe] getrandom() failed: errno={exc.errno} strerror={exc.strerror}",
                flush=True,
            )


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
    print("[grpc probe] before startup probe", flush=True)
    _log_startup_probe()
    print("[grpc probe] before grpc.server()", flush=True)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    print("[grpc probe] after grpc.server()", flush=True)
    greeter_pb2_grpc.add_GreeterServicer_to_server(GreeterServicer(), server)
    print("[grpc probe] after add_GreeterServicer_to_server()", flush=True)
    bound_port = server.add_insecure_port(f"[::]:{port}")
    print(f"[grpc probe] add_insecure_port returned {bound_port}", flush=True)
    print("[grpc probe] before server.start()", flush=True)
    server.start()
    print("[grpc probe] after server.start()", flush=True)
    print(f"gRPC server started on port {port}", flush=True)
    return server


if __name__ == "__main__":
    server = serve()
    server.wait_for_termination()
