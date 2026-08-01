"""``python -m polartrace`` - see :mod:`polartrace.launcher`."""

from polartrace.launcher import run_polartrace_module_main


def main() -> None:
    """Entry point for the ``polartrace`` console script (see pyproject.toml)."""
    run_polartrace_module_main()


if __name__ == "__main__":
    main()
