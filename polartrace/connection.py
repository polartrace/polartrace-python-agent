"""
Connection probe for the PolarTrace collector.

Used by ``polartrace-admin test`` and by the ``PolarTrace`` agent at startup.
The collector URL is **not** user-configurable; it is the ``BASE_URL`` constant on
``PolarTrace`` and changing it requires a code edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - requests is a hard dep, but be defensive
    requests = None  # type: ignore

from polartrace.agent import PolarTrace


def collector_base_url() -> str:
    """Single source of truth for the collector base URL."""
    return PolarTrace.BASE_URL


@dataclass
class ProbeResult:
    ok: bool
    status: str
    http_status: Optional[int] = None
    message: Optional[str] = None
    app_name: Optional[str] = None

    def format_human(self) -> str:
        lines = [
            f"PolarTrace connection: {'OK' if self.ok else 'FAILED'}",
            f"  app_name  : {self.app_name or '(not set)'}",
        ]
        if self.http_status is not None:
            lines.append(f"  http      : {self.http_status}")
        if self.message:
            lines.append(f"  detail    : {self.message}")
        return "\n".join(lines)


def probe(
    *,
    api_key: Optional[str],
    app_name: Optional[str],
    timeout: float = 8.0,
) -> ProbeResult:
    """
    POST {BASE_URL}/service/validate with the credential headers used by the agent.
    Returns a ProbeResult; never raises.
    """
    base = collector_base_url()

    if requests is None:
        return ProbeResult(
            ok=False,
            status="missing-dependency",
            app_name=app_name,
            message="the 'requests' package is not installed in this environment",
        )
    if not api_key or len(api_key) < 10:
        return ProbeResult(
            ok=False,
            status="invalid-key",
            app_name=app_name,
            message="POLARTRACE_LICENSE_KEY missing or shorter than 10 characters",
        )
    if not app_name:
        return ProbeResult(
            ok=False,
            status="missing-app-name",
            message="POLARTRACE_APP_NAME is not set",
        )

    url = f"{base}/service/validate"
    headers = {
        "Content-Type": "application/json",
        "internal-access-token": api_key,
        "service-name": app_name,
    }
    try:
        r = requests.post(url, json={}, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        return ProbeResult(
            ok=False,
            status="connection-refused",
            app_name=app_name,
            message=f"could not reach collector: {e}",
        )
    except requests.exceptions.Timeout:
        return ProbeResult(
            ok=False,
            status="timeout",
            app_name=app_name,
            message=f"collector did not respond within {timeout}s",
        )
    except Exception as e:  # pylint: disable=broad-except
        return ProbeResult(
            ok=False,
            status="error",
            app_name=app_name,
            message=str(e),
        )

    if 200 <= r.status_code < 300:
        return ProbeResult(
            ok=True,
            status="connected",
            http_status=r.status_code,
            app_name=app_name,
        )

    detail: Optional[str] = None
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or None
    except Exception:  # pylint: disable=broad-except
        detail = (r.text or "")[:200] or None

    if r.status_code == 401:
        status = "unauthorized"
        detail = detail or "license key was rejected by the collector"
    elif r.status_code == 404:
        status = "service-not-found"
        detail = detail or "create the service in the PolarTrace console first, then retry"
    elif r.status_code == 400:
        status = "bad-request"
    else:
        status = f"http-{r.status_code}"

    return ProbeResult(
        ok=False,
        status=status,
        http_status=r.status_code,
        app_name=app_name,
        message=detail,
    )
