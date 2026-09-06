"""
Factory for the shared Gemini chat client.

WHY THIS FILE EXISTS (Phase 11A):
Every agent used to build its own `ChatGoogleGenerativeAI` inline, eagerly, in
`__init__` — even when the surrounding service was only being constructed for a
read-only operation (`sdlc_status()`, the quality/traceability reports, staleness
checks, the Streamlit bootstrap). Each construction costs ~1.3 s (it builds two
SSL contexts inside `google-genai`'s client) and makes no network call, so that
time was pure waste on every read-only path.

This module does two things and nothing else:
  1. Centralizes `ChatGoogleGenerativeAI` construction so the timeout / retry
     policy is configured in exactly ONE place (from `settings`) instead of
     being copy-pasted per agent.
  2. Lets each agent build its client LAZILY, on the first real
     generate/refine call (see each agent's `_ensure_llm()` / the deferred
     `structured_llm`).

It is a plain factory function — NOT a base agent class. Each agent still owns
its own prompt handling, `_extract_text`, `_invoke`, and structured-retry
logic; the deliberate per-agent duplication described in CLAUDE.md is unchanged.
"""

from langchain_google_genai import ChatGoogleGenerativeAI

from app.utils.config import settings


def build_chat_llm(*, max_retries: int | None = None) -> ChatGoogleGenerativeAI:
    """Construct the configured Gemini chat client.

    Reads `settings` at CALL time (never import time), so a test that
    monkeypatches `settings.gemini_*` before the first real call still wins.

    * `timeout` / `max_retries` are passed EXPLICITLY (previously left to the
      SDK defaults: `timeout=None` — no client deadline at all — and
      `max_retries=6`). `settings.gemini_timeout_seconds` bounds a hung
      connection; `settings.gemini_max_retries` is the SDK-level HTTP retry
      budget.
    * `max_retries` may be overridden per call. The structured agents
      (`test_case`, `closure_report`) keep their own bounded application-level
      retry loop with jittered backoff and richer transient-error
      classification; the small SDK retry budget here sits under that without
      multiplying it out of control (see `settings.gemini_max_retries`).
    * Model name and temperature are unchanged — still
      `settings.gemini_model` / `settings.gemini_temperature`.
    """
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=settings.gemini_temperature,
        google_api_key=settings.google_api_key,
        timeout=settings.gemini_timeout_seconds,
        max_retries=(
            settings.gemini_max_retries if max_retries is None else max_retries
        ),
    )
