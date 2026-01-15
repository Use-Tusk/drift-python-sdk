"""Resource monitoring for benchmarks - tracks CPU and memory usage."""

from __future__ import annotations

import resource
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CpuStats:
    """CPU usage statistics."""

    user_percent: float = 0.0
    system_percent: float = 0.0
    total_percent: float = 0.0


@dataclass
class MemoryStats:
    """Memory usage statistics."""

    rss_avg: float = 0.0
    rss_max: float = 0.0


@dataclass
class TaskResourceStats:
    """Resource statistics for a single task."""

    cpu: CpuStats = field(default_factory=CpuStats)
    memory: MemoryStats = field(default_factory=MemoryStats)


@dataclass
class TaskStats:
    """Internal tracking for a task."""

    rss_sum: float = 0.0
    rss_max: float = 0.0
    start_user_time: float = 0.0
    start_system_time: float = 0.0
    start_time: float = 0.0
    sample_count: int = 0


class ResourceMonitor:
    """Monitor CPU and memory usage during benchmark tasks."""

    def __init__(self, interval_ms: int = 100, enable_memory_tracking: bool = True):
        self.interval_ms = interval_ms
        self.enable_memory_tracking = enable_memory_tracking

        self._task_stats: dict[str, TaskStats] = {}
        self._completed_stats: dict[str, TaskResourceStats] = {}
        self._current_task_name: str | None = None
        self._current_task_stats: TaskStats | None = None
        self._is_running = False
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the resource monitor."""
        self._is_running = True
        self._stop_event.clear()

        if self.enable_memory_tracking:
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

    def stop(self) -> None:
        """Stop the resource monitor."""
        self._is_running = False
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=1)
            self._monitor_thread = None

    def start_task(self, task_name: str) -> None:
        """Start monitoring a new task."""
        self._current_task_name = task_name

        # Get current CPU times
        usage = resource.getrusage(resource.RUSAGE_SELF)

        task_stats = TaskStats(
            start_user_time=usage.ru_utime,
            start_system_time=usage.ru_stime,
            start_time=time.time(),
        )
        self._task_stats[task_name] = task_stats
        self._current_task_stats = task_stats

    def end_task(self) -> None:
        """End monitoring the current task."""
        if not self._current_task_name or not self._current_task_stats:
            return

        end_time = time.time()
        total_elapsed = end_time - self._current_task_stats.start_time

        # Get final CPU times
        usage = resource.getrusage(resource.RUSAGE_SELF)
        user_time = usage.ru_utime - self._current_task_stats.start_user_time
        system_time = usage.ru_stime - self._current_task_stats.start_system_time

        # Calculate CPU percentages
        if total_elapsed > 0:
            user_percent = (user_time / total_elapsed) * 100
            system_percent = (system_time / total_elapsed) * 100
        else:
            user_percent = 0.0
            system_percent = 0.0

        total_percent = user_percent + system_percent

        # Calculate memory stats
        if self._current_task_stats.sample_count > 0:
            rss_avg = self._current_task_stats.rss_sum / self._current_task_stats.sample_count
            rss_max = self._current_task_stats.rss_max
        else:
            rss_avg = 0.0
            rss_max = 0.0

        resource_stats = TaskResourceStats(
            cpu=CpuStats(
                user_percent=user_percent,
                system_percent=system_percent,
                total_percent=total_percent,
            ),
            memory=MemoryStats(
                rss_avg=rss_avg,
                rss_max=rss_max,
            ),
        )

        self._completed_stats[self._current_task_name] = resource_stats
        self._current_task_name = None
        self._current_task_stats = None

    def _monitor_loop(self) -> None:
        """Background thread for collecting memory samples."""
        while not self._stop_event.wait(self.interval_ms / 1000):
            self._collect_memory_sample()

    def _collect_memory_sample(self) -> None:
        """Collect a memory usage sample."""
        if not self._is_running or not self._current_task_stats:
            return

        # Get RSS from resource module (in bytes on macOS, kilobytes on Linux)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is in bytes on macOS, kilobytes on Linux
        import sys

        if sys.platform == "darwin":
            rss = usage.ru_maxrss  # Already in bytes
        else:
            rss = usage.ru_maxrss * 1024  # Convert KB to bytes

        # For more accurate current RSS, try to use /proc on Linux
        try:
            with open("/proc/self/statm") as f:
                # statm gives pages, page size is typically 4096
                pages = int(f.read().split()[1])
                rss = pages * 4096
        except (FileNotFoundError, PermissionError):
            pass  # Use ru_maxrss fallback

        self._current_task_stats.rss_sum += rss
        self._current_task_stats.rss_max = max(self._current_task_stats.rss_max, rss)
        self._current_task_stats.sample_count += 1

    def get_task_stats(self, task_name: str) -> TaskResourceStats | None:
        """Get resource statistics for a completed task."""
        return self._completed_stats.get(task_name)

    def get_all_task_names(self) -> list[str]:
        """Get names of all completed tasks."""
        return list(self._completed_stats.keys())

    def to_dict(self, task_name: str) -> dict[str, Any] | None:
        """Get task stats as a dictionary."""
        stats = self.get_task_stats(task_name)
        if not stats:
            return None

        return {
            "cpu": {
                "userPercent": stats.cpu.user_percent,
                "systemPercent": stats.cpu.system_percent,
                "totalPercent": stats.cpu.total_percent,
            },
            "memory": {
                "rss": {
                    "avg": stats.memory.rss_avg,
                    "max": stats.memory.rss_max,
                },
            },
        }
