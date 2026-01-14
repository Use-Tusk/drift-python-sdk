"""Execute test requests against the FastAPI app."""

import json
from pathlib import Path

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary


def verify_stack_traces():
    """Verify that stack traces are captured in recorded traces."""
    print("\n" + "=" * 50)
    print("Verifying Stack Traces in Recorded Spans")
    print("=" * 50)

    traces_dir = Path(".tusk/traces")
    if not traces_dir.exists():
        print("WARNING: No traces directory found")
        return

    trace_files = list(traces_dir.glob("*.jsonl"))
    print(f"Found {len(trace_files)} trace files")

    spans_with_stack_traces = 0
    client_spans = 0

    for trace_file in trace_files:
        with open(trace_file) as f:
            for line in f:
                try:
                    span = json.loads(line)

                    # Check for CLIENT spans (outbound calls) - kind=3
                    kind = span.get("kind")
                    if kind == 3:  # CLIENT
                        client_spans += 1
                        stack_trace = span.get("stackTrace", "")

                        if stack_trace:
                            spans_with_stack_traces += 1
                            print(f"\n  Span: {span.get('name', 'unknown')}")
                            print(f"    Parent: {(span.get('parentSpanId') or 'none')[:8]}...")
                            print(f"    Stack trace lines: {len(stack_trace.split(chr(10)))}")
                            # Print first 3 lines of stack trace
                            lines = stack_trace.split("\n")[:3]
                            for line in lines:
                                if line.strip():
                                    print(f"      {line.strip()[:80]}")
                except json.JSONDecodeError:
                    continue

    print("\nSummary:")
    print(f"  CLIENT spans: {client_spans}")
    print(f"  Spans with stack traces: {spans_with_stack_traces}")

    if client_spans > 0 and spans_with_stack_traces == 0:
        print("\nNOTE: Stack traces are currently only captured in REPLAY mode")
        print("      for mock matching. In RECORD mode, they may not be present.")


def verify_context_propagation():
    """Verify that async context propagation worked correctly."""
    print("\n" + "=" * 50)
    print("Verifying Context Propagation in Traces")
    print("=" * 50)

    traces_dir = Path(".tusk/traces")
    if not traces_dir.exists():
        print("WARNING: No traces directory found")
        return True

    # Collect all spans by trace ID
    traces = {}
    for trace_file in traces_dir.glob("*.jsonl"):
        with open(trace_file) as f:
            for line in f:
                try:
                    span = json.loads(line)
                    trace_id = span.get("traceId", "")
                    if trace_id:
                        if trace_id not in traces:
                            traces[trace_id] = []
                        traces[trace_id].append(span)
                except json.JSONDecodeError:
                    continue

    print(f"Found {len(traces)} distinct traces")

    all_valid = True
    for trace_id, spans in traces.items():
        # Find root span (SERVER span or no parent)
        root_spans = [s for s in spans if s.get("isRootSpan") or not s.get("parentSpanId")]
        client_spans = [s for s in spans if s.get("kind") == 3]  # CLIENT

        if root_spans and client_spans:
            root_name = root_spans[0].get("name", "unknown")
            print(f"\n  Trace {trace_id[:8]}...")
            print(f"    Root span: {root_name}")
            print(f"    Total spans: {len(spans)}")
            print(f"    Client spans: {len(client_spans)}")

            # Verify all client spans have a parent in this trace
            span_ids = {s.get("spanId") for s in spans}
            orphaned = [s for s in client_spans if s.get("parentSpanId") and s.get("parentSpanId") not in span_ids]

            if orphaned:
                print(f"    WARNING: {len(orphaned)} orphaned client spans (context may have been lost)")
                all_valid = False
            else:
                print("    ✓ All client spans have valid parents")

    return all_valid


if __name__ == "__main__":
    print("Starting FastAPI test request sequence...\n")

    # Execute standard test sequence
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
    make_request("DELETE", "/api/post/1")

    # Test async context propagation
    print("\n" + "=" * 50)
    print("Testing Async Context Propagation")
    print("=" * 50)

    response = make_request("GET", "/api/test-async-context")
    if response.status_code == 200:
        result = response.json()
        print(f"\n  Test result: {result.get('test_status', 'UNKNOWN')}")
        print(f"  All context preserved: {result.get('all_context_preserved', False)}")
        for r in result.get("results", []):
            status = "✓" if r.get("trace_preserved") else "✗"
            print(f"    {status} Call {r.get('call_id')}: trace_preserved={r.get('trace_preserved')}")

    # Test thread pool context propagation
    print("\n" + "=" * 50)
    print("Testing Thread Pool Context Propagation")
    print("=" * 50)

    response = make_request("GET", "/api/test-thread-context")
    if response.status_code == 200:
        result = response.json()
        print(f"\n  Test result: {result.get('test_status', 'UNKNOWN')}")
        print(f"  All context preserved: {result.get('all_context_preserved', False)}")
        for r in result.get("results", []):
            status = "✓" if r.get("trace_preserved") else "✗"
            print(f"    {status} Task {r.get('task_id')}: trace_preserved={r.get('trace_preserved')}")

    print("\n" + "=" * 50)
    print("All requests completed successfully")
    print("=" * 50)

    print_request_summary()
