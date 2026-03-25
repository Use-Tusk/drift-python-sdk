# redis Instrumentation Bug Tracking

Generated: 2026-03-25

## Summary

- Total tests attempted: 19
- Confirmed bugs: 1
- No bugs found: 18
- Skipped tests: 0

---

## Test Results

### Test 1: Pipeline as context manager

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/pipeline-context-manager`

**Description**:
Tested whether using pipeline as a context manager (`with redis_client.pipeline() as pipe:`) works correctly with instrumentation. Pipeline's `__exit__` calls `reset()` which could interfere with instrumentation.

**Expected Behavior**: Pipeline execute works the same in RECORD and REPLAY modes.

**Actual Behavior**: All modes passed correctly. The `reset()` method doesn't call `execute_command` so no interference.

---

### Test 2: Hash operations with mapping kwarg (HSET mapping=)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/hash-mapping`

**Description**:
Tested HSET with `mapping={"name": "Alice", "age": "30"}` kwarg, plus HGET, HGETALL, HMGET, HDEL, HKEYS, HVALS. The mapping kwarg is expanded into positional args by redis-py before calling execute_command.

**Expected Behavior**: All hash operations recorded and replayed correctly.

**Actual Behavior**: All modes passed. Mapping kwarg is correctly expanded to `HSET key field1 value1 field2 value2 ...` by redis-py.

---

### Test 3: Scan iterator (scan_iter)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/scan-iter`

**Description**:
Tested `scan_iter()` which internally calls `SCAN` multiple times with cursor-based pagination. Created 20 keys and used `scan_iter(match="test:scan:*", count=5)`. Resulted in 4 SCAN calls with cursors 0→12→14→5→0.

**Expected Behavior**: Each SCAN call recorded separately and replayed with correct cursor values.

**Actual Behavior**: All 4 SCAN calls were correctly recorded with their cursor values and replayed successfully.

---

### Test 4: Blocking list operations (BLPOP/BRPOP)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/blpop`

**Description**:
Tested BLPOP and BRPOP with pre-populated list (immediate return) and on empty list with timeout (returns None after 1s timeout).

**Expected Behavior**: BLPOP/BRPOP results correctly serialized including None for timeout case.

**Actual Behavior**: All modes passed. The None result for timeout BLPOP is correctly serialized as `{"result": null}` and deserialized back to None.

---

### Test 5: Lua script EVAL

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/eval-script`

**Description**:
Tested EVAL with Lua scripts including multi-key/multi-arg patterns. The Lua script text is passed as an argument to execute_command.

**Expected Behavior**: EVAL commands with complex arguments recorded and replayed correctly.

**Actual Behavior**: All modes passed. Lua scripts are serialized as string arguments.

---

### Test 6: from_url() client creation

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/from-url`

**Description**:
Tested Redis client created via `redis.from_url("redis://host:port/0")`. Since patching is done on the Redis class (not instances), clients created via from_url should be automatically instrumented.

**Expected Behavior**: Operations on from_url-created client are instrumented.

**Actual Behavior**: All modes passed. Class-level patching correctly covers all Redis instances.

---

### Test 7: Sorted set operations with scores

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/sorted-set`

**Description**:
Tested ZADD, ZRANGE with withscores, ZRANGEBYSCORE, ZSCORE, ZRANK, ZINCRBY, ZCARD. These operations involve float scores and complex argument patterns.

**Expected Behavior**: Float scores correctly serialized and deserialized.

**Actual Behavior**: All modes passed. Float values are preserved through JSON serialization.

---

### Test 8: Pub/Sub publish with subscriber

**Status**: Confirmed Bug - REPLAY mismatch

**Endpoint**: `/test/pubsub-publish`

**Failure Point**: REPLAY

**Description**:
Tested Redis Pub/Sub pattern where a subscriber thread subscribes to a channel and the main thread publishes messages. The publish commands go through `Redis.execute_command()` (which is patched), but the subscriber uses `PubSub.execute_command()` which is a completely separate method defined in the PubSub class.

**Expected Behavior**:
During REPLAY, both publish and subscribe operations should be mocked. The subscriber should receive the recorded messages.

**Actual Behavior**:
During REPLAY, publish commands are correctly mocked (return subscriber count of 1). However, the subscriber thread's PubSub operations (subscribe, get_message) attempt to make real network connections because `PubSub.execute_command()` is not patched by the instrumentation. Since there's no real Redis server in replay mode, the subscriber receives no messages.

**Error Logs**:
```json
{
  "field": "response.body",
  "expected": {
    "received": ["message1", "message2", "message3"],
    "subscribers_notified": [1, 1, 1],
    "success": true
  },
  "actual": {
    "received": [],
    "subscribers_notified": [1, 1, 1],
    "success": true
  },
  "description": "Response body content mismatch"
}
```

**Additional Notes**:
Root cause: In redis-py, the `PubSub` class has its own `execute_command()` method (defined in `redis/client.py` line ~1037) that does NOT call `Redis.execute_command()`. It manages its own persistent connection for receiving messages. The Redis instrumentation only patches `Redis.execute_command`, `Pipeline.execute`, and `Pipeline.immediate_execute_command` (plus their async variants), but does NOT patch `PubSub.execute_command`.

This affects any code that creates a PubSub subscriber within a request handler context. The `publish()` command called on a regular Redis client works correctly since it goes through `Redis.execute_command()`.

To fix: The instrumentation should also patch `PubSub.execute_command()` and `PubSub.get_message()` / `PubSub.listen()` to handle REPLAY mode properly.

---

### Test 9: Set operations (SADD/SMEMBERS/SINTER/SUNION/SDIFF)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/set-operations`

**Description**:
Tested set data type operations including SADD, SMEMBERS, SINTER, SUNION, SDIFF, SISMEMBER, SCARD. Sets return Python set objects.

**Expected Behavior**: Set results correctly serialized (sets become sorted lists).

**Actual Behavior**: All modes passed.

---

### Test 10: Geospatial operations

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/geo-operations`

**Description**:
Tested GEOADD, GEODIST, GEOPOS, GEOSEARCH with longitude/latitude coordinates and distance calculations.

**Expected Behavior**: Float coordinates and distances correctly serialized.

**Actual Behavior**: All modes passed.

---

### Test 11: Register script (EVALSHA pattern)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/register-script`

**Description**:
Tested `redis_client.register_script()` which uses EVALSHA with automatic SHA caching. First call may use EVALSHA→fallback to SCRIPT LOAD→EVALSHA.

**Expected Behavior**: Script execution with SHA caching works in all modes.

**Actual Behavior**: All modes passed.

---

### Test 12: Stream operations (XADD/XREAD/XRANGE)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/stream-operations`

**Description**:
Tested Redis Streams with XADD, XLEN, XRANGE, XREAD, XINFO_STREAM, XTRIM. Stream IDs contain timestamps.

**Expected Behavior**: Stream operations and complex return types correctly handled.

**Actual Behavior**: All modes passed.

---

### Test 13: Large payload / many keys

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/large-payload`

**Description**:
Tested 10KB string value and MSET/MGET with 50 keys. Checks serialization of large payloads.

**Expected Behavior**: Large values correctly serialized without truncation.

**Actual Behavior**: All modes passed.

---

### Test 14: Distributed lock pattern

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/lock-acquire`

**Description**:
Tested `redis_client.lock()` which uses SET with NX/PX flags and Lua scripts for release. The lock operations go through execute_command internally.

**Expected Behavior**: Lock acquire/release works in all modes.

**Actual Behavior**: All modes passed.

---

### Test 15: Async basic operations (non-pipeline)

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/async-basic`

**Description**:
Tested basic async Redis operations (SET, GET, HSET with mapping, HGETALL, DELETE) using `redis.asyncio.Redis`. These use the patched async execute_command.

**Expected Behavior**: Async operations correctly instrumented.

**Actual Behavior**: All modes passed.

---

### Test 16: GETEX and GETDEL commands

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/getex-getdel`

**Description**:
Tested newer Redis commands GETEX (get and set expiry) and GETDEL (get and delete). These are standard commands that go through execute_command.

**Expected Behavior**: New commands correctly handled.

**Actual Behavior**: All modes passed.

---

### Test 17: List operations

**Status**: No Bug - Test passed all modes

**Endpoint**: `/test/list-operations`

**Description**:
Tested comprehensive list operations: RPUSH, LRANGE, LLEN, LINDEX, LPOP, RPOP, LINSERT. These have various argument patterns.

**Expected Behavior**: All list operations correctly instrumented.

**Actual Behavior**: All modes passed.

---

### Test 18: Pipeline context manager (already tested in Test 1)

Duplicate of Test 1 - confirmed no bug.

---

### Test 19: Existing test endpoints (original 18 endpoints)

**Status**: No Bug - All original tests passed all modes

**Description**:
All 18 original test endpoints (health, set, get, delete, incr, keys, mget-mset, pipeline-basic, pipeline-no-transaction, async-pipeline, binary-data, transaction-watch) passed RECORD and REPLAY modes.

---
