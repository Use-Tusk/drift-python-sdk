"""CLI communication module for Drift SDK.

This module handles bidirectional communication between the SDK and the Tusk CLI
for replay testing. Communication uses Protocol Buffers over Unix sockets or TCP.
"""

from .communicator import CommunicatorConfig, ProtobufCommunicator
from .types import (
    ConnectRequest,
    ConnectResponse,
    GetMockRequest,
    GetMockResponse,
    MockRequestInput,
    MockResponseOutput,
    dict_to_span,
    extract_response_data,
    span_to_proto,
)

__all__ = [
    # Request/Response types
    "ConnectRequest",
    "ConnectResponse",
    "GetMockRequest",
    "GetMockResponse",
    "MockRequestInput",
    "MockResponseOutput",
    # Utilities
    "span_to_proto",
    "dict_to_span",
    "extract_response_data",
    # Communicator
    "ProtobufCommunicator",
    "CommunicatorConfig",
]
