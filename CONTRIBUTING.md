# Contributing

Thanks for your interest in improving the PolarTrace Python agent!

## Development setup

Fork and clone the repository, then install in editable mode:

```bash
git clone git@github.com:your-username/polartrace-python-agent.git
cd polartrace-python-agent
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Working on the agent

- The package lives in `polartrace/`; middleware per framework in `polartrace/middleware/`.
- Try changes against the apps in `examples/` — point the agent at your own
  collector while developing.
- `polartrace-admin test` checks connectivity; `polartrace-admin trace-test`
  sends a synthetic trace end to end.

## Before submitting a pull request

1. Make sure `python -m compileall polartrace` passes
2. Verify `polartrace --help` and `polartrace-admin --help` still work
3. Describe what changed and why in the PR body; update `CHANGELOG.md` under `[Unreleased]`

## Reporting bugs

Open a GitHub issue with the agent version, Python version, framework and
version (Flask/FastAPI/Django), and a minimal reproduction. For security
issues see [SECURITY.md](SECURITY.md) — do not open a public issue.
