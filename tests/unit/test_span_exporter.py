"""Tests for span_exporter.py - TdSpanExporter that manages span export adapters."""

from __future__ import annotations

import asyncio
from pathlib import Path

from drift.core.tracing.span_exporter import TdSpanExporter, TdSpanExporterConfig
from drift.core.types import TuskDriftMode
from tests.utils import create_test_span


class TestTdSpanExporterConfig:
    """Tests for TdSpanExporterConfig dataclass."""

    def test_creates_config_with_required_fields(self):
        """Should create config with required fields."""
        config = TdSpanExporterConfig(
            base_directory=Path("/tmp/traces"),
            mode=TuskDriftMode.RECORD,
        )

        assert config.base_directory == Path("/tmp/traces")
        assert config.mode == TuskDriftMode.RECORD
        assert config.observable_service_id is None
        assert config.use_remote_export is False

    def test_creates_config_with_all_fields(self):
        """Should create config with all optional fields."""
        config = TdSpanExporterConfig(
            base_directory=Path("/tmp/traces"),
            mode=TuskDriftMode.RECORD,
            observable_service_id="service-123",
            use_remote_export=True,
            api_key="test-api-key",
            tusk_backend_base_url="https://custom.api.com",
            environment="production",
            sdk_version="1.0.0",
            sdk_instance_id="instance-abc",
        )

        assert config.observable_service_id == "service-123"
        assert config.use_remote_export is True
        assert config.api_key == "test-api-key"
        assert config.tusk_backend_base_url == "https://custom.api.com"
        assert config.environment == "production"
        assert config.sdk_version == "1.0.0"
        assert config.sdk_instance_id == "instance-abc"


class TestTdSpanExporterInitialization:
    """Tests for TdSpanExporter initialization."""

    def test_uses_filesystem_adapter_by_default(self, tmp_path):
        """Should use filesystem adapter when remote export is disabled."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
            use_remote_export=False,
        )

        exporter = TdSpanExporter(config)

        assert len(exporter.adapters) == 1
        assert exporter.adapters[0].name == "filesystem"

    def test_uses_api_adapter_when_remote_export_enabled(self, tmp_path):
        """Should use API adapter when remote export is enabled."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
            use_remote_export=True,
            api_key="test-key",
            observable_service_id="service-123",
            tusk_backend_base_url="https://api.test.com",
            environment="test",
            sdk_version="1.0.0",
            sdk_instance_id="instance-abc",
        )

        exporter = TdSpanExporter(config)

        assert len(exporter.adapters) == 1
        assert exporter.adapters[0].name == "api"

    def test_falls_back_to_filesystem_without_api_key(self, tmp_path):
        """Should fall back to filesystem adapter if API key is missing."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
            use_remote_export=True,
            api_key=None,  # No API key
            observable_service_id="service-123",
        )

        exporter = TdSpanExporter(config)

        assert len(exporter.adapters) == 1
        assert exporter.adapters[0].name == "filesystem"


class TestTdSpanExporterAdapterManagement:
    """Tests for TdSpanExporter adapter management methods."""

    def test_get_adapters(self, tmp_path):
        """Should return copy of adapters list."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        adapters = exporter.get_adapters()

        assert adapters == exporter.adapters
        assert adapters is not exporter.adapters  # Should be a copy

    def test_add_adapter(self, tmp_path, mocker):
        """Should add adapter to the list."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        new_adapter = mocker.MagicMock()
        new_adapter.name = "custom"
        exporter.add_adapter(new_adapter)

        assert len(exporter.adapters) == 2
        assert new_adapter in exporter.adapters

    def test_remove_adapter(self, tmp_path):
        """Should remove adapter from the list."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        # Get the default adapter and remove it
        default_adapter = exporter.adapters[0]
        exporter.remove_adapter(default_adapter)

        assert len(exporter.adapters) == 0

    def test_remove_nonexistent_adapter_is_safe(self, tmp_path, mocker):
        """Should not raise when removing nonexistent adapter."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        other_adapter = mocker.MagicMock()
        other_adapter.name = "other"

        # Should not raise
        exporter.remove_adapter(other_adapter)

    def test_clear_adapters(self, tmp_path):
        """Should clear all adapters."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        assert len(exporter.adapters) >= 1

        exporter.clear_adapters()

        assert len(exporter.adapters) == 0


class TestTdSpanExporterSetMode:
    """Tests for TdSpanExporter.set_mode method."""

    def test_set_mode(self, tmp_path):
        """Should update the mode."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        assert exporter.mode == TuskDriftMode.RECORD

        exporter.set_mode(TuskDriftMode.REPLAY)

        assert exporter.mode == TuskDriftMode.REPLAY


class TestTdSpanExporterExportSpans:
    """Tests for TdSpanExporter.export_spans method."""

    def test_exports_to_all_adapters_in_record_mode(self, tmp_path, mocker):
        """Should export spans to all adapters in RECORD mode."""
        mock_adapter1 = mocker.MagicMock()
        mock_adapter1.name = "adapter1"
        mock_adapter1.export_spans = mocker.AsyncMock()

        mock_adapter2 = mocker.MagicMock()
        mock_adapter2.name = "adapter2"
        mock_adapter2.export_spans = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter1)
        exporter.add_adapter(mock_adapter2)

        spans = [create_test_span(), create_test_span(span_id="c" * 16)]

        asyncio.run(exporter.export_spans(spans))

        mock_adapter1.export_spans.assert_called_once_with(spans)
        mock_adapter2.export_spans.assert_called_once_with(spans)

    def test_does_not_export_in_replay_mode(self, tmp_path, mocker):
        """Should not export spans in REPLAY mode."""
        mock_adapter = mocker.MagicMock()
        mock_adapter.name = "adapter"
        mock_adapter.export_spans = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.REPLAY,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter)

        spans = [create_test_span()]

        asyncio.run(exporter.export_spans(spans))

        mock_adapter.export_spans.assert_not_called()

    def test_does_not_export_in_disabled_mode(self, tmp_path, mocker):
        """Should not export spans in DISABLED mode."""
        mock_adapter = mocker.MagicMock()
        mock_adapter.name = "adapter"
        mock_adapter.export_spans = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.DISABLED,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter)

        spans = [create_test_span()]

        asyncio.run(exporter.export_spans(spans))

        mock_adapter.export_spans.assert_not_called()

    def test_does_not_export_when_no_adapters(self, tmp_path):
        """Should handle case with no adapters gracefully."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()

        spans = [create_test_span()]

        # Should not raise
        asyncio.run(exporter.export_spans(spans))

    def test_continues_on_adapter_error(self, tmp_path, mocker):
        """Should continue exporting to other adapters if one fails."""
        mock_adapter1 = mocker.MagicMock()
        mock_adapter1.name = "failing"
        mock_adapter1.export_spans = mocker.AsyncMock(side_effect=Exception("Export failed"))

        mock_adapter2 = mocker.MagicMock()
        mock_adapter2.name = "working"
        mock_adapter2.export_spans = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter1)
        exporter.add_adapter(mock_adapter2)

        spans = [create_test_span()]

        # Should not raise
        asyncio.run(exporter.export_spans(spans))

        # Second adapter should still be called
        mock_adapter2.export_spans.assert_called_once()


class TestTdSpanExporterShutdown:
    """Tests for TdSpanExporter.shutdown method."""

    def test_shutdown_calls_all_adapters(self, tmp_path, mocker):
        """Should call shutdown on all adapters."""
        mock_adapter1 = mocker.MagicMock()
        mock_adapter1.name = "adapter1"
        mock_adapter1.shutdown = mocker.AsyncMock()

        mock_adapter2 = mocker.MagicMock()
        mock_adapter2.name = "adapter2"
        mock_adapter2.shutdown = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter1)
        exporter.add_adapter(mock_adapter2)

        asyncio.run(exporter.shutdown())

        mock_adapter1.shutdown.assert_called_once()
        mock_adapter2.shutdown.assert_called_once()

    def test_shutdown_continues_on_adapter_error(self, tmp_path, mocker):
        """Should continue shutdown even if one adapter fails."""
        mock_adapter1 = mocker.MagicMock()
        mock_adapter1.name = "failing"
        mock_adapter1.shutdown = mocker.AsyncMock(side_effect=Exception("Shutdown failed"))

        mock_adapter2 = mocker.MagicMock()
        mock_adapter2.name = "working"
        mock_adapter2.shutdown = mocker.AsyncMock()

        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)
        exporter.clear_adapters()
        exporter.add_adapter(mock_adapter1)
        exporter.add_adapter(mock_adapter2)

        # Should not raise
        asyncio.run(exporter.shutdown())

        # Second adapter should still have shutdown called
        mock_adapter2.shutdown.assert_called_once()


class TestTdSpanExporterForceFlush:
    """Tests for TdSpanExporter.force_flush method."""

    def test_force_flush_is_noop(self, tmp_path):
        """Should be a no-op (no batching in exporter itself)."""
        config = TdSpanExporterConfig(
            base_directory=tmp_path,
            mode=TuskDriftMode.RECORD,
        )
        exporter = TdSpanExporter(config)

        # Should not raise
        asyncio.run(exporter.force_flush())
