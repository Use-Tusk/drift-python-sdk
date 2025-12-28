"""Flask app with Redis operations for e2e testing."""

import os
import redis
from drift import TuskDrift
from flask import Flask, jsonify, request

# Initialize Drift SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)

# Initialize Redis client
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    db=0,
    decode_responses=True
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


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
