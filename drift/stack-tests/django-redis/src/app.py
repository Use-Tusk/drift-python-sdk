"""Django + Redis test application runner."""

import os
import sys

# Add src to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

# Initialize Drift SDK before Django
from drift import TuskDrift

sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)

# Now setup Django
import django

django.setup()

# Import WSGI application
from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    sdk.mark_app_as_ready()
    port = int(os.getenv("PORT", "8000"))

    print(f"Starting Django + Redis test server on port {port}...")
    httpd = make_server("0.0.0.0", port, application)
    httpd.serve_forever()
