"""Flask test app for e2e tests - HTTP instrumentation only."""

from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, jsonify, request
from opentelemetry import context as otel_context

from drift import TuskDrift

# Initialize SDK
sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

app = Flask(__name__)


def _run_with_context(ctx, fn, *args, **kwargs):
    """Helper to run a function with OpenTelemetry context in a thread pool."""
    # Attach the context in this thread
    token = otel_context.attach(ctx)
    try:
        return fn(*args, **kwargs)
    finally:
        otel_context.detach(token)


# Health check endpoint
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# GET /api/weather-activity - Get location from IP, weather, and activity recommendations
@app.route("/api/weather-activity", methods=["GET"])
def weather_activity():
    try:
        # First API call: Get user's location from IP
        location_response = requests.get("http://ip-api.com/json/")
        location_data = location_response.json()
        city = location_data["city"]
        lat = location_data["lat"]
        lon = location_data["lon"]
        country = location_data["country"]

        # Business logic: Determine if location is coastal
        is_coastal = abs(lon) > 50 or abs(lat) < 30

        # Second API call: Get weather for the location
        weather_response = requests.get(
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        )
        weather = weather_response.json()["current_weather"]

        # Business logic: Recommend activity based on weather
        temp = weather["temperature"]
        windspeed = weather["windspeed"]

        if temp > 40:
            recommended_activity = "Too hot - stay indoors"
        elif temp > 20 and windspeed < 20:
            recommended_activity = "Beach day!" if is_coastal else "Perfect for hiking!"
        elif temp < 10:
            recommended_activity = "Hot chocolate weather"
        elif windspeed > 30:
            recommended_activity = "Too windy - indoor activities recommended"
        else:
            recommended_activity = "Nice day for a walk"

        # Third API call: Get a random activity suggestion
        activity_response = requests.get("https://bored-api.appbrewery.com/random")
        alternative_activity = activity_response.json()

        return jsonify(
            {
                "location": {
                    "city": city,
                    "country": country,
                    "coordinates": {"lat": lat, "lon": lon},
                    "isCoastal": is_coastal,
                },
                "weather": {
                    "temperature": temp,
                    "windspeed": windspeed,
                    "weathercode": weather["weathercode"],
                    "time": weather["time"],
                },
                "recommendations": {
                    "weatherBased": recommended_activity,
                    "alternative": {
                        "activity": alternative_activity["activity"],
                        "type": alternative_activity["type"],
                        "participants": alternative_activity["participants"],
                    },
                },
            }
        )
    except Exception as e:
        return jsonify({"error": f"Failed to fetch weather and activity data: {str(e)}"}), 500


# GET /api/user/:id - Get random user with seed parameter
@app.route("/api/user/<user_id>", methods=["GET"])
def get_user(user_id):
    try:
        response = requests.get(f"https://randomuser.me/api/?seed={user_id}")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": f"Failed to fetch user data: {str(e)}"}), 500


# POST /api/user - Create random user (no seed)
@app.route("/api/user", methods=["POST"])
def create_user():
    try:
        response = requests.get("https://randomuser.me/api/")
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": f"Failed to create user: {str(e)}"}), 500


# GET /api/post/:id - Get post with comments (parallel execution)
@app.route("/api/post/<post_id>", methods=["GET"])
def get_post(post_id):
    # Get current OpenTelemetry context to propagate to thread pool
    ctx = otel_context.get_current()

    # Fetch post and comments in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        # Run requests with context propagation
        post_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            f"https://jsonplaceholder.typicode.com/posts/{post_id}",
        )
        comments_future = executor.submit(
            _run_with_context,
            ctx,
            requests.get,
            f"https://jsonplaceholder.typicode.com/posts/{post_id}/comments",
        )

        post_response = post_future.result()
        comments_response = comments_future.result()

    return jsonify({"post": post_response.json(), "comments": comments_response.json()})


# POST /api/post - Create new post
@app.route("/api/post", methods=["POST"])
def create_post():
    try:
        data = request.get_json()

        response = requests.post(
            "https://jsonplaceholder.typicode.com/posts",
            json={
                "title": data.get("title"),
                "body": data.get("body"),
                "userId": data.get("userId"),
            },
        )

        return jsonify(response.json()), 201
    except Exception as e:
        return jsonify({"error": f"Failed to create post: {str(e)}"}), 500


# DELETE /api/post/:id - Delete post
@app.route("/api/post/<post_id>", methods=["DELETE"])
def delete_post(post_id):
    try:
        requests.delete(f"https://jsonplaceholder.typicode.com/posts/{post_id}")
        return jsonify({"message": f"Post {post_id} deleted successfully"})
    except Exception as e:
        return jsonify({"error": f"Failed to delete post: {str(e)}"}), 500


if __name__ == "__main__":
    sdk.mark_app_as_ready()
    app.run(host="0.0.0.0", port=8000, debug=False)
