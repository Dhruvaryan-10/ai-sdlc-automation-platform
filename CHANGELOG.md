# Changelog

Notable changes to this project. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project does not
yet publish tagged releases, so entries are grouped by SDLC build **phase**
rather than by semantic version.

## [Unreleased]

### Phase 14 — CI & Deployment Readiness

- **GitHub Actions CI** (`.github/workflows/ci.yml`): on every push to `main`
  and every pull request, on Python **3.11** and **3.12** — installs
  `requirements.txt` (+ `pyflakes`), runs `python -m compileall app`,
  `python -m pyflakes app`, and `pytest -q -ra`. Fully offline: stub LLM agents,
  no secrets, no Gemini calls. A dummy `GOOGLE_API_KEY` is provided only so
  `import app...` passes the Phase 13B startup validation.
- **Docker** (`Dockerfile`, `.dockerignore`): image on
  `python:3.12-slim-bookworm`, `WORKDIR /app`, installs the existing
  `requirements.txt`, runs as a non-root user (`appuser`), serves
  `streamlit run app/ui/streamlit_app.py` on `0.0.0.0:8501`, `EXPOSE 8501`, and
  a `HEALTHCHECK` against Streamlit's `/_stcore/health`. `.dockerignore` keeps
  `.git`, `.claude`, `.env`, `venv/`, caches, and the `outputs/` `uploads/`
  `logs/` runtime directories out of the build context.
- **Deployment docs**: `README.md` gains a **Deployment & CI** section (local
  run, Docker build/run, port 8501, environment variables, `GOOGLE_API_KEY`
  handling, persistent directories, JSON persistence + in-process-only lock, CI
  behaviour, current limitations) and a **License** note.
- **Hermetic test**: `tests/test_streamlit_render.py::`
  `test_persisted_test_case_artifact_is_not_modified_by_preview` now builds its
  own `VersionService` fixture in a `tmp_path` directory instead of depending on
  a machine-local `outputs/` project. The environment-conditional `pytest.skip`
  is removed; the test always runs and keeps all of its original assertions.
- **`CHANGELOG.md`** added.
- **No application behaviour change.** One redundant `f""` prefix (a string
  literal with no placeholders) in `app/agents/closure_report/service.py` was
  changed to a plain string literal so `pyflakes app` is clean; the produced
  text is byte-identical.

**Known open items (carried, not addressed in Phase 14):**
- The repository has **no license file**. None is established anywhere in the
  repo history or metadata; choosing one is a project-owner decision (see
  `README.md` → License).
- `GEMINI_MODEL` discrepancy is unchanged: code / `.env.example` default
  `gemini-3.5-flash` vs the local `.env` `gemini-3.6-flash`. Deliberately out of
  scope — awaits a product decision.

## Earlier phases

### Phase 13B — Configuration Hardening & Startup Reliability

- `app/utils/config.py`: every setting is validated once at `Settings()` time —
  `GOOGLE_API_KEY` non-empty / not the `.env.example` placeholder / stored
  stripped; `GEMINI_TEMPERATURE` in `[0.0, 2.0]`; `GEMINI_TIMEOUT_SECONDS >= 1`;
  `GEMINI_MAX_RETRIES >= 0`; `LOG_LEVEL` in the standard vocabulary, stored
  upper-case. `resolved_*_dir()` now return absolute paths. New
  `summary_for_log()` exposes a secret-free view of the effective config.
- `app/ui/streamlit_app.py`: a startup guard turns a configuration
  `ValidationError` into a clean `st.error` + `st.stop` (no traceback, key value
  never shown) and logs exactly one sanitized `config: …` line.
- `.streamlit/config.toml` (tracked): `showErrorDetails=false`,
  `headless=true`, `maxUploadSize=25`.

### Phase 13A — Core Generation Observability

- `app/utils/run_context.py`: per-run `run_id` / `project_id` via
  `contextvars` (copied into LangGraph's sync-node executor).
- `app/utils/metrics.py`: `instrumented_invoke()` emits one `event=llm_call`
  telemetry line per provider call — stage, latency, attempt, token usage when
  available, outcome. Never raises; never logs prompts, model output, or
  secrets.
- `app/utils/logger.py`: every log record carries `run_id=`.
- `app/orchestration/graph.py`: `run_step()` logs run start / complete /
  failure with elapsed time and the failing stage. Retry behaviour and graph
  topology unchanged.

### Phase 12B — Error Taxonomy & User-Facing Error Handling

- `app/utils/errors.py`: a pure, deterministic `classify(exc) -> AppError`
  taxonomy (13 categories) plus `log_app_error()`. The UI's `friendly_error()`
  now delegates to it. No provider text, `pydantic.ValidationError` values,
  tracebacks, or secrets reach the UI or the logs.

### Phase 12A — Durable, Atomic Version Persistence

- `app/services/version_service.py`: `versions.json` is written atomically
  (unique temp file → `flush` + `fsync` → `os.replace` → directory fsync); a
  `versions.json.bak` last-known-good copy is refreshed before each write; a
  corrupt primary is transparently recovered from the backup (logged CRITICAL;
  `VersionPersistenceError` if unrecoverable); the read-modify-write cycle is
  serialized by a per-path re-entrant in-process lock; timestamps are
  timezone-aware UTC. The JSON record shape and every public method signature
  are unchanged.

### Phases 1–11 (summary)

Business Analyst (SOW → BRD), Solution Architect (BRD → HLD), Initial User
Story, Low-Level Design, User Story Refinement, QA / Test Case, and Closure
Report agents. Shared lifecycle per artifact: generate → manual edit / AI
refine → append-only version history → choose final (locks) → unlock → Word
export. Deterministic read-only Traceability and Project Quality reports. One
sequential LangGraph (with a concurrent HLD ∥ Initial-User-Story fan-out)
orchestrating the seven agents; it never finalizes. LLM performance work:
HLD/user-story parallelization, deterministic LLD generation-context reduction,
explicit Gemini timeout / retry policy.
