"""Flask middleware for PolarTrace - request logging only."""

import time
from typing import Any

from polartrace.agent import PolarTrace, RequestLog


def _get_client_ip(environ: dict) -> str:
    return (
        environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or environ.get("HTTP_X_REAL_IP", "")
        or environ.get("REMOTE_ADDR", "unknown")
    )


def flask_middleware(agent: PolarTrace):
    """Flask middleware: captures request logs and instruments for tracing."""

    def middleware(app: Any):
        agent._instrument_traces(app, "flask")
        @app.before_request
        def before():
            from flask import g
            g._polartrace_start = time.time()

        @app.after_request
        def after(response):
            from flask import g, request
            if not hasattr(g, "_polartrace_start"):
                return response

            duration_ms = (time.time() - g._polartrace_start) * 1000
            start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(g._polartrace_start))
            end_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

            body = None
            if agent._capture_body:
                body = request.get_json(silent=True)
                if body:
                    body = agent._sanitize_body(body)

            request_log = RequestLog(
                timestamp=start_iso,
                method=request.method,
                path=request.path,
                status_code=response.status_code,
                duration=duration_ms,
                start_time=start_iso,
                end_time=end_iso,
                headers=dict(request.headers) if agent._capture_headers else None,
                query=dict(request.args) if agent._capture_query and request.args else None,
                body=body,
                user_agent=request.headers.get("User-Agent"),
                ip=_get_client_ip(request.environ),
                host=request.host,
                framework="flask",
            )
            agent._log_request(request_log)
            return response

        return app

    return middleware
