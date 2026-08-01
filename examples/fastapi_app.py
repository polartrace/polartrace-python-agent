"""Minimal FastAPI app monitored by PolarTrace.

Run (zero code changes, agent attaches automatically):

    POLARTRACE_APP_NAME=example-fastapi \
    POLARTRACE_LICENSE_KEY=<your-api-key> \
    polartrace-admin run-program uvicorn fastapi_app:app --port 8000
"""

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def index():
    return {"hello": "world"}


@app.get("/error")
def error():
    return {"error": "synthetic failure"}
