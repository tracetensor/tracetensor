# Changelog

## 0.1.0 — 2026-08-10

Initial open-source release.

- CLI: `tracetensor run`, `serve`, `tasks`, `dataset`, `vault`
- Agents: oracle, claude-code, codex, mini-swe, anthropic, openai, openrouter
- Docker-based sandboxed execution with isolated containers per trial
- Verifier grading with reward.json scoring
- Cost tracking (vendor-reported)
- Example tasks: test-suite (10), pr-suite (5), agentic-suite (5), mediaos-deep-v2 (3), and standalone tasks
- Horizontal scaling via Postgres-backed job queue
- API with token auth and rate limiting
