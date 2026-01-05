"""CLI communication module for Drift SDK.

This module handles bidirectional communication between the SDK and the Tusk CLI
for replay testing. Communication uses Protocol Buffers over Unix sockets or TCP.
"""

from .communicator import CommunicatorConfig, ProtobufCommunicator
from .types import (
    CliMessage,
    CLIMessageType,
    ConnectRequest,
    ConnectResponse,
    GetMockRequest,
    GetMockResponse,
    MessageType,
    MockRequestInput,
    MockResponseOutput,
    # Protobuf types (re-exported)
    SdkMessage,
    SDKMessageType,
    dict_to_span,
    extract_response_data,
    span_to_proto,
)

__all__ = [
    # Message types
    "MessageType",
    "SDKMessageType",
    "CLIMessageType",
    # Request/Response types
    "ConnectRequest",
    "ConnectResponse",
    "GetMockRequest",
    "GetMockResponse",
    "MockRequestInput",
    "MockResponseOutput",
    # Protobuf types
    "SdkMessage",
    "CliMessage",
    # Utilities
    "span_to_proto",
    "dict_to_span",
    "extract_response_data",
    # Communicator
    "ProtobufCommunicator",
    "CommunicatorConfig",
]
