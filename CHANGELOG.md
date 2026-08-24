# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Bounded the log and trace queues (drop-oldest, 5000 items each) to match the existing host-metrics cap, so a long collector outage can no longer grow memory unbounded
- Flushes now ship at most 500 items per tick instead of the entire queue
- Retained failures (429/5xx/network) back off exponentially (10s doubling up to 5 min) and honor the collector's `Retry-After` header on 429

## [1.0.0] - 2026-08-25

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
