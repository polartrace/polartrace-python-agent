"""
Build argv for ``python -m polartrace ...`` and execute the real interpreter.
"""

from __future__ import annotations

import os
import runpy
import sys
import typing as t


def _usage(stream: t.TextIO = sys.stdout) -> None:
    print(
        "Usage:\n"
        "  python -m polartrace <script.py> [arg ...]\n"
        "  python -m polartrace -m <module> [arg ...]\n"
        "Applies when POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY are set (e.g. from a JSON config).\n",
        end="",
        file=stream,
    )


def is_python_interpreter_invocation(executable: str) -> bool:
    base = os.path.basename(executable).lower()
    if base in ("python", "python3", "python2"):
        return True
    if base.startswith("python3.") or base.startswith("python2."):
        return True
    if base.endswith("python.exe") or base.endswith("python3.exe") or base.endswith("python2.exe"):
        return True
    return False


def build_polartrace_argv(python_path: str, user_rest: t.List[str]) -> t.List[str]:
    """
    ``python_path`` = argv[0] of the user command (e.g. python, /venv/bin/python3).
    ``user_rest`` = argv[1:] (e.g. [\"-m\", \"uvicorn\", \"app:app\"] or [\"app.py\", \"--help\"]).
    """
    if not user_rest:
        return [python_path, "-m", "polartrace"]
    if user_rest[0] == "-m":
        return [python_path, "-m", "polartrace", "-m", *user_rest[1:]]
    return [python_path, "-m", "polartrace", *user_rest]


def run_polartrace_module_main() -> None:
    """``python -m polartrace`` - bootstrap, then run script or -m module."""
    import polartrace.bootstrap as _bootstrap

    _bootstrap.install()

    args = list(sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        _usage()
        sys.exit(0 if args and args[0] in ("-h", "--help") else 2)

    if args[0] == "-m":
        if len(args) < 2:
            _usage(sys.stderr)
            sys.exit(2)
        mod = args[1]
        sys.argv = [mod] + args[2:]
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
        return

    script = args[0]
    sys.argv = [script] + args[1:]
    runpy.run_path(script, run_name="__main__")
