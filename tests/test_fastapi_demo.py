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
            print(f"    Target: {input_val.get('target')}")
            print(f"    URL: {input_val.get('url')}")
            if input_val.get("httpVersion"):
                print(f"    HTTP Version: {input_val['httpVersion']}")
            if "body" in input_val:
                print(f"    Body (base64): {input_val['body'][:50]}...")
                print(f"    Body Size: {input_val.get('bodySize')} bytes")

        print("\n  Output:")
        output_val = span.output_value
        if isinstance(output_val, dict):
            print(f"    Status: {output_val.get('statusCode')} {output_val.get('statusMessage', '')}")
            if "body" in output_val:
                print(f"    Body (base64): {output_val['body'][:50]}...")
                print(f"    Body Size: {output_val.get('bodySize')} bytes")

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
