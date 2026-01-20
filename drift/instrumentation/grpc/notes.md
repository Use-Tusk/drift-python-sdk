# gRPC Instrumentation Notes

## 1. gRPC Communication Patterns

gRPC supports 4 types of RPC patterns:

### Unary RPC ✅ (Implemented)

```text
Client ──[Request]──> Server
Client <──[Response]── Server
```

- Single request, single response
- Like a regular function call
- Example: `SayHello(HelloRequest) → HelloReply`

### Server Streaming ✅ (Implemented)

```text
Client ──[Request]──────────> Server
Client <──[Response 1]─────── Server
Client <──[Response 2]─────── Server
Client <──[Response N]─────── Server
```

- Single request, stream of responses
- Example: `ListFeatures(Rectangle) → stream Feature`
- Use case: Fetching paginated data, real-time feeds, LLM token streaming

### Client Streaming ✅ (Implemented)

```text
Client ──[Request 1]──────> Server
Client ──[Request 2]──────> Server
Client ──[Request N]──────> Server
Client <──[Response]─────── Server
```

- Stream of requests, single response
- Example: `RecordRoute(stream Point) → RouteSummary`
- Use case: File uploads, aggregating data from client

### Bidirectional Streaming ✅ (Implemented)

```text
Client ──[Request 1]──────> Server
Client <──[Response 1]───── Server
Client ──[Request 2]──────> Server
Client <──[Response 2]───── Server
        ... (interleaved)
```

- Stream of requests AND stream of responses (simultaneously)
- Example: `RouteChat(stream RouteNote) → stream RouteNote`
- Use case: Chat applications, real-time audio processing, collaborative editing

## 2. Python gRPC API

### Channel Methods

The `grpcio` library's `Channel` class provides methods to create callable objects for each pattern:

```python
channel.unary_unary(method)    # Returns UnaryUnaryMultiCallable
channel.unary_stream(method)   # Returns UnaryStreamMultiCallable
channel.stream_unary(method)   # Returns StreamUnaryMultiCallable
channel.stream_stream(method)  # Returns StreamStreamMultiCallable
```

### Invocation Variants

Each `MultiCallable` supports different invocation styles:

| Variant | Syntax | Behavior |
|---------|--------|----------|
| **Direct call** | `response = callable(request)` | Blocks until response received |
| **with_call** | `response, call = callable.with_call(request)` | Returns response + Call object with metadata |
| **future** | `future = callable.future(request)` | Returns Future for async handling |

Example:

```python
# Direct call - simplest, blocks
response = stub.SayHello(request)

# with_call - access metadata
response, call = stub.SayHello.with_call(request)
initial_metadata = call.initial_metadata()
trailing_metadata = call.trailing_metadata()

# future - non-blocking
future = stub.SayHello.future(request)
# ... do other work ...
response = future.result()  # blocks when needed
```

### Naming in Instrumentation Code

The instrumentation handlers follow the pattern `_handle_{pattern}_{variant}`:

| Handler | Pattern | Variant |
|---------|---------|---------|
| `_handle_unary_unary` | unary_unary | Direct call |
| `_handle_unary_unary_with_call` | unary_unary | with_call |
| `_handle_unary_unary_future` | unary_unary | future |
| `_handle_unary_stream` | unary_stream | Direct call (iterator) |
| `_handle_stream_unary` | stream_unary | Direct call |
| `_handle_stream_unary_with_call` | stream_unary | with_call |
| `_handle_stream_unary_future` | stream_unary | future |
| `_handle_stream_stream` | stream_stream | Direct call (iterator) |

Note: Streaming responses (`unary_stream`, `stream_stream`) don't have `with_call`/`future` variants - they always return iterators.

## 3. Use Cases in Python Ecosystem

gRPC is commonly used in Python for ML inference, data engineering, and cloud services.

### ML Inference Services

| Service | Primary Pattern | Use Case |
|---------|----------------|----------|
| TensorFlow Serving | Unary | `Predict(PredictRequest) → PredictResponse` |
| Triton Inference Server | Unary | `ModelInfer(ModelInferRequest) → ModelInferResponse` |
| vLLM / Text Generation | Server Streaming | `Generate(Prompt) → stream Token` (token-by-token output) |
| Batch Inference | Unary | Send batch of inputs, get batch of outputs |

**Verdict**: Mostly unary, with server streaming for LLM token streaming

### Google Cloud APIs

| Service | Primary Pattern | Use Case |
|---------|----------------|----------|
| BigQuery | Unary + Server Streaming | Query submission (unary), large result streaming |
| Cloud Storage | Unary | Upload/download objects |
| Pub/Sub | Server Streaming | `StreamingPull()` for message consumption |
| Firestore | Server Streaming | Real-time listeners |
| Speech-to-Text | Bidirectional | `StreamingRecognize()` - send audio chunks, get transcripts |
| Dialogflow | Bidirectional | `StreamingDetectIntent()` for conversations |

**Verdict**: Mostly unary + server streaming. Bidirectional mainly for real-time audio/conversation.

### Data Engineering

| Tool | Primary Pattern | Use Case |
|------|----------------|----------|
| Apache Beam | Unary | Job submission, status checks |
| Apache Kafka (gRPC bridge) | Server Streaming | Consuming message streams |
| Ray Serve | Unary | Remote function calls |
| Dask | Unary | Task submission |

**Verdict**: Almost entirely unary

### Coverage Summary

| Pattern | Estimated Usage | Status | Typical Use Cases |
|---------|-----------------|--------|-------------------|
| Unary | ~80-85% | ✅ Implemented | Predictions, queries, CRUD, job submission |
| Server Streaming | ~10-15% | ✅ Implemented | LLM tokens, large results, real-time feeds |
| Client Streaming | ~2-3% | ✅ Implemented | Audio upload (Speech-to-Text) |
| Bidirectional | ~2-3% | ✅ Implemented | Real-time audio/conversations |

**All 4 gRPC communication patterns are now implemented for client-side instrumentation.**

## 4. Implementation Details

### What's Patched

The instrumentation patches the concrete `grpc._channel.Channel` class (not just the abstract `grpc.Channel`):

- `grpc._channel.Channel.unary_unary()` - Returns instrumented callable
- `grpc._channel.Channel.unary_stream()` - Returns instrumented callable
- `grpc._channel.Channel.stream_unary()` - Returns instrumented callable
- `grpc._channel.Channel.stream_stream()` - Returns instrumented callable

**Important**: We patch `grpc._channel.Channel` (the concrete implementation), not `grpc.Channel` (the abstract base class). Python's MRO resolves methods from the concrete class first, so patching the abstract class has no effect.

### Methods Instrumented

| Method | Description | Record | Replay |
|--------|-------------|--------|--------|
| `callable()` | Direct unary call | ✅ | ✅ |
| `callable.with_call()` | Unary with metadata | ✅ | ✅ |
| `callable.future()` | Async unary (future) | ✅ | ✅ |
| `stream_callable()` | Server streaming | ✅ | ✅ |
| `stream_unary_callable()` | Client streaming | ✅ | ✅ |
| `stream_stream_callable()` | Bidirectional streaming | ✅ | ✅ |

### Data Serialization

gRPC messages often contain binary data (protobuf `bytes` fields). The instrumentation handles this using:

1. `serialize_grpc_payload()`: Converts protobuf messages to JSON-serializable dicts, replacing `bytes` with placeholders and storing actual data in a `buffer_map`

2. `deserialize_grpc_payload()`: Restores `bytes` fields from the `buffer_map` during replay

3. `serialize_grpc_metadata()`: Converts gRPC metadata (headers/trailers) to a JSON-serializable format

### Comparison with Node SDK

This implementation follows the same patterns as the Node SDK's gRPC instrumentation:

| Feature | Node SDK | Python SDK |
|---------|----------|------------|
| Unary calls | ✅ `makeUnaryRequest` | ✅ `unary_unary` |
| Server streaming | ✅ `makeServerStreamRequest` | ✅ `unary_stream` |
| Client streaming | ❌ Not implemented | ✅ `stream_unary` |
| Bidirectional | ❌ Not implemented | ✅ `stream_stream` |
| Server-side | ❌ Commented out | ❌ Not implemented |
| Buffer handling | ✅ Placeholder-based | ✅ Placeholder-based |

**Note**: Python SDK has more complete client-side coverage than the Node SDK.

## 5. Future Considerations

### Server-Side Instrumentation

Server-side gRPC instrumentation (inbound requests) is not yet implemented. Similar to the Node SDK, we'll hold off until a customer asks for it.

If needed, this would involve:

1. Patching `grpc.server()` or `Server.add_insecure_port()`
2. Wrapping service handlers to create SERVER spans
3. Handling replay of server responses (more complex than client-side)

### Known Limitations

1. **Client streaming iterator consumption**: For `stream_unary` and `stream_stream` calls, the request iterator is consumed upfront to capture all requests. This changes behavior slightly if the iterator has side effects.

2. **Protobuf int64 serialization**: Protobuf's `MessageToDict` converts `int64` fields to strings for JSON compatibility. The `MockProtoMessage` class handles this by converting numeric strings back to integers during replay.
