import os
from typing import List, Optional

from .config import TuskConfig
from .types import CleanSpanData, DriftMode
from ..instrumentation.registry import install_hooks
from ..tracing.adapters.memory import InMemorySpanAdapter


class TuskDrift:
    _instance: Optional["TuskDrift"] = None
    _initialized = False

    def __init__(self):
        self.mode: DriftMode = self._detect_mode()
        self.config = TuskConfig()
        self.app_ready = False
        self._in_memory_adapter = InMemorySpanAdapter()

    @classmethod
    def get_instance(cls) -> "TuskDrift":
        if cls._instance is None:
            cls._instance = TuskDrift()
        return cls._instance

    @classmethod
    def initialize(
        cls,
        api_key: Optional[str] = None,
        env: Optional[str] = None,
        sampling_rate: Optional[float] = None,
    ) -> "TuskDrift":
        instance = cls.get_instance()

        if cls._initialized:
            print("Warning: TuskDrift already initialized")
            return instance

        instance.config = TuskConfig(
            api_key=api_key,
            env=env,
            sampling_rate=sampling_rate or 1.0,
        )

        install_hooks()

        cls._initialized = True
        print(f"Drift SDK initialized in {instance.mode} mode")

        return instance

    def _detect_mode(self) -> DriftMode:
        mode_env = os.environ.get("TUSK_DRIFT_MODE", "").upper()

        if mode_env == "RECORD":
            return "RECORD"
        elif mode_env == "REPLAY":
            return "REPLAY"
        elif mode_env in ("DISABLED", "DISABLE"):
            return "DISABLED"
        else:
            return "RECORD"

    def mark_app_as_ready(self) -> None:
        self.app_ready = True
        print("Application marked as ready")

    def collect_span(self, span: CleanSpanData) -> None:
        """Collect a span synchronously"""
        self._in_memory_adapter.export_spans([span])

    def get_in_memory_spans(self) -> List[CleanSpanData]:
        return self._in_memory_adapter.get_all_spans()

    def shutdown(self) -> None:
        self._in_memory_adapter.shutdown()
        print("Drift SDK shutdown complete")
