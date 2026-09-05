"""
Phase 9A — SDLC Quality, Traceability & Governance Hardening: BACKEND ONLY.

`app.quality.traceability` is a read-only, deterministic reporting layer built
entirely on top of already-persisted artifacts (via the existing services /
`VersionService`). It does not modify, replace, or change the behaviour of any
existing service, agent, prompt, schema, or the orchestration graph/state/status
- it only *reads* what they already produced and computes a report from it.

No Gemini calls, no new persistence stream, no database, no mutation of any
artifact. See `app/quality/traceability.py` for the public functions.
"""
