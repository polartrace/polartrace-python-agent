"""
PolarTrace Python Agent - observability for Django, Flask, and FastAPI.

**One config file, any framework (Flask / FastAPI auto-hook; Django: add one middleware in settings):**

- Put credentials in a JSON file (see ``polartrace/config.example.json``), set
  ``POLARTRACE_CONFIG_FILE`` to that path, then start your app with
  ``polartrace-admin run-program python …`` (or ``python -m polartrace …`` with env set).

**Env-only:** ``POLARTRACE_APP_NAME`` and ``POLARTRACE_LICENSE_KEY`` then ``python -m polartrace myapp.py``.

**Manual:** ``from polartrace import PolarTrace`` and attach framework middleware in code.
"""

from polartrace.agent import PolarTrace

__version__ = "1.0.0"
__all__ = ["PolarTrace"]
