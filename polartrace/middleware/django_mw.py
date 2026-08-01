"""Django middleware for PolarTrace."""

import time
import json

from polartrace.agent import auto_init_from_env


class DjangoPolarTraceMiddleware:
    """
    Django middleware that captures request logs and sends to PolarTrace.
    Add to MIDDLEWARE in settings.py (as early as possible):
        'polartrace.middleware.django_mw.DjangoPolarTraceMiddleware',

    Uses POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY from environment.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.agent = auto_init_from_env()
        if self.agent:
            self.agent._instrument_traces(None, "django")
        if not self.agent:
            raise RuntimeError(
                "PolarTrace: Set POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY "
                "or initialize PolarTrace before adding middleware"
            )

    def __call__(self, request):
        return self._process_request(request, self.get_response)

    def _process_request(self, request, get_response):
        start = time.time()
        start_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(start))

        def get_client_ip():
            xff = request.META.get("HTTP_X_FORWARDED_FOR")
            if xff:
                return xff.split(",")[0].strip()
            return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "unknown")

        body = None
        if self.agent._capture_body and request.body:
            try:
                body = json.loads(request.body)
            except (json.JSONDecodeError, ValueError):
                body = None
        if body and self.agent._sanitize_body:
            body = self.agent._sanitize_body(body)

        query = dict(request.GET) if self.agent._capture_query and request.GET else None
        headers = None
        if self.agent._capture_headers:
            headers = {k: v for k, v in request.META.items() if k.startswith("HTTP_")}

        response = get_response(request)

        duration_ms = (time.time() - start) * 1000
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        from polartrace.agent import RequestLog

        request_log = RequestLog(
            timestamp=start_iso,
            method=request.method,
            path=request.path,
            status_code=response.status_code if hasattr(response, "status_code") else 200,
            duration=duration_ms,
            start_time=start_iso,
            end_time=end_iso,
            headers=headers,
            query=query,
            body=body,
            user_agent=request.META.get("HTTP_USER_AGENT"),
            ip=get_client_ip(),
            host=request.get_host(),
            framework="django",
        )
        self.agent._log_request(request_log)
        return response
