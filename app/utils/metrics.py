"""
Phase 13A — generation telemetry (stdlib only, additive, never behaviour-changing).

`instrumented_invoke(llm, prompt, *, stage, attempt)` is a transparent wrapper
around `llm.invoke(prompt)`: it returns exactly what `invoke` returns and
re-raises exactly what `invoke` raises, and — as a side effect — emits ONE
machine-readable telemetry line per provider invocation:

    event=llm_call run_id=<uuid> generation_id=<uuid> project_id=<id|-> stage=<stage>
    model=<name> prompt_tokens=<n|-> completion_tokens=<n|-> total_tokens=<n|->
    latency_ms=<n> attempt=<n> outcome=<success|error> [error_type=<ClassName>]

Hard rules:
  * Missing / partial / malformed `usage_metadata` never fails the generation —
    the token fields become ``"-"``.
  * No second Gemini call is made to obtain token counts.
  * Prompts, raw model output, artifact content, API keys, provider headers, and
    `pydantic.ValidationError` values are never logged — only the bounded fields
    above (the error case logs the exception *type name* only).
  * Any failure inside telemetry itself is swallowed; the SDLC generation
    continues normally.

Retry behaviour is untouched: `attempt` is passed in by the caller's existing
loop; this module only observes it.
"""

from __future__ import annotations

import time
from typing import Any

from app.utils.logger import get_logger
from app.utils.run_context import current_project_id, current_run_id, new_generation_id

try:  # keep telemetry import-safe even if config somehow fails to load
    from app.utils.config import settings as _settings
except Exception:  # pragma: no cover - config load failure is handled elsewhere
    _settings = None

logger = get_logger("app.telemetry")

_MISSING = "-"


def _elapsed_ms(t0: float) -> int:
    try:
        return max(0, round((time.perf_counter() - t0) * 1000))
    except Exception:  # pragma: no cover
        return 0


def _model_name() -> str:
    try:
        name = getattr(_settings, "gemini_model", None)
        return name if isinstance(name, str) and name else _MISSING
    except Exception:  # pragma: no cover
        return _MISSING


def _coerce_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(response: Any) -> tuple[int | None, int | None, int | None]:
    """Return ``(prompt_tokens, completion_tokens, total_tokens)`` from an LLM
    response; any element may be ``None``. NEVER raises.

    Reads LangChain's ``AIMessage.usage_metadata`` first (the standard shape),
    then falls back to ``response_metadata['usage_metadata']``. Tolerates the
    several key spellings Gemini/LangChain have used. The structured-output path
    returns a parsed Pydantic model with no usage attached — that legitimately
    yields ``(None, None, None)``.
    """
    try:
        meta = getattr(response, "usage_metadata", None)
        if not isinstance(meta, dict):
            rmeta = getattr(response, "response_metadata", None)
            if isinstance(rmeta, dict):
                inner = rmeta.get("usage_metadata")
                meta = inner if isinstance(inner, dict) else None
        if not isinstance(meta, dict):
            return (None, None, None)

        prompt = _coerce_int(
            meta.get("input_tokens")
            or meta.get("prompt_tokens")
            or meta.get("prompt_token_count")
        )
        completion = _coerce_int(
            meta.get("output_tokens")
            or meta.get("completion_tokens")
            or meta.get("candidates_token_count")
        )
        total = _coerce_int(
            meta.get("total_tokens") or meta.get("total_token_count")
        )
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        return (prompt, completion, total)
    except Exception:  # pragma: no cover - defensive; telemetry must not raise
        return (None, None, None)


def _fmt(value: int | None) -> str:
    return str(value) if isinstance(value, int) else _MISSING


def log_llm_call(
    *,
    stage: str,
    generation_id: str,
    latency_ms: int,
    outcome: str,
    attempt: int = 1,
    response: Any = None,
    error: BaseException | None = None,
) -> None:
    """Emit exactly one ``event=llm_call`` telemetry record. NEVER raises."""
    try:
        prompt_t = completion_t = total_t = None
        if response is not None:
            prompt_t, completion_t, total_t = extract_usage(response)

        fields = [
            "event=llm_call",
            f"run_id={current_run_id()}",
            f"generation_id={generation_id}",
            f"project_id={current_project_id()}",
            f"stage={stage or _MISSING}",
            f"model={_model_name()}",
            f"prompt_tokens={_fmt(prompt_t)}",
            f"completion_tokens={_fmt(completion_t)}",
            f"total_tokens={_fmt(total_t)}",
            f"latency_ms={latency_ms if isinstance(latency_ms, int) else _MISSING}",
            f"attempt={attempt if isinstance(attempt, int) and attempt >= 1 else 1}",
            f"outcome={outcome}",
        ]
        if outcome == "error" and error is not None:
            fields.append(f"error_type={type(error).__name__}")
        logger.info(" ".join(fields))
    except Exception:  # pragma: no cover - telemetry must never break the app
        pass


def _safe_log(**kwargs: Any) -> None:
    """`log_llm_call` already swallows its own errors; this second guard makes
    telemetry inert even if `log_llm_call` is replaced/broken (spec: observability
    must never break the app)."""
    try:
        log_llm_call(**kwargs)
    except Exception:  # pragma: no cover - defensive
        pass


def instrumented_invoke(llm: Any, prompt: Any, *, stage: str, attempt: int = 1) -> Any:
    """Call ``llm.invoke(prompt)`` and emit one telemetry record for it.

    Transparent: returns exactly ``llm.invoke(prompt)``'s value and re-raises its
    exception unchanged (the caller's own ``try/except`` still wraps it as the
    existing ``<Agent>Error(...) from exc``). Telemetry-side failures are
    swallowed. Latency covers only the provider call.
    """
    try:
        generation_id = new_generation_id()
    except Exception:  # pragma: no cover - defensive
        generation_id = "-"
    t0 = time.perf_counter()
    try:
        response = llm.invoke(prompt)
    except BaseException as exc:
        _safe_log(
            stage=stage, generation_id=generation_id, latency_ms=_elapsed_ms(t0),
            outcome="error", attempt=attempt, error=exc,
        )
        raise
    _safe_log(
        stage=stage, generation_id=generation_id, latency_ms=_elapsed_ms(t0),
        outcome="success", attempt=attempt, response=response,
    )
    return response
