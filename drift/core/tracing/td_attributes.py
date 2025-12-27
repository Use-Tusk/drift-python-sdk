"""Drift-specific OpenTelemetry span attribute constants.

These constants define the attribute keys used to store Drift-specific data
on OpenTelemetry spans. They follow the 'td.' namespace convention.
"""


class TdSpanAttributes:
    """Drift-specific span attribute keys."""

    # Core identification
    NAME = "td.name"
    PACKAGE_NAME = "td.package_name"
    INSTRUMENTATION_NAME = "td.instrumentation_name"
    SUBMODULE_NAME = "td.submodule_name"
    PACKAGE_TYPE = "td.package_type"

    # Data capture
    INPUT_VALUE = "td.input_value"
    OUTPUT_VALUE = "td.output_value"
    INPUT_SCHEMA = "td.input_schema"
    OUTPUT_SCHEMA = "td.output_schema"
    INPUT_SCHEMA_HASH = "td.input_schema_hash"
    OUTPUT_SCHEMA_HASH = "td.output_schema_hash"
    INPUT_VALUE_HASH = "td.input_value_hash"
    OUTPUT_VALUE_HASH = "td.output_value_hash"

    # Schema merge hints
    INPUT_SCHEMA_MERGES = "td.input_schema_merges"
    OUTPUT_SCHEMA_MERGES = "td.output_schema_merges"

    # Flags
    IS_PRE_APP_START = "td.is_pre_app_start"
    IS_ROOT_SPAN = "td.is_root_span"
    IS_USED = "td.is_used"

    # Metadata
    METADATA = "td.metadata"
    TRANSFORM_METADATA = "td.transform_metadata"
    STACK_TRACE = "td.stack_trace"

    # Replay mode
    REPLAY_TRACE_ID = "td.replay_trace_id"
