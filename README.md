# PolarTrace Python agent

Production-grade observability agent for Python. Framework-agnostic - works with **Flask**, **FastAPI**, **Django**, **Celery**, and any other Python framework or plain script via a single auto-instrumentation entry point. No per-framework code lives in your application files.

The integration follows four steps. Read once, then never touch it again.

---

## On-host: 4-step integration

```text
1. pip install polartrace                 ← install the agent
2. polartrace-admin init --name my-app    ← name the application
3. <edit polartrace.config.json>             ← paste your license key
4. polartrace-admin test                  ← verify connectivity
   polartrace-admin run-program python <cmd>      ← run with instrumentation
```

### Step 1 - Install

```bash
pip install polartrace
```

That's it - one command, no extras to remember. Request logs, distributed traces, and host metrics (CPU / memory / load) are all included. Every supported integration (Flask / FastAPI / Django, `requests` / `urllib3` / `httpx`, `pymongo` / SQLAlchemy / `psycopg2` / `redis`) lights up automatically as soon as you import the library.

Both `polartrace` and `polartrace-admin` are now on your `PATH`.

### Step 2 - Name the application

```bash
polartrace-admin init --name my-service
```

That writes `polartrace.config.json` in the current directory:

```json
{
  "polartrace": {
    "app_name": "my-service",
    "license_key": "your-license-key-minimum-10-characters",
    "console_log": false
  }
}
```

> The collector URL is fixed in the agent code and is not part of the config surface. Customers don't see it and can't override it from the file, env, or CLI.

Prefer environment variables? Skip the file and `export POLARTRACE_APP_NAME=my-service` instead.

### Step 3 - Configure with API key

1. Sign in to the PolarTrace console.
2. Create a service that matches `app_name` from step 2.
3. Copy the API key shown after creation.
4. Paste it into `license_key` in `polartrace.config.json` (or `export POLARTRACE_LICENSE_KEY=…`).

> Never commit the real key. Add `polartrace.config.json` to `.gitignore`.

### Step 4 - Run and verify

```bash
polartrace-admin test
```

Expected output:

```text
PolarTrace connection: OK
  app_name  : my-service
  http      : 200
```

### The single command to start an instrumented app

Once the config file exists and `polartrace-admin test` returns OK, this is **the only command you need** - it loads the config and runs whatever command you'd normally use to start the app:

```bash
POLARTRACE_CONFIG_FILE=polartrace.config.json polartrace-admin run-program <YOUR_COMMAND>
```

`<YOUR_COMMAND>` is exactly the command you use today, e.g.:

```bash
POLARTRACE_CONFIG_FILE=polartrace.config.json polartrace-admin run-program python app.py
POLARTRACE_CONFIG_FILE=polartrace.config.json polartrace-admin run-program python -m uvicorn myapp:app --host 0.0.0.0 --port 8000
POLARTRACE_CONFIG_FILE=polartrace.config.json polartrace-admin run-program python manage.py runserver
```

If `polartrace.config.json` is in the current working directory you can drop the env-var prefix - the file is auto-discovered:

```bash
polartrace-admin run-program python app.py
```

The agent attaches automatically - Flask/FastAPI via constructor hook, Django via the middleware line documented below.

---

## Docker integration

Same pattern, container-friendly: pass credentials as env vars, then exec the single `polartrace-admin run-program <YOUR_COMMAND>` line from `CMD`.

```dockerfile
FROM python:3.11-slim
WORKDIR /app

RUN pip install --no-cache-dir polartrace
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8000
# Required at run time:
#   POLARTRACE_APP_NAME, POLARTRACE_LICENSE_KEY
# Optional:
#   POLARTRACE_ENABLE_CONSOLE_LOG=1
# (Collector URL is built into the agent - no environment variable for it.)

EXPOSE 8000

HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["sh", "-c", "polartrace-admin test || true; exec polartrace-admin run-program python -m uvicorn myapp:app --host 0.0.0.0 --port ${PORT}"]
```

Build and run:

```bash
docker build -t myapp .
docker run -p 8000:8000 \
  -e POLARTRACE_APP_NAME=my-service-docker \
  -e POLARTRACE_LICENSE_KEY=your-key \
  myapp
```

`docker-compose.yml`:

```yaml
services:
  app:
    build: .
    ports: ["8000:8000"]
    environment:
      POLARTRACE_APP_NAME: my-service-docker
      POLARTRACE_LICENSE_KEY: "${POLARTRACE_LICENSE_KEY}"
```

The `polartrace-admin test` step in CMD prints a connection summary in the container logs - easy to confirm during `docker compose up`.

---

## CLI reference

| Command                                       | Role                                                                                                              |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `polartrace-admin init`                       | Scaffold `polartrace.config.json` in the current directory.                                                          |
| `polartrace-admin status`                     | Print resolved config (file + env). License key is redacted.                                                      |
| `polartrace-admin test`                       | POST `/service/validate` to confirm credentials and reachability. Exit code 0 on success.                         |
| `polartrace-admin run-program <python> <args…>`       | Apply config, then start `python -m polartrace <args>` so Flask/FastAPI auto-attach.                              |
| `python -m polartrace <script.py> [args]`     | Equivalent to `polartrace-admin run-program` for scripts (env vars must already be set).                                  |
| `python -m polartrace -m <module> [args]`     | Module form, e.g. `-m uvicorn myapp:app`.                                                                          |

Precedence: **CLI flags > env vars > config file > defaults.** A value already exported in the shell wins over the same key in `polartrace.config.json` - use that to override per-environment.

---

## Configuration reference

### JSON file (`polartrace.config.json`)

The root may be the keys directly, or nested under a `polartrace` block - both are accepted.

| Key                       | Env var                          | Notes                                                       |
| ------------------------- | -------------------------------- | ----------------------------------------------------------- |
| `app_name` / `service`    | `POLARTRACE_APP_NAME`            | Required. `app_name` wins if both are present.              |
| `license_key`             | `POLARTRACE_LICENSE_KEY`         | Required. Minimum 10 characters.                            |
| `console_log`             | `POLARTRACE_ENABLE_CONSOLE_LOG`  | `true` / `1` enables stdout banner + extra logging.         |

> The collector URL is the `PolarTrace.BASE_URL` constant in `polartrace/agent.py`. It is intentionally not configurable from JSON, environment variables, or the CLI - change it in code if you need to point at a different collector.

### Discovery rules

`polartrace-admin` looks for the config file in this order:

1. `$POLARTRACE_CONFIG_FILE` (absolute or relative path)
2. `./polartrace.config.json`
3. `./polartrace.json`

If none are found and the required env vars are set, that's still fine - env-only is a supported flow.

---

## Architecture (for reviewers)

| Layer                    | Module                          | Responsibility                                                              |
| ------------------------ | ------------------------------- | --------------------------------------------------------------------------- |
| **Config**               | `polartrace.config_loader`      | Read JSON, merge nested blocks, write `POLARTRACE_*` env vars.              |
| **Connection probe**     | `polartrace.connection`         | One-shot `service/validate` POST, structured result for `admin test`.       |
| **Agent (core)**         | `polartrace.agent`              | Buffer + flush loop for logs and traces, body/header sanitisation.          |
| **Bootstrap**            | `polartrace.bootstrap`          | Monkey-patches Flask & FastAPI `__init__` so middleware attaches everywhere they are constructed. |
| **Launcher**             | `polartrace.launcher`           | `python -m polartrace` - installs bootstrap, then `runpy`s your script / module. |
| **CLI**                  | `polartrace.admin_cli`          | `init`, `status`, `test`, `run` subcommands.                                |
| **Framework middleware** | `polartrace.middleware.*`       | Per-framework request capture (Flask hook, Starlette middleware, Django middleware). |

**Framework-agnostic** because the *core* (config + agent + connection) knows nothing about web frameworks. Framework support is layered on:

- **Flask / FastAPI** - auto-attached by `bootstrap.py` patching their constructors. Zero code changes in your app.
- **Django** - add one line to `MIDDLEWARE` (Django constructs middleware once at boot, so we don't need a constructor patch). See below.
- **Any other framework / plain script** - `auto_init_from_env()` runs from `python -m polartrace`, so logs and traces start flowing as soon as your code does. Wrap your own request lifecycle by calling `PolarTrace(...)._log_request(RequestLog(...))` if you want explicit instrumentation.

### Django settings line

```python
# settings.py
MIDDLEWARE = [
    "polartrace.middleware.django_mw.DjangoPolarTraceMiddleware",
    # … your other middleware
]
```

### Graceful degradation

If the agent can't connect, the *host application must never crash*. We enforce this with:

1. The connection probe is best-effort; failures are logged to `polartrace_agent.log` and re-tried on each flush interval.
2. Optional imports (`requests`, OpenTelemetry, framework middleware) all fail closed - missing dependencies disable the feature, never the app.
3. Per-request capture wraps payload decoding in `try/except`, so a malformed body doesn't break the response path.

---

## Manual integration (advanced)

If you can't use `polartrace-admin run-program`, attach middleware directly:

```python
# Flask
from polartrace import PolarTrace
agent = PolarTrace(api_key=..., service_name=...)
agent.middleware_flask()(app)
```

```python
# FastAPI
from polartrace import PolarTrace
from polartrace.middleware.fastapi_mw import FastAPIPolarTraceMiddleware
agent = PolarTrace(api_key=..., service_name=...)
app.add_middleware(FastAPIPolarTraceMiddleware, agent=agent)
```

For Django, the middleware line is required either way - see above.

---

## Examples

See the [`examples/`](examples/) directory for minimal, self-contained Flask,
FastAPI and Django apps wired up with the agent.

---

## Agent log file

The agent writes its own diagnostics to `polartrace_agent.log` in your
process's working directory (append-only). Add it to your `.gitignore`.

---

## Contributing & Security

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue privately,
see [SECURITY.md](SECURITY.md).

---

## License

MIT - see the [LICENSE](LICENSE) file.
