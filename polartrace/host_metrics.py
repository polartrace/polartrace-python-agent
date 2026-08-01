"""
Host & process metrics sampler.

Mirrors the Node.js agent's host-metrics behaviour: every flush interval, takes a
snapshot of CPU usage, memory, host info, and uptime, and posts a batch of samples
to ``{BASE_URL}/host-metrics`` in the JSON shape the PolarTrace collector expects.

Python doesn't have a V8-style heap or an always-running event loop, so the
``heapUsedBytes`` / ``heapTotalBytes`` / ``externalBytes`` / ``arrayBuffersBytes``
fields are populated with sensible Python equivalents (RSS-based), and the
``eventLoop`` block is reported as all-zero. The schema requires those fields,
so we emit them rather than omitting them.
"""

from __future__ import annotations

import os
import platform
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:
    import psutil  # type: ignore
    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - psutil is a hard dep, but be defensive
    psutil = None  # type: ignore
    _PSUTIL_AVAILABLE = False


_MAX_QUEUE = 500  # collector validation upper bound


class HostMetricsMonitor:
    """
    Samples CPU / memory / host stats on the agent's flush timer and forwards
    each batch via ``on_batch``. The agent reuses its existing HTTP session for
    transport so authentication headers stay consistent.
    """

    def __init__(
        self,
        on_batch: Callable[[List[Dict[str, Any]]], None],
        sample_interval_seconds: float,
        enable_console_log: bool = False,
    ):
        self._on_batch = on_batch
        self._interval = sample_interval_seconds
        self._enable_console_log = enable_console_log
        self._timer: Optional[threading.Timer] = None
        self._samples: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._proc: Optional[Any] = None
        self._initialized = False

        if _PSUTIL_AVAILABLE and psutil is not None:
            try:
                self._proc = psutil.Process(os.getpid())
                # Prime cpu_percent so the next call returns a real value instead of 0.
                self._proc.cpu_percent(interval=None)
                self._initialized = True
            except Exception:
                self._proc = None

    @property
    def initialized(self) -> bool:
        return self._initialized

    def start(self) -> None:
        if not self._initialized:
            return
        self._schedule()

    def _schedule(self) -> None:
        t = threading.Timer(self._interval, self._tick)
        t.daemon = True
        self._timer = t
        t.start()

    def _tick(self) -> None:
        try:
            sample = self._take_sample()
        except Exception as e:  # pylint: disable=broad-except
            if self._enable_console_log:
                print(f"HostMetricsMonitor: sample failed: {e}")
            sample = None

        if sample is not None:
            with self._lock:
                self._samples.append(sample)
                if len(self._samples) > _MAX_QUEUE:
                    # Drop oldest to bound memory, like the Node.js agent does.
                    self._samples = self._samples[-_MAX_QUEUE:]

            batch: List[Dict[str, Any]]
            with self._lock:
                batch = list(self._samples)
                self._samples.clear()
            if batch:
                try:
                    self._on_batch(batch)
                except Exception as e:  # pylint: disable=broad-except
                    if self._enable_console_log:
                        print(f"HostMetricsMonitor: on_batch failed: {e}")
                    # Re-queue so we retry next tick.
                    with self._lock:
                        self._samples = batch + self._samples

        # Schedule the next tick regardless of success.
        self._schedule()

    def destroy(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        self._timer = None

    # ------------------------------------------------------------------ #
    # Sample construction
    # ------------------------------------------------------------------ #
    def _take_sample(self) -> Optional[Dict[str, Any]]:
        if not self._initialized or self._proc is None or psutil is None:
            return None

        cpu_percent = self._proc.cpu_percent(interval=None)
        cpu_times = self._proc.cpu_times()
        mem_info = self._proc.memory_info()
        proc_create = self._proc.create_time()
        rss_bytes = int(mem_info.rss)

        vm = psutil.virtual_memory()
        try:
            load1, load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0

        container = self._maybe_container_info()

        return {
            "timestamp": int(time.time() * 1000),
            "process": {
                "pid": os.getpid(),
                "cpuPercent": float(cpu_percent or 0.0),
                "cpuUserMicros": int((cpu_times.user or 0.0) * 1_000_000),
                "cpuSystemMicros": int((cpu_times.system or 0.0) * 1_000_000),
                "rssBytes": rss_bytes,
                # Python has no V8-style heap. Use RSS to populate the heap fields so
                # the dashboard's "memory" widget still shows a meaningful number.
                "heapUsedBytes": rss_bytes,
                "heapTotalBytes": rss_bytes,
                "externalBytes": 0,
                "arrayBuffersBytes": 0,
                "uptimeSeconds": max(0.0, time.time() - proc_create),
            },
            "eventLoop": {
                # CPython has no stdlib event loop. Emit zeros so the schema validates.
                "meanMs": 0.0,
                "p50Ms": 0.0,
                "p95Ms": 0.0,
                "p99Ms": 0.0,
                "maxMs": 0.0,
            },
            "host": {
                "hostname": socket.gethostname() or "",
                "platform": platform.system() or "",
                "cpuCount": psutil.cpu_count(logical=True) or 0,
                "loadAvg1": float(load1),
                "loadAvg5": float(load5),
                "loadAvg15": float(load15),
                "freeMemBytes": int(vm.available),
                "totalMemBytes": int(vm.total),
                "uptimeSeconds": max(0.0, time.time() - psutil.boot_time()),
                **({"container": container} if container is not None else {}),
            },
        }

    def _maybe_container_info(self) -> Optional[Dict[str, Any]]:
        """Best-effort cgroup v1/v2 inspection. Returns None if not in a container."""
        try:
            # cgroup v2 limits
            mem_max_path = "/sys/fs/cgroup/memory.max"
            mem_current_path = "/sys/fs/cgroup/memory.current"
            cpu_max_path = "/sys/fs/cgroup/cpu.max"

            if not os.path.exists(mem_max_path) and not os.path.exists("/sys/fs/cgroup/memory"):
                return None

            mem_limit: Optional[int] = None
            mem_usage: Optional[int] = None
            cpu_cores: Optional[float] = None

            if os.path.exists(mem_max_path):
                with open(mem_max_path, encoding="ascii") as f:
                    raw = f.read().strip()
                mem_limit = int(raw) if raw.isdigit() else None
                if os.path.exists(mem_current_path):
                    with open(mem_current_path, encoding="ascii") as f:
                        raw = f.read().strip()
                    mem_usage = int(raw) if raw.isdigit() else None

            if os.path.exists(cpu_max_path):
                with open(cpu_max_path, encoding="ascii") as f:
                    raw = f.read().strip().split()
                if raw and raw[0] != "max":
                    quota = int(raw[0])
                    period = int(raw[1]) if len(raw) > 1 else 100_000
                    cpu_cores = quota / period if period else None

            if mem_limit is None and mem_usage is None and cpu_cores is None:
                return None
            return {
                "memoryLimitBytes": mem_limit,
                "memoryUsageBytes": mem_usage,
                "cpuLimitCores": cpu_cores,
            }
        except Exception:
            return None
