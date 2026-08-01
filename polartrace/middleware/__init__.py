"""
Framework-specific PolarTrace middleware (Flask, Django, FastAPI).

Imports are lazy so installing one framework doesn't pull the other two. This is what
makes the agent truly framework-agnostic: a Flask-only project doesn't need starlette,
a Django-only project doesn't need Flask, etc.

Use direct submodule imports if you want to skip the laziness:

    from polartrace.middleware.flask import flask_middleware
    from polartrace.middleware.django_mw import DjangoPolarTraceMiddleware
    from polartrace.middleware.fastapi_mw import FastAPIPolarTraceMiddleware
"""

from importlib import import_module
from typing import Any

__all__ = ["flask_middleware", "DjangoPolarTraceMiddleware", "FastAPIPolarTraceMiddleware"]

_LAZY = {
    "flask_middleware": ("polartrace.middleware.flask", "flask_middleware"),
    "DjangoPolarTraceMiddleware": ("polartrace.middleware.django_mw", "DjangoPolarTraceMiddleware"),
    "FastAPIPolarTraceMiddleware": ("polartrace.middleware.fastapi_mw", "FastAPIPolarTraceMiddleware"),
}


def __getattr__(name: str) -> Any:  # PEP 562
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'polartrace.middleware' has no attribute {name!r}")
    module_name, attr = target
    return getattr(import_module(module_name), attr)
