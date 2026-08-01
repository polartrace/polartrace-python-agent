"""
Load application settings from a JSON file into process environment (POLARTRACE_* only).

See ``config.example.json`` in this package for the supported shape. Only the application
name, license key, and console-log toggle are user-configurable; the collector URL is a
code-level constant on :class:`polartrace.agent.PolarTrace` and is not read from this file.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional


def _merge_polartrace_section(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Top-level or nested "polartrace" block - both are supported.
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("polartrace")
    if isinstance(inner, dict):
        out = {k: v for k, v in data.items() if k != "polartrace"}
        out.update(inner)
        return out
    return dict(data)


def _truthy(v: Any) -> bool:
    if v is True:
        return True
    if v is None or v is False:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def load_file_as_dict(path: str) -> Dict[str, Any]:
    """
    Read ``path`` and return the merged config dict (no env writes).
    Used by ``polartrace-admin status`` to inspect what will be applied.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("PolarTrace: config file root must be a JSON object")
    return _merge_polartrace_section(data)


def apply_file_to_environ(path: str, *, clear_existing: bool = False) -> None:
    """
    Read ``path`` (UTF-8 JSON) and set POLARTRACE_* for known keys.

    Precedence rule: existing process env wins. A value already set in the environment is
    not overwritten by the file - this lets users pass ``POLARTRACE_LICENSE_KEY=...`` ad-hoc
    in front of ``polartrace-admin run-program`` to override the committed file.

    If ``clear_existing`` is True, unsets prior POLARTRACE_APP_NAME, POLARTRACE_LICENSE_KEY,
    POLARTRACE_ENABLE_CONSOLE_LOG before applying (for testing only).

    ``collector_url`` / ``endpoint`` keys in the file are intentionally ignored - the
    collector URL is fixed in code.
    """
    if clear_existing:
        for k in (
            "POLARTRACE_APP_NAME",
            "POLARTRACE_LICENSE_KEY",
            "POLARTRACE_ENABLE_CONSOLE_LOG",
        ):
            os.environ.pop(k, None)

    d = load_file_as_dict(path)

    def _setdefault(env_key: str, value: Optional[str]) -> None:
        if value and not os.environ.get(env_key):
            os.environ[env_key] = value

    for k in ("app_name", "service"):
        if d.get(k):
            _setdefault("POLARTRACE_APP_NAME", str(d[k]).strip())
            break

    if d.get("license_key"):
        _setdefault("POLARTRACE_LICENSE_KEY", str(d["license_key"]).strip())

    if "console_log" in d:
        if _truthy(d["console_log"]):
            _setdefault("POLARTRACE_ENABLE_CONSOLE_LOG", "1")
