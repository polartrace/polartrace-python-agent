"""FastAPI middleware for PolarTrace."""

import time
import json
from typing import Any, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class FastAPIPolarTraceMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware that captures request logs.
    Add via: app.add_middleware(FastAPIPolarTraceMiddleware, agent=agent)
    """

    def __init__(self, app, agent=None):
        if agent is None:
            from polartrace.agent import auto_init_from_env
            agent = auto_init_from_env()
        if not agent:
            raise RuntimeError(
                "PolarTrace: Set POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY "
                "or pass agent= to add_middleware(FastAPIPolarTraceMiddleware, agent=agent)"
            )
        self.agent = agent
        agent._instrument_traces(app, "fastapi")
        super().__init__(app, self.dispatch)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.time()
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start))

        def get_client_ip():
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
            return request.headers.get("x-real-ip") or request.client.host if request.client else "unknown"

        body = None
        if self.agent._capture_body:
            try:
                body_bytes = await request.body()
                if body_bytes:
                    body = json.loads(body_bytes)
            except (json.JSONDecodeError, ValueError):
                body = None
        if body and self.agent._sanitize_body:
            body = self.agent._sanitize_body(body)

        query = dict(request.query_params) if self.agent._capture_query and request.query_params else None
        headers = dict(request.headers) if self.agent._capture_headers else None

        response = await call_next(request)

        duration_ms = (time.time() - start) * 1000
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        from polartrace.agent import RequestLog

        request_log = RequestLog(
            timestamp=start_iso,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration=duration_ms,
            start_time=start_iso,
            end_time=end_iso,
            headers=headers,
            query=query,
            body=body,
            user_agent=request.headers.get("user-agent"),
            ip=get_client_ip(),
            host=request.headers.get("host"),
            framework="fastapi",
        )
        self.agent._log_request(request_log)
        return response
