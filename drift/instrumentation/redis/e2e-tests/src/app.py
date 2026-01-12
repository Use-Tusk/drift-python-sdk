"""Flask app with Redis operations for e2e testing."""

import os

import redis
from flask import Flask, jsonify, request

from drift import TuskDrift

# Initialize Drift SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)

# Initialize Redis client
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"), port=int(os.getenv("REDIS_PORT", "6379")), db=0, decode_responses=True
)


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})


@app.route("/redis/set", methods=["POST"])
def redis_set():
    """Set a value in Redis."""
    try:
        data = request.get_json()
        key = data.get("key")
        value = data.get("value")
        ex = data.get("ex")  # Optional expiration in seconds

        if not key or value is None:
            return jsonify({"error": "key and value are required"}), 400

        if ex:
            redis_client.set(key, value, ex=ex)
        else:
            redis_client.set(key, value)

        return jsonify({"key": key, "value": value, "success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/redis/get/<key>")
def redis_get(key):
    """Get a value from Redis by key."""
    try:
        value = redis_client.get(key)
        return jsonify({"key": key, "value": value, "exists": value is not None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/redis/delete/<key>", methods=["DELETE"])
def redis_delete(key):
    """Delete a key from Redis."""
    try:
        result = redis_client.delete(key)
        return jsonify({"key": key, "deleted": result > 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/redis/incr/<key>", methods=["POST"])
def redis_incr(key):
    """Increment a counter in Redis."""
    try:
        value = redis_client.incr(key)
        return jsonify({"key": key, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/redis/keys/<pattern>")
def redis_keys(pattern):
    """Get all keys matching a pattern."""
    try:
        keys = redis_client.keys(pattern)
        return jsonify({"pattern": pattern, "keys": keys, "count": len(keys)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/mget-mset", methods=["GET"])
def test_mget_mset():
    """Test MGET/MSET - multiple key operations."""
    try:
        # MSET multiple keys
        redis_client.mset({"test:mset:key1": "value1", "test:mset:key2": "value2", "test:mset:key3": "value3"})
        # MGET multiple keys
        result = redis_client.mget(["test:mset:key1", "test:mset:key2", "test:mset:key3", "test:mset:nonexistent"])
        # Clean up
        redis_client.delete("test:mset:key1", "test:mset:key2", "test:mset:key3")
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/pipeline-basic", methods=["GET"])
def test_pipeline_basic():
    """Test basic pipeline operations."""
    try:
        pipe = redis_client.pipeline()
        pipe.set("test:pipe:key1", "value1")
        pipe.set("test:pipe:key2", "value2")
        pipe.get("test:pipe:key1")
        pipe.get("test:pipe:key2")
        results = pipe.execute()
        # Clean up
        redis_client.delete("test:pipe:key1", "test:pipe:key2")
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test/pipeline-no-transaction", methods=["GET"])
def test_pipeline_no_transaction():
    """Test pipeline with transaction=False."""
    try:
        pipe = redis_client.pipeline(transaction=False)
        pipe.set("test:pipe:notx:key1", "value1")
        pipe.incr("test:pipe:notx:counter")
        pipe.get("test:pipe:notx:key1")
        results = pipe.execute()
        # Clean up
        redis_client.delete("test:pipe:notx:key1", "test:pipe:notx:counter")
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/async-pipeline", methods=["GET"])
def test_async_pipeline():
    """Test async pipeline operations using asyncio."""
    import asyncio
    import redis.asyncio as aioredis

    async def run_async_pipeline():
        # Create async Redis client
        async_client = aioredis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=0,
            decode_responses=True
        )

        try:
            # Create async pipeline
            pipe = async_client.pipeline()
            pipe.set("test:async:pipe:key1", "async_value1")
            pipe.set("test:async:pipe:key2", "async_value2")
            pipe.get("test:async:pipe:key1")
            pipe.get("test:async:pipe:key2")
            results = await pipe.execute()

            # Clean up
            await async_client.delete("test:async:pipe:key1", "test:async:pipe:key2")

            return results
        finally:
            await async_client.aclose()

    try:
        results = asyncio.run(run_async_pipeline())
        return jsonify({
            "success": True,
            "results": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/test/binary-data", methods=["GET"])
def test_binary_data():
    """Test binary data that cannot be decoded as UTF-8."""
    try:
        # Create a Redis client without decode_responses for binary data
        binary_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=0,
            decode_responses=False
        )

        # Binary data that cannot be decoded as UTF-8
        binary_value = bytes([0x80, 0x81, 0x82, 0xFF, 0xFE, 0xFD])

        # Set binary data
        binary_client.set("test:binary:key", binary_value)

        # Get binary data back
        retrieved = binary_client.get("test:binary:key")

        # Clean up
        binary_client.delete("test:binary:key")

        return jsonify({
            "success": True,
            "original_hex": binary_value.hex(),
            "retrieved_hex": retrieved.hex() if retrieved else None,
            "match": binary_value == retrieved
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
