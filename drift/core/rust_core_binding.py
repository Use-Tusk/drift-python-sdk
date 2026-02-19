"""Optional bridge to local drift-rust-core Python bindings.

This module is intentionally fail-open: if bindings are unavailable,
callers fall back to native Python implementations.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_binding_module = None
_binding_load_attempted = False


def _enabled() -> bool:
    return os.getenv("TUSK_USE_RUST_CORE", "0").lower() in {"1", "true", "yes"}


def _load_binding():
    global _binding_module, _binding_load_attempted
    if _binding_load_attempted:
        return _binding_module
    _binding_load_attempted = True
    try:
        import drift_core as binding

        _binding_module = binding
    except Exception as exc:  # pragma: no cover - depends on runtime env
        logger.debug("Rust core binding not available: %s", exc)
        _binding_module = None
    return _binding_module


def normalize_and_hash_jsonable(value: Any) -> tuple[Any, str] | None:
    """Return normalized JSON value and deterministic hash via Rust binding."""
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        normalized_json, digest = binding.normalize_and_hash(payload)
        return json.loads(normalized_json), digest
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust normalize_and_hash failed, falling back: %s", exc)
        return None


def normalize_json_jsonable(value: Any) -> Any | None:
    """Return normalized JSON value via Rust binding."""
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        normalized_json = binding.normalize_json(payload)
        return json.loads(normalized_json)
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust normalize_json failed, falling back: %s", exc)
        return None


def deterministic_hash_jsonable(value: Any) -> str | None:
    """Return deterministic hash via Rust binding."""
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return binding.deterministic_hash(payload)
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust deterministic_hash failed, falling back: %s", exc)
        return None


def process_export_payload_jsonable(value: Any, schema_merges: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Process payload for export in one Rust call.

    Returns:
        {
          "normalized_value": Any,
          "decoded_value": Any,
          "decoded_value_hash": str,
          "decoded_schema": Any,
          "decoded_schema_hash": str,
          "protobuf_struct_bytes": bytes,
        }
    """
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        (
            normalized_value,
            decoded_value_hash,
            decoded_schema,
            decoded_schema_hash,
            protobuf_struct_bytes,
        ) = binding.process_export_payload_pyobject(value, schema_merges)
        return {
            "normalized_value": normalized_value,
            "decoded_value_hash": decoded_value_hash,
            "decoded_schema": decoded_schema,
            "decoded_schema_hash": decoded_schema_hash,
            "protobuf_struct_bytes": protobuf_struct_bytes,
        }
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust process_export_payload failed, falling back: %s", exc)
        return None


def build_span_proto_bytes(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    name: str,
    package_name: str,
    instrumentation_name: str,
    submodule_name: str,
    package_type: int,
    environment: str | None,
    kind: int,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    input_schema_hash: str,
    output_schema_hash: str,
    input_value_hash: str,
    output_value_hash: str,
    status_code: int,
    status_message: str,
    is_pre_app_start: bool,
    is_root_span: bool,
    timestamp_seconds: int,
    timestamp_nanos: int,
    duration_seconds: int,
    duration_nanos: int,
    metadata: dict[str, Any] | None,
    input_value: Any,
    output_value: Any,
    input_value_proto_struct_bytes: bytes | None,
    output_value_proto_struct_bytes: bytes | None,
) -> bytes | None:
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        return binding.build_span_proto_bytes_pyobject(
            trace_id,
            span_id,
            parent_span_id,
            name,
            package_name,
            instrumentation_name,
            submodule_name,
            package_type,
            environment,
            kind,
            input_schema,
            output_schema,
            input_schema_hash,
            output_schema_hash,
            input_value_hash,
            output_value_hash,
            status_code,
            status_message,
            is_pre_app_start,
            is_root_span,
            timestamp_seconds,
            timestamp_nanos,
            duration_seconds,
            duration_nanos,
            metadata,
            input_value,
            output_value,
            input_value_proto_struct_bytes,
            output_value_proto_struct_bytes,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust build_span_proto_bytes failed, falling back: %s", exc)
        return None


def build_export_spans_request_bytes(
    *,
    observable_service_id: str,
    environment: str,
    sdk_version: str,
    sdk_instance_id: str,
    span_proto_bytes_list: list[bytes],
) -> bytes | None:
    if not _enabled():
        return None
    binding = _load_binding()
    if binding is None:
        return None
    try:
        return binding.build_export_spans_request_bytes_pyobject(
            observable_service_id,
            environment,
            sdk_version,
            sdk_instance_id,
            span_proto_bytes_list,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback path
        logger.debug("Rust build_export_spans_request_bytes failed, falling back: %s", exc)
        return None
