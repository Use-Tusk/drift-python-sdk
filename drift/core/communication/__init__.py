"""CLI communication module for Drift SDK.

This module handles bidirectional communication between the SDK and the Tusk CLI
for replay testing. Communication uses Protocol Buffers over Unix sockets or TCP.
"""

from .types import (
    MessageType,
    SDKMessageType,
    CLIMessageType,
    ConnectRequest,
    ConnectResponse,
    GetMockRequest,
    GetMockResponse,
    EnvVarRequest,
    EnvVarResponse,
)
from .communicator import ProtobufCommunicator, CommunicatorConfig

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
    "EnvVarRequest",
    "EnvVarResponse",
    # Communicator
    "ProtobufCommunicator",
    "CommunicatorConfig",
]
