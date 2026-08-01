"""
TraceMonitor - OpenTelemetry-based tracing for PolarTrace.
Captures spans from HTTP/FastAPI/Flask/Django and forwards to agent callback.
"""

import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
    from opentelemetry.sdk.trace.export import SpanProcessor
    from opentelemetry.sdk.resources import Resource
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False
    trace = None
    TracerProvider = None
    ReadableSpan = object
    SpanProcessor = object
    Resource = None

# --- Framework instrumentors (incoming HTTP - Flask / FastAPI / Django) ---------- #
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FASTAPI_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    FastAPIInstrumentor = None
    FASTAPI_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    FLASK_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    FlaskInstrumentor = None
    FLASK_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    DJANGO_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    DjangoInstrumentor = None
    DJANGO_INSTRUMENTOR_AVAILABLE = False

# --- Outgoing HTTP client instrumentors (mirrors Node.js native http/https patch) - #
try:
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    REQUESTS_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    RequestsInstrumentor = None
    REQUESTS_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
    URLLIB3_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    URLLib3Instrumentor = None
    URLLIB3_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPX_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    HTTPXClientInstrumentor = None
    HTTPX_INSTRUMENTOR_AVAILABLE = False

# --- Database instrumentors (mirrors Node.js MongoDB + adds SQL / Redis) --------- #
try:
    from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    PYMONGO_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    PymongoInstrumentor = None
    PYMONGO_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    SQLALCHEMY_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    SQLAlchemyInstrumentor = None
    SQLALCHEMY_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
    PSYCOPG2_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    Psycopg2Instrumentor = None
    PSYCOPG2_INSTRUMENTOR_AVAILABLE = False

try:
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    REDIS_INSTRUMENTOR_AVAILABLE = True
except ImportError:
    RedisInstrumentor = None
    REDIS_INSTRUMENTOR_AVAILABLE = False


# Stable namespace used to derive a deterministic UUID5 from each 16-char OTEL span ID.
# Using a fixed namespace means every span ID maps to the *same* UUID across the process,
# so child spans' parentSpanId references survive the conversion. This mirrors the Node.js
# agent's ``spanIdMap`` behaviour (same input → same output) without the memory overhead.
_POLARTRACE_SPAN_ID_NS = uuid.UUID("8c9e6679-7425-40de-944b-e07fc1f90ae7")


def _span_id_to_uuid(span_id: str) -> str:
    """
    Convert an OTEL span ID (16-char hex) to a 32-char UUID hex.

    Deterministic: the same input always returns the same output, so parent/child
    relationships in the emitted span graph stay correctly linked after conversion.
    """
    if not span_id:
        return span_id
    if len(span_id) == 32:
        return span_id
    return uuid.uuid5(_POLARTRACE_SPAN_ID_NS, span_id).hex


class PolarTraceSpanProcessor(SpanProcessor):
    """Custom SpanProcessor that forwards spans to PolarTrace callback."""

    def __init__(
        self,
        on_span: Callable[[Dict[str, Any]], None],
        enable_console_log: bool = False,
        exclude_agent_spans: bool = True,
        agent_endpoints: Optional[List[str]] = None,
    ):
        self._on_span = on_span
        self._enable_console_log = enable_console_log
        self._exclude_agent_spans = exclude_agent_spans
        self._agent_endpoints = agent_endpoints or ["/api/traces", "/api/log", "/api/logs"]

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        try:
            if self._exclude_agent_spans and self._is_agent_span(span):
                return
            # Filter internal noise
            if self._is_noise_span(span):
                return

            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if hasattr(ctx.trace_id, "__format__") else str(ctx.trace_id)
            span_id = format(ctx.span_id, "016x") if hasattr(ctx.span_id, "__format__") else str(ctx.span_id)
            span_id_uuid = _span_id_to_uuid(span_id)

            parent_span_id = None
            if span.parent and hasattr(span.parent, "span_id"):
                ps = span.parent.span_id
                parent_span_id = _span_id_to_uuid(format(ps, "016x") if hasattr(ps, "__format__") else str(ps))

            start_ns = span.start_time
            end_ns = span.end_time
            if hasattr(span.start_time, "__iter__"):
                start_ns = span.start_time[0] * 1_000_000_000 + span.start_time[1]
            if hasattr(span.end_time, "__iter__"):
                end_ns = span.end_time[0] * 1_000_000_000 + span.end_time[1]
            duration_ms = (end_ns - start_ns) / 1_000_000

            kind_map = {1: "SERVER", 2: "CLIENT", 3: "PRODUCER", 4: "CONSUMER", 5: "INTERNAL"}
            kind = kind_map.get(getattr(span.kind, "value", 5), "INTERNAL")

            status_code = "ERROR" if span.status.is_ok is False else "OK"
            attrs = dict(span.attributes) if span.attributes else {}

            # Events: OTEL span events become {name, timestamp, attributes}
            events: List[Dict[str, Any]] = []
            for ev in getattr(span, "events", None) or []:
                events.append({
                    "name": getattr(ev, "name", str(ev)),
                    "timestamp": int(getattr(ev, "timestamp", 0) or 0),
                    "attributes": self._sanitize_attrs(dict(getattr(ev, "attributes", {}) or {})),
                })

            # Links: explicit cross-trace references. Node.js sends {traceId, spanId} pairs.
            links: List[Dict[str, Any]] = []
            for link in getattr(span, "links", None) or []:
                ctx = getattr(link, "context", None)
                if ctx is None:
                    continue
                links.append({
                    "traceId": format(ctx.trace_id, "032x"),
                    "spanId": _span_id_to_uuid(format(ctx.span_id, "016x")),
                })

            # Resource: service-level metadata (service.name, service.version, etc.)
            resource_attrs: Dict[str, Any] = {}
            res = getattr(span, "resource", None)
            if res is not None and getattr(res, "attributes", None):
                resource_attrs = dict(res.attributes)

            trace_span = {
                "traceId": trace_id,
                "spanId": span_id_uuid,
                "parentSpanId": parent_span_id,
                "name": span.name,
                "kind": kind,
                "startTime": int(start_ns),
                "endTime": int(end_ns),
                "duration": duration_ms,
                "status": {"code": status_code, "message": getattr(span.status, "description", None)},
                "attributes": self._sanitize_attrs(attrs),
                "events": events,
                "links": links,
                "resource": resource_attrs,
            }
            self._on_span(trace_span)
        except Exception as e:
            if self._enable_console_log:
                print(f"TraceMonitor: Error processing span: {e}")

    def _is_agent_span(self, span: ReadableSpan) -> bool:
        attrs = span.attributes or {}
        url = attrs.get("http.url") or attrs.get("url") or ""
        return any(ep in str(url) for ep in self._agent_endpoints)

    def _is_noise_span(self, span: ReadableSpan) -> bool:
        name = (span.name or "").lower()
        noise = ["tcp.connect", "dns.lookup", "net.connect", "socket.connect"]
        return any(n in name for n in noise)

    def _sanitize_attrs(self, attrs: Dict[str, Any]) -> Dict[str, Any]:
        sensitive = ["password", "token", "secret", "apikey", "authorization"]
        out = {}
        for k, v in attrs.items():
            if any(s in k.lower() for s in sensitive):
                out[k] = "[REDACTED]"
            else:
                try:
                    out[k] = v
                except Exception:
                    out[k] = str(v)
        return out

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class TraceMonitor:
    """OpenTelemetry trace monitor with custom span processor for PolarTrace."""

    def __init__(
        self,
        service_name: str,
        service_version: str = "1.0.0",
        enable_console_log: bool = False,
        exclude_agent_spans: bool = True,
        agent_endpoints: Optional[List[str]] = None,
        on_span: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self._service_name = service_name
        self._service_version = service_version
        self._enable_console_log = enable_console_log
        self._on_span = on_span or (lambda s: None)
        self._provider: Optional[TracerProvider] = None
        self._processor: Optional[PolarTraceSpanProcessor] = None
        self._initialized = False
        self._exclude_agent_spans = exclude_agent_spans
        self._agent_endpoints = agent_endpoints

    @property
    def initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> None:
        if not OTEL_AVAILABLE or trace is None or TracerProvider is None or Resource is None:
            if self._enable_console_log:
                print("TraceMonitor: OpenTelemetry not installed, tracing disabled")
            return

        if self._initialized:
            return

        self._processor = PolarTraceSpanProcessor(
            on_span=self._on_span,
            enable_console_log=self._enable_console_log,
            exclude_agent_spans=self._exclude_agent_spans,
            agent_endpoints=self._agent_endpoints,
        )

        resource = Resource.create({
            "service.name": self._service_name,
            "service.version": self._service_version,
        })

        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(self._processor)
        trace.set_tracer_provider(self._provider)
        self._initialized = True

        # Auto-instrument process-wide integrations: outgoing HTTP clients + databases.
        # Framework instrumentors (Flask/FastAPI/Django) are still attached per-app via
        # ``instrument_flask`` / ``instrument_fastapi`` / ``instrument_django`` because
        # they need an app handle. Everything else patches the underlying library and
        # captures every call automatically.
        self._auto_instrument_clients()

        if self._enable_console_log:
            print("TraceMonitor: Initialized")

    def _auto_instrument_clients(self) -> None:
        """
        Patch outgoing HTTP clients and DB drivers if they're installed AND the matching
        OpenTelemetry instrumentation extra is available. Each call is wrapped in
        try/except so a missing/broken instrumentor never blocks the others.
        """
        candidates = [
            ("requests", REQUESTS_INSTRUMENTOR_AVAILABLE, RequestsInstrumentor),
            ("urllib3", URLLIB3_INSTRUMENTOR_AVAILABLE, URLLib3Instrumentor),
            ("httpx", HTTPX_INSTRUMENTOR_AVAILABLE, HTTPXClientInstrumentor),
            ("pymongo", PYMONGO_INSTRUMENTOR_AVAILABLE, PymongoInstrumentor),
            ("sqlalchemy", SQLALCHEMY_INSTRUMENTOR_AVAILABLE, SQLAlchemyInstrumentor),
            ("psycopg2", PSYCOPG2_INSTRUMENTOR_AVAILABLE, Psycopg2Instrumentor),
            ("redis", REDIS_INSTRUMENTOR_AVAILABLE, RedisInstrumentor),
        ]
        self._auto_instrumented: List[Any] = []
        for name, available, instrumentor_cls in candidates:
            if not (available and instrumentor_cls):
                continue
            try:
                inst = instrumentor_cls()
                # OpenTelemetry instrumentors are singletons; calling instrument() twice
                # raises - guard with is_instrumented_by_opentelemetry where available.
                if getattr(inst, "is_instrumented_by_opentelemetry", False):
                    continue
                inst.instrument()
                self._auto_instrumented.append(inst)
                if self._enable_console_log:
                    print(f"TraceMonitor: auto-instrumented {name}")
            except Exception as e:  # pylint: disable=broad-except
                if self._enable_console_log:
                    print(f"TraceMonitor: failed to instrument {name}: {e}")

    def instrument_fastapi(self, app: Any) -> None:
        """Instrument FastAPI app if opentelemetry-instrumentation-fastapi is available."""
        if FASTAPI_INSTRUMENTOR_AVAILABLE and FastAPIInstrumentor and app:
            FastAPIInstrumentor.instrument_app(app)

    def instrument_flask(self, app: Any) -> None:
        """Instrument Flask app if opentelemetry-instrumentation-flask is available."""
        if FLASK_INSTRUMENTOR_AVAILABLE and FlaskInstrumentor and app:
            FlaskInstrumentor.instrument_app(app)

    def instrument_django(self) -> None:
        """Instrument Django if opentelemetry-instrumentation-django is available."""
        if DJANGO_INSTRUMENTOR_AVAILABLE and DjangoInstrumentor:
            DjangoInstrumentor().instrument()

    def destroy(self) -> None:
        # Un-instrument anything we patched so re-init in the same process works cleanly.
        for inst in getattr(self, "_auto_instrumented", []):
            try:
                inst.uninstrument()
            except Exception:
                pass
        self._auto_instrumented = []

        if self._provider:
            try:
                self._provider.shutdown()
            except Exception:
                pass
        self._initialized = False
