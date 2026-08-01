"""
``polartrace-admin`` - configuration and launcher CLI.

Subcommands:

    init         Scaffold a polartrace.config.json in the current directory.
    test         Validate API key / app name against the collector.
    status       Print the resolved configuration (file + env precedence).
    run-program  Load config and start the given Python command with auto-instrumentation.

The collector URL is hard-coded in :class:`polartrace.agent.PolarTrace` and is not
user-configurable through any of these subcommands.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import typing as t

from polartrace.config_loader import apply_file_to_environ, load_file_as_dict
from polartrace.connection import collector_base_url, probe
from polartrace.launcher import build_polartrace_argv, is_python_interpreter_invocation

DEFAULT_CONFIG_NAMES = ("polartrace.config.json", "polartrace.app.json", "polartrace.json")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _discover_config_path() -> t.Optional[str]:
    explicit = os.environ.get("POLARTRACE_CONFIG_FILE", "").strip()
    if explicit:
        return os.path.abspath(explicit) if os.path.isfile(explicit) else None
    for name in DEFAULT_CONFIG_NAMES:
        if os.path.isfile(name):
            return os.path.abspath(name)
    return None


def _resolve_python(exe: str) -> str:
    cand = os.path.expanduser(exe)
    if os.path.isfile(cand):
        return cand
    w = shutil.which(exe)
    return w or cand


def _eprint(*a: t.Any) -> None:
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def _cmd_init(args: argparse.Namespace) -> int:
    target = os.path.abspath(args.path or DEFAULT_CONFIG_NAMES[0])
    if os.path.exists(target) and not args.force:
        _eprint(f"polartrace-admin init: {target} already exists (use --force to overwrite)")
        return 1

    app_name = (args.name or "").strip() or "my-service"
    api_key = (args.key or "").strip() or "your-license-key-minimum-10-characters"

    payload = {
        "polartrace": {
            "app_name": app_name,
            "license_key": api_key,
            "console_log": False,
        }
    }
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"Wrote {target}")
    print()
    print("Next steps:")
    print(f"  1. Edit {os.path.basename(target)} and set 'license_key' (from the PolarTrace console).")
    print(f"  2. Run    polartrace-admin test")
    print(f"  3. Start  polartrace-admin run-program python app.py   (or your usual command)")
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _resolved_config() -> t.Dict[str, str]:
    """
    Effective config for the current process.
    Reads config file (if discoverable) into a local dict, then overlays env vars
    so the user can see what 'run' / 'test' will actually use.
    """
    out: t.Dict[str, str] = {
        "app_name": "",
        "license_key": "",
        "console_log": "",
        "config_file": "",
    }

    path = _discover_config_path()
    if path:
        out["config_file"] = path
        try:
            d = load_file_as_dict(path)
            out["app_name"] = str(d.get("app_name") or d.get("service") or "")
            out["license_key"] = str(d.get("license_key") or "")
            cl = d.get("console_log")
            out["console_log"] = "1" if cl in (True, "1", "true", "yes", "on") else ""
        except Exception as e:  # pylint: disable=broad-except
            _eprint(f"polartrace-admin: failed to parse {path}: {e}")

    # env overrides
    for env_key, slot in (
        ("POLARTRACE_APP_NAME", "app_name"),
        ("POLARTRACE_LICENSE_KEY", "license_key"),
        ("POLARTRACE_ENABLE_CONSOLE_LOG", "console_log"),
    ):
        v = os.environ.get(env_key)
        if v:
            out[slot] = v
    return out


def _redact(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "********"
    return f"{key[:4]}…{key[-4:]}"


def _tracing_readiness() -> t.Tuple[bool, t.List[str], t.List[str]]:
    """
    Inspect which of the bundled instrumentors are actually usable in this venv.

    Every instrumentor ships with the base ``polartrace`` install, so the only reason
    one would be missing is if the underlying client library it patches isn't
    importable. An instrumentor is "usable" only when both the instrumentor module
    AND its target library import cleanly - that's what determines whether spans
    will actually be produced at runtime.

    Returns (sdk_ok, usable_names, dormant_names_with_missing_lib).
    """
    try:
        import opentelemetry.sdk  # noqa: F401
        sdk_ok = True
    except ImportError:
        return False, [], []

    candidates = [
        # (label, instrumentor module, attr, target_library_module)
        ("flask",      "opentelemetry.instrumentation.flask",      "FlaskInstrumentor",      "flask"),
        ("fastapi",    "opentelemetry.instrumentation.fastapi",    "FastAPIInstrumentor",    "fastapi"),
        ("django",     "opentelemetry.instrumentation.django",     "DjangoInstrumentor",     "django"),
        ("requests",   "opentelemetry.instrumentation.requests",   "RequestsInstrumentor",   "requests"),
        ("urllib3",    "opentelemetry.instrumentation.urllib3",    "URLLib3Instrumentor",    "urllib3"),
        ("httpx",      "opentelemetry.instrumentation.httpx",      "HTTPXClientInstrumentor","httpx"),
        ("pymongo",    "opentelemetry.instrumentation.pymongo",    "PymongoInstrumentor",    "pymongo"),
        ("sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor", "sqlalchemy"),
        ("psycopg2",   "opentelemetry.instrumentation.psycopg2",   "Psycopg2Instrumentor",   "psycopg2"),
        ("redis",      "opentelemetry.instrumentation.redis",      "RedisInstrumentor",      "redis"),
    ]
    usable: t.List[str] = []
    dormant: t.List[str] = []
    for label, mod, attr, target_lib in candidates:
        try:
            __import__(target_lib)
        except ImportError:
            dormant.append(label)
            continue
        try:
            module = __import__(mod, fromlist=[attr])
            getattr(module, attr)
            usable.append(label)
        except (ImportError, AttributeError):
            dormant.append(label)
    return sdk_ok, usable, dormant


def _host_metrics_readiness() -> t.Tuple[bool, str]:
    try:
        import psutil  # noqa: F401
        return True, ""
    except ImportError:
        return False, "psutil is missing - reinstall polartrace"


def _cmd_status(_args: argparse.Namespace) -> int:
    cfg = _resolved_config()
    print("PolarTrace effective configuration")
    print(f"  config_file : {cfg['config_file'] or '(none discovered)'}")
    print(f"  app_name    : {cfg['app_name'] or '(not set)'}")
    print(f"  license_key : {_redact(cfg['license_key'])}")
    print(f"  collector   : {collector_base_url()}  (fixed in code)")
    print(f"  console_log : {'on' if cfg['console_log'] else 'off'}")
    print()

    sdk_ok, usable, dormant = _tracing_readiness()
    print("Tracing")
    if not sdk_ok:
        print("  status      : BROKEN - OpenTelemetry SDK missing")
        print("  fix         : pip install --force-reinstall polartrace")
    else:
        print(f"  status      : ENABLED ({len(usable)} integration(s) active)")
        print(f"  active      : {', '.join(usable) if usable else '(none - no supported client libs imported yet)'}")
        if dormant:
            print(f"  dormant     : {', '.join(dormant)}")
            print(f"                (these light up automatically once you `import` their library)")
    print()

    hm_ok, hm_msg = _host_metrics_readiness()
    print("Host metrics (CPU / memory / load)")
    if hm_ok:
        print("  status      : ENABLED - samples sent every 10s to /api/host-metrics")
    else:
        print(f"  status      : DISABLED - {hm_msg}")
    print()

    print("Precedence: CLI flags > env vars > config file > defaults.")
    return 0


# --------------------------------------------------------------------------- #
# trace-test
# --------------------------------------------------------------------------- #
def _cmd_trace_test(args: argparse.Namespace) -> int:
    """End-to-end smoke test of the trace pipeline.

    Spins up the tracer in-process, creates a synthetic span, optionally exercises
    a few common client libraries (requests / sqlalchemy / pymongo / redis) if they
    happen to be importable, then reports what got captured.

    Use this when ``polartrace-admin status`` says tracing is ENABLED but you still
    don't see anything in the PolarTrace console - this confirms the agent's local
    capture path works and isolates the problem to the collector / UI side.
    """
    path = _discover_config_path()
    if path and not args.no_file:
        apply_file_to_environ(path)

    api_key = os.environ.get("POLARTRACE_LICENSE_KEY", "trace-test-key-1234")
    app_name = os.environ.get("POLARTRACE_APP_NAME", "trace-test-app")
    os.environ["POLARTRACE_LICENSE_KEY"] = api_key
    os.environ["POLARTRACE_APP_NAME"] = app_name

    from polartrace.agent import PolarTrace, _get_global_agent

    agent = _get_global_agent() or PolarTrace(
        api_key=api_key,
        service_name=app_name,
        enable_console_log=False,
    )

    if not getattr(agent, "_trace_monitor", None):
        print("trace-test: tracer not initialised - try reinstalling the agent.")
        print("  Fix:  pip install --force-reinstall polartrace")
        return 1

    # 1. Synthetic span ------------------------------------------------------ #
    from opentelemetry import trace  # type: ignore
    tracer = trace.get_tracer("polartrace.trace_test")
    with tracer.start_as_current_span("polartrace.trace-test") as span:
        span.set_attribute("polartrace.test", True)

    # 2. Light exercise of common libs (best-effort, all wrapped in try/except) -- #
    exercised: t.List[str] = []
    try:
        from sqlalchemy import create_engine, text  # type: ignore
        eng = create_engine("sqlite:///:memory:")
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        exercised.append("sqlalchemy")
    except Exception:
        pass

    import time as _t
    _t.sleep(0.2)

    # 3. Inspect what landed -------------------------------------------------- #
    with agent._trace_lock:  # noqa: SLF001
        captured = list(agent._trace_queue)

    print(f"trace-test: captured {len(captured)} span(s)")
    if not captured:
        print("  No spans captured. Common causes:")
        print("    - Agent is broken or partially installed  (run: polartrace-admin status)")
        print("    - You did not start the app via 'polartrace-admin run-program'")
        return 1

    for s in captured:
        print(f"  kind={s['kind']:8s}  name={s['name']:35s}  traceId={s['traceId'][:10]}…")
    if exercised:
        print(f"  Exercised libraries: {', '.join(exercised)}")

    # 4. Attempt a flush so we know the collector is reachable ---------------- #
    print()
    print("Flushing to collector …")
    agent._flush_traces()  # noqa: SLF001
    with agent._trace_lock:  # noqa: SLF001
        leftover = len(agent._trace_queue)
    if leftover == 0:
        print(f"  OK - collector accepted the spans ({collector_base_url()}/traces).")
        return 0
    print(f"  FAILED - {leftover} span(s) still queued (collector rejected or unreachable).")
    print(f"  Check {os.path.join(os.getcwd(), 'polartrace_agent.log')} for the HTTP error.")
    return 1


# --------------------------------------------------------------------------- #
# test
# --------------------------------------------------------------------------- #
def _cmd_test(args: argparse.Namespace) -> int:
    # Apply config file to env first so CLI flags can still override
    path = _discover_config_path()
    if path and not args.no_file:
        apply_file_to_environ(path)

    api_key = args.key or os.environ.get("POLARTRACE_LICENSE_KEY") or ""
    app_name = args.name or os.environ.get("POLARTRACE_APP_NAME") or ""

    if not api_key or not app_name:
        _eprint(
            "polartrace-admin test: need POLARTRACE_LICENSE_KEY and POLARTRACE_APP_NAME.\n"
            "  Pass --key/--name, set env vars, or run 'polartrace-admin init' to scaffold a config."
        )
        return 2

    result = probe(api_key=api_key, app_name=app_name, timeout=args.timeout)
    print(result.format_human())
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- #
# run-program
# --------------------------------------------------------------------------- #
def _cmd_run_program(args: argparse.Namespace) -> int:
    if not args.cmd:
        _eprint(
            "polartrace-admin run-program: pass the command you'd normally use to start the app,\n"
            "  e.g.  polartrace-admin run-program python app.py\n"
            "        polartrace-admin run-program python -m uvicorn myapp:app"
        )
        return 2

    path = _discover_config_path()
    if path:
        apply_file_to_environ(path)
    elif not (os.environ.get("POLARTRACE_APP_NAME") and os.environ.get("POLARTRACE_LICENSE_KEY")):
        _eprint(
            "polartrace-admin run-program: no config file and no POLARTRACE_* env vars.\n"
            "  Set POLARTRACE_CONFIG_FILE, place polartrace.config.json in the cwd, or export\n"
            "  POLARTRACE_APP_NAME and POLARTRACE_LICENSE_KEY before running."
        )
        return 1

    exe, rest = args.cmd[0], list(args.cmd[1:])
    if not is_python_interpreter_invocation(exe):
        _eprint(
            f"polartrace-admin run-program: first argument must be a Python executable; got: {shlex.quote(exe)}\n"
            f"  Example:  polartrace-admin run-program {sys.executable} app.py"
        )
        return 1

    first = _resolve_python(exe)
    child = build_polartrace_argv(first, rest)
    if os.path.basename(child[0]).lower() in ("polartrace", "polartrace-admin"):
        _eprint("polartrace-admin: do not wrap polartrace or polartrace-admin again.")
        return 1

    try:
        os.execve(child[0], child, os.environ.copy())  # noqa: S606
    except OSError as e:
        _eprint(f"polartrace-admin: could not start {child!r}: {e}")
        return 1
    return 0  # unreachable on success


# --------------------------------------------------------------------------- #
# Argparse wiring
# --------------------------------------------------------------------------- #
_EPILOG = """\
On-host 4-step flow:
  1. pip install polartrace
  2. polartrace-admin init --name my-service
  3. Edit polartrace.config.json (paste your license key from the console)
  4. polartrace-admin test
     POLARTRACE_CONFIG_FILE=polartrace.config.json polartrace-admin run-program python app.py
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polartrace-admin",
        description="PolarTrace agent - configuration and launcher CLI.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-v", "--version", action="version",
        version="polartrace-admin (PolarTrace Python agent)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("init", help="Scaffold polartrace.config.json in the current directory")
    pi.add_argument("--name", help="POLARTRACE_APP_NAME for the project")
    pi.add_argument("--key", help="POLARTRACE_LICENSE_KEY (better: edit the file later)")
    pi.add_argument("--path", help="Output path (default: ./polartrace.config.json)")
    pi.add_argument("--force", action="store_true", help="Overwrite an existing file")
    pi.set_defaults(func=_cmd_init)

    pt = sub.add_parser("test", help="POST /service/validate to verify credentials + connectivity")
    pt.add_argument("--name", help="Override POLARTRACE_APP_NAME for this test only")
    pt.add_argument("--key", help="Override POLARTRACE_LICENSE_KEY for this test only")
    pt.add_argument("--timeout", type=float, default=8.0, help="Probe timeout in seconds (default 8)")
    pt.add_argument("--no-file", action="store_true",
                    help="Ignore polartrace.config.json; use env / flags only")
    pt.set_defaults(func=_cmd_test)

    ps = sub.add_parser("status", help="Print the resolved configuration (license redacted)")
    ps.set_defaults(func=_cmd_status)

    ptr = sub.add_parser(
        "trace-test",
        help="Fire a synthetic span and confirm the trace pipeline reaches the collector",
    )
    ptr.add_argument("--no-file", action="store_true",
                     help="Ignore polartrace.config.json; use env / flags only")
    ptr.set_defaults(func=_cmd_trace_test)

    pr = sub.add_parser(
        "run-program",
        help="The single command to start an instrumented Python app",
        description=(
            "Load config (from POLARTRACE_CONFIG_FILE / polartrace.config.json / env vars), "
            "then exec the command you pass - the same command you'd normally use to start your app. "
            "The first token must be your Python executable (python, python3, or a venv path)."
        ),
    )
    pr.add_argument(
        "cmd", nargs=argparse.REMAINDER,
        help="e.g. python app.py  |  python -m uvicorn myapp:app  |  python manage.py runserver"
    )
    pr.set_defaults(func=_cmd_run_program)

    return p


def main() -> t.Optional[int]:
    parser = _build_parser()
    args = parser.parse_args()
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    return func(args)


if __name__ == "__main__":
    r = main()
    if r is not None:
        raise SystemExit(r)
