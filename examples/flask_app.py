"""Minimal Flask app monitored by PolarTrace.

Run (zero code changes, agent attaches automatically):

    POLARTRACE_APP_NAME=example-flask \
    POLARTRACE_LICENSE_KEY=<your-api-key> \
    polartrace-admin run-program python flask_app.py
"""

from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/")
def index():
    return jsonify(hello="world")


@app.get("/error")
def error():
    return jsonify(error="synthetic failure"), 500


if __name__ == "__main__":
    app.run(port=8000)
