"""E2E tests for Drift Python SDK.

These tests validate the full integration between:
- SDK span capture (RECORD mode)
- CLI communication protocol
- Mock replay (REPLAY mode)

E2E tests run in Docker containers and use the `tusk` CLI to:
1. Record spans from test requests
2. Replay tests with mocked responses
3. Validate that responses match recordings
"""
