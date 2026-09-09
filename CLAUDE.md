# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-powered SDLC platform, built one agent at a time. Streamlit UI, Google Gemini (via
LangChain), plain JSON persistence (no database — deferred to a later Project Memory phase).

- **Phase 1 — Business Analyst Agent** (`app/agents/business_analyst/`): SOW → BRD.
- **Phase 2 — Solution Architect Agent** (`app/agents/solution_architect/`): accepted/final
  BRD → HLD.
- **Phase 3 — Initial User Story Agent** (`app/agents/initial_user_story/`): accepted/final
  BRD → draft user stories. An **independent branch** from the BRD, parallel to Phase 2 —
  it must **not** import `solution_architect` and must **not** need the HLD or any LLD.
- **Phase 4 — Low-Level Design Agent** (`app/agents/low_level_design/`): accepted/final HLD →
  LLD. Accepted HLD is the **only** hard prerequisite; BRD is supporting context; draft user
  stories are *optional* context read via `VersionService(subdir="user_stories")` — the LLD
  package must **not** import the Initial User Story Agent package or construct its service.
  LLD → `solution_architect` is allowed (the HLD is the required source).
- **Phase 5 — User Story Refinement Agent** (`app/agents/user_story_refinement/`): reconciles
  the existing user stories against the accepted BRD (primary) + accepted HLD + accepted LLD
  (both optional context). Hard prerequisites: a final BRD **and** at least one existing
  user-story version. **Writes into the same `user_stories` stream** (no second history) via
  its own `VersionService(subdir="user_stories")`; reads BRD/HLD/LLD via
  `VersionService(subdir=…)`. It imports **no** other agent package (`initial_user_story`,
  `solution_architect`, `low_level_design`). A refinement version has `source="ai_refine"`
  and a **composite** `source_ref` `"brd_v{b};us_v{u};hld_v{h|none};lld_v{l|none}"`. Three-way
  staleness: `is_stale()` / `stale_sources()` compare that composite to the current final
  BRD/HLD/LLD — one live boolean, itemised reasons, never stored, never auto-refines. `refine()`
  also stamps the document body's own `**Source:** Artifact Refinement` + `**Built From:**`
  lines (initial generation keeps `**Source:** Accepted BRD`; the refine prompt is told not
  to touch those lines; historical versions are never rewritten).
- **Phase 6 — QA / Test Case Agent** (`app/agents/test_case/`): accepted/final BRD (the
  **only** hard prerequisite) + accepted HLD + accepted LLD + final/refined-or-latest User
  Stories (all **optional context**) → test cases in an **own** `test_cases` stream. The
  agent returns **JSON** (prompt contract); `TestCaseService` validates it, renders a
  Markdown document with stable `TC-NNN` sections (same storage convention as every other
  artifact), and appends via `VersionService(subdir="test_cases")`. Imports **no** other
  agent package (only `ProjectMetadata` + `PromptManager` from `business_analyst`). Actions:
  `generate()` (v1), `regenerate()` (fresh rebuild → new version), `refine_with_ai(feedback)`
  (feedback-driven, preserves unaffected `TC-NNN`), `save_manual_edit()`. Composite
  `source_ref` `"brd_v{b};hld_v{h|none};lld_v{l|none};us_v{u|none}"`. **Per-source** staleness
  (`stale_sources()`): BRD stale if its version changed; HLD/LLD/User Stories stale **only**
  if they were *used* (recorded as an int) and their authoritative version later changed — a
  previously-absent optional artifact appearing later is **not** stale. Never auto-regenerates.
- **Phase 7 — Closure Report Agent** (`app/agents/closure_report/`): the END-OF-SDLC
  synthesis. **Not** the Project Quality Report — it *consumes* it. **Only hard prerequisite:
  a final BRD** (`NoFinalBRDError`). Every other missing/incomplete artifact (no final
  HLD/LLD, no user stories, no test cases, test cases not finalized, uncovered
  requirements/stories, ungrounded/orphan references) is a **finding inside the report**, not
  a failure. **Deterministic code computes every fact** (artifact exists/latest/final/status,
  requirement & story & test counts, coverage, grounding & orphan findings, blockers,
  outstanding items) by reusing `app.quality.build_project_quality_report_for_project()` +
  `build_project_traceability_report()` **verbatim** — no coverage/version logic is
  re-implemented. **Gemini writes narrative prose ONLY** (`ClosureNarrative`: six `str`
  fields via `with_structured_output`, mirrors the QA agent's structured+retry pattern) and
  is forbidden (in the prompt) from computing anything or choosing the status. **Closure
  status is deterministic**, exactly one of `READY_FOR_CLOSURE` / `CLOSURE_WITH_OPEN_ITEMS`
  / `NOT_READY_FOR_CLOSURE` (no numeric score, no "production ready"): NOT_READY when a
  required final artifact is missing (final HLD, final LLD, user stories present, test cases
  present *and* finalized); OPEN_ITEMS when all required final evidence exists but objective
  non-blocking findings remain; READY otherwise. Stored **Markdown** (9 sections) in an
  **own** `closure_report` stream via `VersionService(subdir="closure_report")`. Actions:
  `generate()` (v1), `regenerate()` (fresh rebuild → new version — never overwrites).
  **No `refine_with_ai`.** Composite `source_ref`
  `"brd_v{b};hld_v{h|none};lld_v{l|none};us_v{u|none};tc_v{t|none}"` (final versions;
  User Stories = latest, no finalization stage). Imports **no** other agent package (only
  `ProjectMetadata` + `PromptManager` from `business_analyst`, plus `app.quality.*`).
  **Never auto-finalizes** — `choose_final` is a separate human action.

Every stage shares one lifecycle: generate → manual edit / AI refine → append-only version
history → choose final (locks) → unlock → Word export. BRD, HLD, user-story, LLD,
test-case, and closure-report versions are stored in separate streams; **Phase 3 and Phase 5
both write the one `user_stories` stream**, the QA agent only ever writes `test_cases`, and
the Closure Report agent only ever writes `closure_report`. Each new agent is built
**structurally parallel** to the existing ones (same wrapper/service shape) — there is
deliberately no shared base class and `_extract_text` / `_invoke` / `_derive_metadata_*` are
duplicated per agent. Do not consolidate until a dedicated cleanup.

## Commands

Requires Python 3.11+ (code uses `X | None` and `list[...]` builtins).

```bash
# one-time setup
python -m venv venv
venv\Scripts\Activate.ps1          # Windows PowerShell;  macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then put a real GOOGLE_API_KEY in .env

# run (must be from the repo root)
streamlit run app/ui/streamlit_app.py

# tests — deterministic, no API key or network needed (stub LLM agents)
pytest                              # whole suite
pytest tests/test_user_story_refinement_service.py::test_no_final_brd_blocks_refinement  # single test

# checks CI runs (Phase 14) — Python 3.11 and 3.12, fully offline
python -m compileall app
python -m pyflakes app             # pyflakes is dev-only, NOT in requirements.txt
pytest -q -ra

# Docker (Phase 14) — image serves Streamlit on 0.0.0.0:8501 as a non-root user
docker build -t sdlc-ba-agent .
docker run --rm -p 8501:8501 -e GOOGLE_API_KEY=your_real_key \
  -v "$(pwd)/outputs:/app/outputs" -v "$(pwd)/uploads:/app/uploads" -v "$(pwd)/logs:/app/logs" \
  sdlc-ba-agent
```

There is **no build step** configured. `pyflakes` is the only static check (run by CI;
not in `requirements.txt`, and `app/` must stay pyflakes-clean). The `pytest` suite (added
in Phase 2) uses stub agents injected via the `agent=` constructor param and monkeypatches
`settings.output_dir` to a tmp dir — it never calls Gemini. Real generation/refinement is
verified manually through the Streamlit UI. A missing or invalid `GOOGLE_API_KEY` raises a
pydantic `ValidationError` at startup (by design — see `app/utils/config.py`); `conftest.py`
sets a dummy key before importing `app`.

**CI / Docker (Phase 14):** `.github/workflows/ci.yml` runs the three checks above on push
to `main` and on every PR (Python 3.11 + 3.12); it is offline and makes no Gemini calls
(dummy `GOOGLE_API_KEY`). `Dockerfile` + `.dockerignore` build a `python:3.12-slim-bookworm`
image (`WORKDIR /app`, non-root `appuser`, `HEALTHCHECK` on `/_stcore/health`); the base
image digest is intentionally not hard-coded. `outputs/` `uploads/` `logs/` must be
volume-mounted to persist — and the `VersionService` write lock is **in-process only**, so
run a **single** server process per `outputs/` directory. See `README.md` §8. The repo has
**no license** yet (pending an owner decision — `README.md` §9). The `GEMINI_MODEL`
code/`.env.example` (`gemini-3.5-flash`) vs local `.env` (`gemini-3.6-flash`) discrepancy is
unresolved and out of scope.

## Architecture — strict layering

Work flows in one direction only; keep it that way.

- **`app/ui/streamlit_app.py`** renders widgets and maps exceptions to plain-English
  messages (`friendly_error`). It calls **only** the seven services and the docx helpers
  (`generate_brd_docx` / `_hld_docx` / `_user_stories_docx` / `_lld_docx` / `_test_cases_docx`
  / `_closure_report_docx`) — never parsers, the agents, or `VersionService` directly. Users
  must never see a traceback. **Eight tabs**: Step 1 Upload & Generate, Step 2 BRD, Step 3
  HLD, Step 4 User Story (Phase 3 generation + freeform AI refine), Step 5 LLD, **Step 6 User
  Story Refinement** (standalone — hosts the Phase 5 "Refine from Artifacts" action + three-way
  stale banner, writing the same `user_stories` stream), **Step 7 QA / Test Case Workspace**
  (Phase 6), **Step 8 Closure Report** (Phase 7 — generate / regenerate / view / choose final;
  the SDLC Pipeline panel also advances to it). `session_state` keys are prefixed per
  workspace — `hld_`, `us_`, `lld_`, `usr_`, `qa_`, `closure_` — so they never collide.
  `story_version_label(v)` labels a Phase 5 refinement version "Artifact Refinement"
  (`source=="ai_refine"` **and** composite `source_ref`).
- **`…/business_analyst/service.py::BusinessAnalystService`** — orchestration point for the
  BRD: `extract → clean → agent → version`, plus manual edit, AI refine, finalize/lock.
- **`…/solution_architect/service.py::SolutionArchitectService`**,
  **`…/initial_user_story/service.py::InitialUserStoryService`**, and
  **`…/low_level_design/service.py::LowLevelDesignService`** — orchestration points for the
  HLD, draft user stories, and the LLD. Each takes its source service(s) by constructor DI:
  SA + US take `ba_service=` and gate on `ba_service.get_final_brd()`; LLD takes `sa_service=`
  (gate on `sa_service.get_final_hld()`) **and** `ba_service=` (BRD as supporting context),
  and reads the optional draft-user-story stream directly via
  `VersionService(subdir="user_stories")` — never `InitialUserStoryService`. The gate is
  `is_final=True` on the source (lock state irrelevant) or a local `NoFinalBRDError` /
  `NoFinalHLDError` (each module defines its own — no cross-branch import). Their
  `VersionService` uses `subdir="hld"` / `"user_stories"` / `"lld"`.
  `brd_changed_since_hld()` / `brd_changed_since_stories()` / `hld_changed_since_lld()` +
  `source_*_version()` back a non-blocking "may be stale" banner via the `source_ref` stamped
  on v1 (`"brd_v{n}"` / `"hld_v{n}"`) — display hint only, no dependency tracking /
  invalidation / regeneration. The LLD tracks **only** its direct source (HLD); BRD→LLD and
  user-story→LLD staleness are deliberately not tracked.
  Phase 5's **`…/user_story_refinement/service.py::UserStoryRefinementService`**, Phase 6's
  **`…/test_case/service.py::TestCaseService`**, and Phase 7's
  **`…/closure_report/service.py::ClosureReportService`** take **no** service DI — they read
  the other streams through `VersionService(subdir=…)` (Phase 7 additionally reads
  `app.quality.*`) directly. Phase 5 `refine()` gates on a final BRD + an existing story
  version + the story stream being unlocked. Phase 6 gates on a final BRD (`NoFinalBRDError`)
  + the test-case stream being unlocked (`TestCaseLockedError`); HLD/LLD/User Stories are
  optional — a sentinel is passed when absent, never a block. Phase 7 gates **only** on a
  final BRD (`NoFinalBRDError`) + the closure stream being unlocked
  (`ClosureReportLockedError`); it decides `closure_status` deterministically and calls Gemini
  for narrative prose only.
- **`…/business_analyst/agent.py`**, **`…/solution_architect/agent.py`**,
  **`…/initial_user_story/agent.py`**, **`…/low_level_design/agent.py`**,
  **`…/user_story_refinement/agent.py`**, **`…/test_case/agent.py`**, and
  **`…/closure_report/agent.py`** are the only modules that know about Gemini
  (`langchain_google_genai.ChatGoogleGenerativeAI`). `_extract_text` (normalizes Gemini 3+
  str/dict/list content blocks) and `_invoke` are intentionally duplicated across all seven;
  `test_case` and `closure_report` also duplicate the structured-output + transient-retry
  block. (`test_case` classes carry `__test__ = False` so pytest does not try to collect the
  `Test*`-named classes.)
- **`app/services/version_service.py::VersionService`** is the only persistence layer. One
  JSON file per version stream: `outputs/<project_id>/versions.json` (BRD),
  `…/hld/versions.json`, `…/user_stories/versions.json`, `…/lld/versions.json`,
  `…/test_cases/versions.json`, `…/closure_report/versions.json` — via the `subdir` kwarg.
  Each instance only ever reads/writes its own file, so the streams are fully isolated (the
  `user_stories` file is written by both the Phase 3 and Phase 5 services; `test_cases` only
  by Phase 6; `closure_report` only by Phase 7). `BRDVersion` is the shared record type (name
  kept for history); `source_ref` is optional provenance (single `"brd_v{n}"` for Phases 2–4;
  composite for Phases 5–7). **Unchanged since Phase 1** — do not modify it.
- **`app/services/version_text.py::stamp_version_number`** — shared helper both services use
  to force the in-document `**Version:** N` line to match the tracked version.
- **`app/parsers/`**: `detector.py` picks a parser by file extension; `docx_parser` /
  `pdf_parser` (PyMuPDF, imported as `fitz`) / `text_parser` extract raw text;
  `text_cleaner.clean_text` strips page numbers / boilerplate footers before the LLM.

## Non-obvious rules

- **Prompts are never hardcoded.** They live in `app/agents/*/prompts/*.txt` and are
  rendered by `PromptManager`, whose `_SafeFormatter` raises `PromptRenderError` on any
  `{placeholder}` that wasn't supplied. Each agent passes `PromptManager(prompts_dir=…)`
  pointing at its own `prompts/` dir. Tuning prompt wording should not require code changes;
  adding a new `{placeholder}` to a template *does* require passing it from that `agent.py`.
- **Version history is append-only.** `add_version` never mutates existing entries.
  `mark_final` / `unlock_final` only flip the `is_final` / `is_locked` booleans — they
  never rewrite a version's `content`, so finalizing never rewrites history.
- **Lock semantics** (same for all streams). `choose_final_brd` / `choose_final_hld` /
  `choose_final_stories` / `choose_final_lld` / `TestCaseService.choose_final` mark a version
  final *and* lock it. While locked, `save_manual_edit` / `refine_with_ai` (and Phase 5's
  `refine()`, Phase 6's `generate/regenerate/refine_with_ai`) raise `BRDLockedError` /
  `HLDLockedError` / `UserStoryLockedError` / `LLDLockedError` / `RefinementLockedError` /
  `TestCaseLockedError`. `unlock_final*` releases the lock but keeps `is_final` and the
  content; any later edit creates a brand new version.
- **Version-line stamping.** `stamp_version_number` (in `app/services/version_text.py`)
  force-rewrites the in-document `**Version:** N` line to match the tracked version after
  every generate, edit, and refine — because the refine prompts deliberately tell the model
  to leave unrelated content untouched, so it would never bump that line itself.
- **`app/document_generator/brd_generator.py`** is the only markdown→docx path.
  `generate_hld_docx`, `generate_user_stories_docx`, `generate_lld_docx`,
  `generate_test_cases_docx`, and `generate_closure_report_docx` all delegate to
  `generate_brd_docx` (every stored document is Markdown — the QA service renders the agent's
  JSON to Markdown before persisting; the Closure service renders deterministic facts +
  narrative to Markdown). It is
  intentionally a minimal converter that handles only what the templates emit: `#`/`##`/`###`
  headings, `-`/`*` bullets, `1.` numbered items, `|`-delimited tables, and `**Key:** value`
  metadata lines. Don't grow it into a full markdown engine — change the prompt templates'
  output shape instead.
- **Config and logging are singletons.** Import `settings` from `app/utils/config.py`
  rather than calling `os.getenv`. Get loggers via `app/utils/logger.py::get_logger(name)`
  (one rotating `logs/app.log` + console; idempotent so Streamlit reruns don't stack
  handlers).
- **Imports are absolute from the `app.` package root.** `streamlit_app.py` appends the
  repo root to `sys.path` so `streamlit run app/ui/streamlit_app.py` works from the root.

## Orchestration (`app/orchestration/`)

A single sequential LangGraph wires the seven agents:
`resolve_state → ensure_brd → gate_brd → ensure_hld/ensure_user_stories → gate_hld →
ensure_lld → gate_lld → ensure_test_cases → gate_test_cases → ensure_closure_report →
gate_closure_report → END`. Each `ensure_*` node is a thin delegator to the existing
service's public generate method, guarded by an idempotency check (`*_latest_version`);
each `gate_*` node is read-only and stops the run at the next required human approval. The
graph **never finalizes anything**. `build_sdlc_graph()` / `run_step()` take every service
as an optional injection point; `sdlc_status()` is a pure read-only snapshot with a
`next_step` vocabulary. Traceability and the Project Quality Report are **not** graph nodes
(pure read-only `app.quality.*` functions, no approval lifecycle). `refine_user_stories_step()`
is a standalone non-node entry point (Phase 5 is deliberately not in the graph).

## Scope boundaries (Phases 1–7)

Deliberately **not** in this build — do not add without being asked: a Documentation Agent,
Project Memory, RAG, a real database (SQLite/H2/Postgres/Supabase/…), authentication /
multi-user, load/perf/security-penetration test generation, and any generic multi-agent
framework or shared base agent/service class (including consolidating the duplicated
`_extract_text` / `_invoke` / `_derive_metadata_*` / structured-retry block across the seven
agent packages). Persistence stays JSON; the database decision is deferred to the Project
Memory phase.

## Environment variables (`.env`)

`GOOGLE_API_KEY` (required) · `GEMINI_MODEL` (default `gemini-3.5-flash`) ·
`GEMINI_TEMPERATURE` (default `0.3`) · `GEMINI_TIMEOUT_SECONDS` (default `900`) ·
`GEMINI_MAX_RETRIES` (default `2`) · `UPLOAD_DIR` / `OUTPUT_DIR` / `LOG_DIR` ·
`LOG_LEVEL` (default `INFO`). `outputs/`, `uploads/`, `logs/` are created on demand
and are git-ignored.

**Phase 13B — every field is validated once in `Settings()` (`app/utils/config.py`);
an invalid `.env` raises `pydantic.ValidationError` at import, which the Streamlit
startup guard turns into a clean `st.error` + `st.stop` (no traceback, key never
shown).** `GOOGLE_API_KEY` — non-empty, not the `.env.example` placeholder,
stored stripped. `GEMINI_TEMPERATURE` — `0.0`–`2.0` inclusive. `GEMINI_TIMEOUT_SECONDS`
— integer `>= 1`. `GEMINI_MAX_RETRIES` — integer `>= 0`. `LOG_LEVEL` —
`{DEBUG,INFO,WARNING,ERROR,CRITICAL}`, case-insensitive, stored upper-case.
`GEMINI_MODEL` is **not** range-checked (no model allow-list). `resolved_*_dir()`
return absolute `Path`s (relative values resolve against the launch directory — run
from the repo root) and still `mkdir(exist_ok=True)`. `settings.summary_for_log()`
returns a secret-free dict (model, temperature, timeout_seconds, max_retries,
log_level, output/upload/log dirs — **never** the API key) for the one sanitized
`config: …` INFO line emitted at Streamlit startup. `.streamlit/config.toml`
(tracked) sets `client.showErrorDetails = false`, `server.headless = true`,
`server.maxUploadSize = 25`.
