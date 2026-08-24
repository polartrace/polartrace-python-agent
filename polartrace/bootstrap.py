"""
Pre-import hooks so Flask / FastAPI / Django apps get instrumented when env has credentials.

Call install() before any ``import flask`` / ``import fastapi`` / ``import django``, or use
``python -m polartrace`` / ``polartrace-admin run-program`` which does that for you.

Each framework is patched independently and silently no-ops when the framework isn't
installed, so installing only one framework never pulls dependencies for the other two.
"""

from __future__ import annotations

import os
from typing import Any, Callable

_INSTALLED: bool = False
_agent: Any = None


def _env_ok() -> bool:
    return bool(os.environ.get("POLARTRACE_APP_NAME") and os.environ.get("POLARTRACE_LICENSE_KEY"))


def install() -> None:
    """
    If POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY are set, create the global agent
    and patch Flask / FastAPI / Django to attach middleware automatically. Idempotent.
    """
    global _INSTALLED, _agent
    if _INSTALLED:
        return
    if not _env_ok():
        return

    from polartrace.agent import auto_init_from_env

    _agent = auto_init_from_env()
    if _agent is None:
        return

    _try_patch_flask(_agent)
    _try_patch_fastapi(_agent)
    _try_patch_django(_agent)
    _INSTALLED = True


def _try_patch_flask(agent: Any) -> None:
    try:
        import flask  # type: ignore
    except ImportError:
        return

    if getattr(flask.Flask, "_polartrace_patched", False):
        return

    _orig: Callable = flask.Flask.__init__

    def _wrapped_init(self: Any, *a: Any, **kw: Any) -> None:
        _orig(self, *a, **kw)
        if getattr(self, "_polartrace_middleware_attached", False):
            return
        self._polartrace_middleware_attached = True
        agent.middleware_flask()(self)

    flask.Flask.__init__ = _wrapped_init  # type: ignore[assignment]
    flask.Flask._polartrace_patched = True  # type: ignore[attr-defined]


def _try_patch_fastapi(agent: Any) -> None:
    try:
        import fastapi  # type: ignore
    except ImportError:
        return

    if getattr(fastapi.FastAPI, "_polartrace_patched", False):
        return

    from polartrace.middleware.fastapi_mw import FastAPIPolarTraceMiddleware

    _orig: Callable = fastapi.FastAPI.__init__

    def _wrapped_init(self: Any, *a: Any, **kw: Any) -> None:
        _orig(self, *a, **kw)
        if getattr(self, "_polartrace_middleware_attached", False):
            return
        self._polartrace_middleware_attached = True
        self.add_middleware(FastAPIPolarTraceMiddleware, agent=agent)
        # Instrument traces here, where `self` is the actual FastAPI app.
        # (Middleware constructors only ever see the next ASGI app in the chain.)
        agent._instrument_traces(self, "fastapi")

    fastapi.FastAPI.__init__ = _wrapped_init  # type: ignore[assignment]
    fastapi.FastAPI._polartrace_patched = True  # type: ignore[attr-defined]


_DJANGO_MIDDLEWARE_PATH = "polartrace.middleware.django_mw.DjangoPolarTraceMiddleware"


def _try_patch_django(agent: Any) -> None:
    """
    Zero-code Django integration.

    Wraps ``django.core.handlers.base.BaseHandler.load_middleware`` so the first time
    Django builds its handler we prepend ``DjangoPolarTraceMiddleware`` to
    ``settings.MIDDLEWARE``. The user never edits ``settings.py``.

    Importing ``django.core.handlers.base`` does not load the user's settings - it
    only defines the BaseHandler class. The mutation happens later, inside
    ``load_middleware`` when settings have been resolved.
    """
    del agent  # The middleware reads the singleton via auto_init_from_env() itself.
    try:
        from django.core.handlers.base import BaseHandler  # type: ignore
    except ImportError:
        return

    if getattr(BaseHandler, "_polartrace_patched", False):
        return

    _orig: Callable = BaseHandler.load_middleware

    def _wrapped_load_middleware(self: Any, *a: Any, **kw: Any) -> Any:
        try:
            from django.conf import settings  # type: ignore
            current = list(getattr(settings, "MIDDLEWARE", None) or [])
            if _DJANGO_MIDDLEWARE_PATH not in current:
                settings.MIDDLEWARE = [_DJANGO_MIDDLEWARE_PATH, *current]
            # OTel's Django instrumentor works by inserting its own middleware
            # into settings.MIDDLEWARE, so it must run before Django reads the
            # list here - calling it any later (e.g. from the PolarTrace
            # middleware's __init__, which runs inside load_middleware) means
            # no SERVER spans are ever produced.
            from polartrace.agent import auto_init_from_env
            _agent = auto_init_from_env()
            if _agent:
                _agent._instrument_traces(None, "django")
        except Exception:
            # Never break Django's own startup; fall through to original behaviour.
            pass
        return _orig(self, *a, **kw)

    BaseHandler.load_middleware = _wrapped_load_middleware  # type: ignore[assignment]
    BaseHandler._polartrace_patched = True  # type: ignore[attr-defined]
