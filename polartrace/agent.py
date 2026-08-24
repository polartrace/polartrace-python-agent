"""
PolarTrace Agent - Request logging and distributed tracing for Django, Flask, FastAPI.
Sends request logs to PolarTrace collector (api/log) and traces to (api/traces).
"""

import os
import time
import threading
from typing import Any, Callable, Dict, List, Optional
from dataclasses import asdict, dataclass

try:
    import requests
except ImportError:
    requests = None

try:
    from polartrace.trace_monitor import TraceMonitor
    _TRACE_MONITOR_AVAILABLE = True
except ImportError:
    TraceMonitor = None
    _TRACE_MONITOR_AVAILABLE = False

# Global singleton for auto-init
_POLARTRACE_AGENT: Optional["PolarTrace"] = None


@dataclass
class RequestLog:
    """Request log payload sent to /api/log."""
    timestamp: str
    method: str
    path: str
    status_code: Optional[int] = None
    duration: float = 0
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    headers: Optional[Dict[str, Any]] = None
    query: Optional[Dict[str, Any]] = None
    body: Optional[Any] = None
    user_agent: Optional[str] = None
    ip: Optional[str] = None
    host: Optional[str] = None
    console_logs: Optional[List[Dict[str, Any]]] = None
    error: Optional[Dict[str, Any]] = None
    service_id: Optional[str] = None
    framework: Optional[str] = None  # django, flask, fastapi


def _get_global_agent() -> Optional["PolarTrace"]:
    return globals().get("_POLARTRACE_AGENT")


def _set_global_agent(agent: "PolarTrace") -> None:
    globals()["_POLARTRACE_AGENT"] = agent


def resolve_collector_base_url() -> str:
    """
    Collector base URL: production unless ``POLARTRACE_ENDPOINT`` says otherwise.

    Deployments that are not production - a staging environment, a local collector during
    an integration test - previously had no way to redirect the agent, because the URL was
    a constant with no override. That made the Python integration impossible to exercise
    anywhere except against live production.

    Accepts either the collector root or a full ingest URL (``.../api/log``), so the same
    value works here and in the Node.js agent's ``POLARTRACE_ENDPOINT``.
    """
    raw = (os.environ.get("POLARTRACE_ENDPOINT") or "").strip()
    if not raw:
        return PolarTrace.DEFAULT_BASE_URL
    base = raw.rstrip("/")
    for suffix in ("/log", "/traces", "/host-metrics"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base or PolarTrace.DEFAULT_BASE_URL


class PolarTrace:
    """
    PolarTrace observability agent for Python web apps.

    Config:
        api_key: PolarTrace license key (required)
        service_name: Application name (required)
        enable_console_log: Debug logging
        capture_headers/capture_body/capture_query: Request capture flags

    Note: the collector URL defaults to production and is overridable only through the
    ``POLARTRACE_ENDPOINT`` environment variable, for pointing an app at a staging or
    local collector. The config file deliberately cannot set it.
    """

    DEFAULT_BASE_URL = "https://collector.polartrace.com/api"
    BASE_URL = DEFAULT_BASE_URL
    FLUSH_INTERVAL = 10
    API_TIMEOUT = 10
    _MAX_LOG_QUEUE = 5000  # drop-oldest bound, mirrors host_metrics._MAX_QUEUE
    _MAX_TRACE_QUEUE = 5000  # drop-oldest bound, mirrors host_metrics._MAX_QUEUE
    _MAX_BATCH = 500  # max items shipped per flush tick
    _MAX_BACKOFF = 300  # seconds - retry backoff ceiling (5 minutes)

    def __init__(
        self,
        api_key: str,
        service_name: str,
        enable_console_log: bool = False,
        capture_headers: bool = True,
        capture_body: bool = True,
        capture_query: bool = True,
    ):
        if not service_name:
            raise ValueError("PolarTrace: service_name is required")
        if not api_key or len(api_key) < 10:
            raise ValueError("PolarTrace: api_key is required and must be at least 10 characters")

        self._api_key = api_key
        self._service_name = service_name
        self._base_url = resolve_collector_base_url()
        self._log_url = f"{self._base_url}/log"
        self._traces_url = f"{self._base_url}/traces"
        self._host_metrics_url = f"{self._base_url}/host-metrics"
        self._enable_console_log = enable_console_log
        self._capture_headers = capture_headers
        self._capture_body = capture_body
        self._capture_query = capture_query

        self._log_queue: List[Dict[str, Any]] = []
        self._log_lock = threading.Lock()
        self._trace_queue: List[Dict[str, Any]] = []
        self._trace_lock = threading.Lock()
        self._is_flushing = False
        self._is_trace_flushing = False
        # Retry backoff state, tracked per stream. While the deadline is in the
        # future, flush ticks for that stream are skipped; the deadline doubles
        # on each consecutive retained failure.
        self._log_consecutive_failures = 0
        self._log_backoff_until = 0.0
        self._trace_consecutive_failures = 0
        self._trace_backoff_until = 0.0
        self._flush_timer: Optional[threading.Timer] = None
        self._log_path = os.path.join(os.getcwd(), "polartrace_agent.log")
        self._connection_status = "pending"
        self._connection_error: Optional[str] = None
        self._trace_monitor: Optional[Any] = None
        self._host_metrics_monitor: Optional[Any] = None

        self._init_file_logging()
        self._init_http_client()
        self._validate_connection()
        self._setup_trace_monitor()
        self._setup_host_metrics_monitor()
        self._setup_flush_timer()
        self._show_connection_status()
        _set_global_agent(self)

    def _init_file_logging(self) -> None:
        try:
            self._log_stream = open(self._log_path, "a", encoding="utf-8")
        except OSError:
            self._log_stream = None

    def _write_log(self, msg: str) -> None:
        if self._log_stream:
            try:
                ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                self._log_stream.write(f"[{ts}] {msg}\n")
                self._log_stream.flush()
            except OSError:
                pass

    def _init_http_client(self) -> None:
        self._session = None
        if requests:
            self._session = requests.Session()
            self._session.headers.update({
                "Content-Type": "application/json",
                "internal-access-token": self._api_key,
                "service-name": self._service_name,
            })
            self._session.timeout = self.API_TIMEOUT

    def _validate_connection(self) -> None:
        if not self._session or not self._base_url:
            self._connection_status = "failed"
            self._connection_error = "Endpoint or HTTP client not configured"
            return

        url = f"{self._base_url}/service/validate"
        try:
            r = self._session.post(url, json={}, timeout=self.API_TIMEOUT)
            if 200 <= r.status_code < 300:
                self._connection_status = "connected"
                self._connection_error = None
            else:
                self._connection_status = "failed"
                self._connection_error = r.json().get("message", f"API returned {r.status_code}")
        except Exception as e:
            self._connection_status = "failed"
            self._connection_error = str(e)

    def _setup_trace_monitor(self) -> None:
        """Initialize OpenTelemetry trace monitor (shipped in the base ``polartrace`` install)."""
        if not _TRACE_MONITOR_AVAILABLE or not TraceMonitor:
            return
        try:
            agent_endpoints = [
                "/api/traces", "/api/log", "/api/logs", "/api/host-metrics",
                "/traces", "/log", "/logs", "/host-metrics",
            ]
            self._trace_monitor = TraceMonitor(
                service_name=self._service_name,
                service_version="1.0.0",
                enable_console_log=self._enable_console_log,
                exclude_agent_spans=True,
                agent_endpoints=agent_endpoints,
                on_span=self._queue_span,
            )
            self._trace_monitor.initialize()
        except Exception as e:
            if self._enable_console_log:
                self._write_log(f"PolarTrace: Trace monitor init skipped: {e}")
            self._trace_monitor = None

    def _setup_host_metrics_monitor(self) -> None:
        """Start the CPU/memory/host sampler. Posts batches at every flush tick."""
        try:
            from polartrace.host_metrics import HostMetricsMonitor
        except ImportError:
            return
        try:
            self._host_metrics_monitor = HostMetricsMonitor(
                on_batch=self._send_host_metrics,
                sample_interval_seconds=self.FLUSH_INTERVAL,
                enable_console_log=self._enable_console_log,
            )
            self._host_metrics_monitor.start()
        except Exception as e:
            if self._enable_console_log:
                self._write_log(f"PolarTrace: Host metrics init skipped: {e}")
            self._host_metrics_monitor = None

    def _send_host_metrics(self, samples: List[Dict[str, Any]]) -> None:
        """POST a batch of host samples to /api/host-metrics. Raises on failure so the
        monitor can re-queue and retry on the next tick."""
        if not (self._session and self._host_metrics_url):
            return
        r = self._session.post(
            self._host_metrics_url, json={"samples": samples}, timeout=self.API_TIMEOUT
        )
        if not 200 <= r.status_code < 300:
            body = ""
            try:
                body = r.text[:200] if r.text else ""
            except Exception:
                pass
            self._write_log(
                f"PolarTrace: Failed to send host metrics - HTTP {r.status_code}: {body}"
            )
            raise RuntimeError(f"host-metrics HTTP {r.status_code}")

    def _queue_span(self, span: Dict[str, Any]) -> None:
        """Queue a trace span for flushing."""
        with self._trace_lock:
            self._trace_queue.append(span)
            if len(self._trace_queue) > self._MAX_TRACE_QUEUE:
                # Drop oldest to bound memory, like the host metrics sampler does.
                self._trace_queue = self._trace_queue[-self._MAX_TRACE_QUEUE:]

    def _setup_flush_timer(self) -> None:
        if not self._base_url:
            return

        def run():
            self._flush_logs()
            self._flush_traces()
            t = threading.Timer(self.FLUSH_INTERVAL, run)
            self._flush_timer = t
            t.daemon = True
            t.start()

        self._flush_timer = threading.Timer(self.FLUSH_INTERVAL, run)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    @staticmethod
    def _should_retain(status_code: int) -> bool:
        """Whether a failed batch is worth retrying.

        429 (collector backpressure) and 5xx/network problems are transient -
        keep the batch and retry on the next tick. Any other 4xx is a
        deterministic rejection (validation/auth): retrying the same bytes can
        never succeed, so the batch must be dropped or it blocks the queue
        head forever.
        """
        return status_code == 429 or status_code >= 500

    @staticmethod
    def _retry_after_seconds(response: Any) -> Optional[float]:
        """Parse a Retry-After header (delay in seconds) from a response, if present."""
        try:
            raw = response.headers.get("Retry-After")
            seconds = float(raw) if raw else None
            return seconds if seconds and seconds > 0 else None
        except (TypeError, ValueError, AttributeError):
            return None

    def _next_backoff(self, consecutive_failures: int, retry_after: Optional[float] = None) -> float:
        """Exponential backoff for retained failures: 10s, 20s, 40s... capped at
        ``_MAX_BACKOFF``. When the collector sent a Retry-After (429), honor the
        larger of the two."""
        backoff = min(self.FLUSH_INTERVAL * (2 ** (consecutive_failures - 1)), self._MAX_BACKOFF)
        return max(backoff, retry_after or 0.0)

    def _flush_logs(self) -> None:
        if self._is_flushing:
            return
        if time.monotonic() < self._log_backoff_until:
            # Backing off after a retained failure - skip this tick.
            return
        with self._log_lock:
            if not self._log_queue:
                return
            to_send = self._log_queue[:self._MAX_BATCH]
            del self._log_queue[:self._MAX_BATCH]

        self._is_flushing = True
        try:
            if self._session and self._log_url:
                r = self._session.post(self._log_url, json={"logs": to_send}, timeout=self.API_TIMEOUT)
                if r.status_code in range(200, 300):
                    self._log_consecutive_failures = 0
                    self._log_backoff_until = 0.0
                else:
                    try:
                        err_body = r.text[:500] if r.text else "(no body)"
                    except Exception:
                        err_body = "(could not read)"
                    self._write_log(
                        f"PolarTrace: Failed to send logs - HTTP {r.status_code}: {err_body}. "
                        f"Check POLARTRACE_LICENSE_KEY (must be API key from console) and POLARTRACE_APP_NAME (must match service name)."
                    )
                    if self._should_retain(r.status_code):
                        with self._log_lock:
                            self._log_queue = to_send + self._log_queue
                            if len(self._log_queue) > self._MAX_LOG_QUEUE:
                                self._log_queue = self._log_queue[-self._MAX_LOG_QUEUE:]
                        self._log_consecutive_failures += 1
                        retry_after = self._retry_after_seconds(r) if r.status_code == 429 else None
                        self._log_backoff_until = time.monotonic() + self._next_backoff(
                            self._log_consecutive_failures, retry_after
                        )
                    else:
                        # 4xx (other than 429) is deterministic: the collector will
                        # never accept this batch, so retrying forever would just
                        # block every newer log behind it and grow memory unbounded.
                        self._write_log(f"PolarTrace: Dropping {len(to_send)} rejected log(s) (HTTP {r.status_code}).")
                        self._log_consecutive_failures = 0
                        self._log_backoff_until = 0.0
        except Exception as e:
            self._write_log(f"PolarTrace: Failed to send logs: {e}. Check collector URL and network.")
            with self._log_lock:
                self._log_queue = to_send + self._log_queue
                if len(self._log_queue) > self._MAX_LOG_QUEUE:
                    self._log_queue = self._log_queue[-self._MAX_LOG_QUEUE:]
            self._log_consecutive_failures += 1
            self._log_backoff_until = time.monotonic() + self._next_backoff(self._log_consecutive_failures)
        finally:
            self._is_flushing = False

    def _flush_traces(self) -> None:
        if self._is_trace_flushing:
            return
        if time.monotonic() < self._trace_backoff_until:
            # Backing off after a retained failure - skip this tick.
            return
        with self._trace_lock:
            if not self._trace_queue:
                return
            to_send = self._trace_queue[:self._MAX_BATCH]
            del self._trace_queue[:self._MAX_BATCH]

        self._is_trace_flushing = True
        try:
            if self._session and self._traces_url:
                r = self._session.post(
                    self._traces_url, json={"spans": to_send}, timeout=self.API_TIMEOUT
                )
                if r.status_code in range(200, 300):
                    self._trace_consecutive_failures = 0
                    self._trace_backoff_until = 0.0
                else:
                    self._write_log(
                        f"PolarTrace: Failed to send traces - HTTP {r.status_code}: {r.text[:200] if r.text else '(no body)'}"
                    )
                    if self._should_retain(r.status_code):
                        with self._trace_lock:
                            self._trace_queue = to_send + self._trace_queue
                            if len(self._trace_queue) > self._MAX_TRACE_QUEUE:
                                self._trace_queue = self._trace_queue[-self._MAX_TRACE_QUEUE:]
                        self._trace_consecutive_failures += 1
                        retry_after = self._retry_after_seconds(r) if r.status_code == 429 else None
                        self._trace_backoff_until = time.monotonic() + self._next_backoff(
                            self._trace_consecutive_failures, retry_after
                        )
                    else:
                        self._write_log(f"PolarTrace: Dropping {len(to_send)} rejected span(s) (HTTP {r.status_code}).")
                        self._trace_consecutive_failures = 0
                        self._trace_backoff_until = 0.0
        except Exception as e:
            self._write_log(f"PolarTrace: Failed to send traces: {e}")
            with self._trace_lock:
                self._trace_queue = to_send + self._trace_queue
                if len(self._trace_queue) > self._MAX_TRACE_QUEUE:
                    self._trace_queue = self._trace_queue[-self._MAX_TRACE_QUEUE:]
            self._trace_consecutive_failures += 1
            self._trace_backoff_until = time.monotonic() + self._next_backoff(self._trace_consecutive_failures)
        finally:
            self._is_trace_flushing = False

    def _show_connection_status(self) -> None:
        if self._connection_status == "pending":
            return
        ok = self._connection_status == "connected"
        msg = (
            f"PolarTrace agent connected | service={self._service_name} endpoint={self._base_url}"
            if ok
            else f"PolarTrace agent FAILED to connect | service={self._service_name} "
                 f"endpoint={self._base_url} reason={self._connection_error}"
        )
        self._write_log(msg)
        if self._enable_console_log:
            print(f"[polartrace] {msg}")

    def _sanitize_body(self, body: Any) -> Any:
        sensitive = ["password", "token", "secret", "apikey", "authorization", "creditcard", "ssn"]

        def _sanitize(obj: Any) -> Any:
            if obj is None:
                return obj
            if isinstance(obj, str):
                return obj
            if isinstance(obj, (list, tuple)):
                return [_sanitize(x) for x in obj]
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    key_lower = k.lower()
                    if any(s in key_lower for s in sensitive):
                        out[k] = "[REDACTED]"
                    else:
                        out[k] = _sanitize(v)
                return out
            return obj

        return _sanitize(body)

    def _ensure_json_serializable(self, obj: Any) -> Any:
        """Convert obj to JSON-serializable form (collector expects plain dicts/str/numbers)."""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, (list, tuple)):
            return [self._ensure_json_serializable(x) for x in obj]
        if isinstance(obj, dict):
            return {str(k): self._ensure_json_serializable(v) for k, v in obj.items()}
        return str(obj)

    def _log_request(self, log: RequestLog) -> None:
        d = asdict(log)
        # Convert to camelCase for API compatibility; collector expects object/array types, not null
        api_log = {
            "timestamp": str(d["timestamp"]),
            "method": str(d["method"]),
            "path": str(d["path"]),
            "statusCode": d.get("status_code"),
            "duration": float(d["duration"]) if d.get("duration") is not None else 0.0,
            "startTime": str(d["start_time"]) if d.get("start_time") else None,
            "endTime": str(d["end_time"]) if d.get("end_time") else None,
            "headers": self._ensure_json_serializable(d["headers"]) if d.get("headers") else {},
            "query": self._ensure_json_serializable(d["query"]) if d.get("query") else {},
            "body": self._ensure_json_serializable(d.get("body")) if d.get("body") is not None else None,
            "userAgent": str(d["user_agent"]) if d.get("user_agent") else None,
            "ip": str(d["ip"]) if d.get("ip") else None,
            "host": str(d["host"]) if d.get("host") else None,
            "consoleLogs": self._ensure_json_serializable(d["console_logs"]) if d.get("console_logs") else [],
        }
        if d.get("error"):
            api_log["error"] = self._ensure_json_serializable(d["error"])
        # Omit absent optional fields instead of sending explicit nulls: the
        # collector's Joi schema treats null as a type violation on optional
        # keys, and a single null (e.g. a request with no User-Agent header)
        # would 400 the whole batch.
        api_log = {k: v for k, v in api_log.items() if v is not None}
        with self._log_lock:
            self._log_queue.append(api_log)
            if len(self._log_queue) > self._MAX_LOG_QUEUE:
                # Drop oldest to bound memory, like the host metrics sampler does.
                self._log_queue = self._log_queue[-self._MAX_LOG_QUEUE:]

    def _instrument_traces(self, app: Any, framework: str) -> None:
        """Instrument the app for OpenTelemetry tracing. Called by framework middleware."""
        if not self._trace_monitor or not getattr(self._trace_monitor, "initialized", False):
            return
        if framework == "flask" and app:
            self._trace_monitor.instrument_flask(app)
        elif framework == "fastapi" and app:
            self._trace_monitor.instrument_fastapi(app)
        elif framework == "django":
            self._trace_monitor.instrument_django()

    def middleware_flask(self):
        """Flask middleware factory."""
        from polartrace.middleware.flask import flask_middleware
        return flask_middleware(self)

    def middleware_django(self):
        """Django middleware factory."""
        from polartrace.middleware.django_mw import DjangoPolarTraceMiddleware
        return DjangoPolarTraceMiddleware(self)

    def middleware_fastapi(self):
        """Returns FastAPIPolarTraceMiddleware class. Use: app.add_middleware(agent.middleware_fastapi(), agent=self)"""
        from polartrace.middleware.fastapi_mw import FastAPIPolarTraceMiddleware
        return FastAPIPolarTraceMiddleware(self)

    def destroy(self) -> None:
        """Clean shutdown: stop timers, flush buffers."""
        if self._flush_timer:
            self._flush_timer.cancel()
        if self._trace_monitor:
            try:
                self._trace_monitor.destroy()
            except Exception:
                pass
        if self._host_metrics_monitor:
            try:
                self._host_metrics_monitor.destroy()
            except Exception:
                pass
        if self._log_stream:
            try:
                self._log_stream.close()
            except OSError:
                pass


def auto_init_from_env() -> Optional[PolarTrace]:
    """
    Auto-initialize PolarTrace from environment variables.
    POLARTRACE_APP_NAME, POLARTRACE_LICENSE_KEY required.
    Returns existing agent if already initialized (programmatic or env).
    """
    global _POLARTRACE_AGENT
    if _POLARTRACE_AGENT is not None:
        return _POLARTRACE_AGENT

    name = os.environ.get("POLARTRACE_APP_NAME")
    key = os.environ.get("POLARTRACE_LICENSE_KEY")
    if not name or not key:
        return _get_global_agent()

    enable_log = os.environ.get("POLARTRACE_ENABLE_CONSOLE_LOG", "").lower() in ("1", "true")

    agent = PolarTrace(
        api_key=key,
        service_name=name,
        enable_console_log=enable_log,
    )
    _POLARTRACE_AGENT = agent
    return agent
