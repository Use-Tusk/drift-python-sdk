import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TUSK_DRIFT_MODE"] = "RECORD"

from drift import FastAPIInstrumentation, TuskDrift

sdk = TuskDrift.initialize()
_ = FastAPIInstrumentation()

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()


class EchoPayload(BaseModel):
    message: str
    count: int = 0


@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": time.time()}


@app.get("/greet/{name}")
def greet(name: str, greeting: str = Query(default="Hello")):
    return {"message": f"{greeting}, {name}!", "name": name}


@app.post("/echo")
def echo(payload: EchoPayload):
    return {"echoed": payload.model_dump(), "received_at": time.time()}


@app.get("/error")
def error():
    return JSONResponse({"error": "Something went wrong"}, status_code=500)


sdk.mark_app_as_ready()


def make_test_requests():
    import requests as http_client

    base_url = "http://127.0.0.1:5556"

    _ = http_client.get(f"{base_url}/health")
    _ = http_client.get(f"{base_url}/greet/World?greeting=Hi")
    payload = {"message": "Hello from client", "count": 42}
    res = http_client.post(f"{base_url}/echo", json=payload)
    print(res)
    _ = http_client.get(f"{base_url}/error")


def display_captured_spans():
    time.sleep(0.5)

    spans = sdk.get_in_memory_spans()

    print(f"CAPTURED SPANS ({len(spans)} total)")

    for i, span in enumerate(spans, 1):
        print(f"\n[Span {i}]")
        print(f"  Name: {span.name}")
        print(f"  Trace ID: {span.trace_id}")
        print(f"  Span ID: {span.span_id}")
        print(
            f"  Package: {span.package_name} ({span.package_type.name if span.package_type else 'N/A'})"
        )
        print(f"  Kind: {span.kind.name}")
        print(f"  Status: {span.status.code.name} - {span.status.message}")
        print(f"  Duration: {span.duration.seconds}s {span.duration.nanos}ns")

        print("\n  Input:")
        input_val = span.input_value
        if isinstance(input_val, dict):
            print(f"    Method: {input_val.get('method')}")
            print(f"    Path: {input_val.get('path')}")
            if "query" in input_val and input_val["query"]:
                print(f"    Query: {input_val['query']}")
            if "body" in input_val:
                print(f"    Body: {json.dumps(input_val['body'], indent=6)}")

        print("\n  Output:")
        output_val = span.output_value
        if isinstance(output_val, dict):
            print(f"    Status: {output_val.get('status_code')}")
            if "body" in output_val:
                print(f"    Body: {json.dumps(output_val['body'], indent=6)}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    import threading

    import uvicorn

    print("Starting FastAPI app on http://127.0.0.1:5556")

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=5556, log_level="warning")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    time.sleep(1)

    try:
        make_test_requests()
        display_captured_spans()

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback

        traceback.print_exc()

    print("\n\nTest complete!")
