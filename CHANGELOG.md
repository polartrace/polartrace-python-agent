# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-02

### Added
- Initial public release of the PolarTrace Python agent
- Zero-code integration via `polartrace-admin run-program` / `python -m polartrace`
- Flask, FastAPI and Django middleware (request logs with sanitized bodies)
- OpenTelemetry-based distributed tracing with automatic instrumentation for
  requests/urllib3/httpx, pymongo, SQLAlchemy, psycopg2 and Redis
- Host metrics sampling (CPU, memory, load, uptime; container cgroup limits)
- `polartrace-admin` CLI: config, connection test, trace self-test
- Sensitive-field redaction for request bodies and span attributes
- Batched shipping with retain-and-retry backpressure handling
