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

Every stage shares one lifecycle: generate → manual edit / AI refine → append-only version
history → choose final (locks) → unlock → Word export. BRD, HLD, and user-story versions are
stored in separate streams. Each new agent is built **structurally parallel** to the
existing ones (same wrapper/service shape) — there is deliberately no shared base class and
`_extract_text` / `_invoke` / `_derive_metadata_from_brd` are duplicated per agent. Do not
consolidate until a dedicated post-Phase-3 cleanup.

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
pytest tests/test_initial_user_story_service.py::test_no_brd_blocks_story_generation  # single test
```

There is **no build step and no linter** configured. The `pytest` suite (added in Phase 2)
uses stub agents injected via the `agent=` constructor param and monkeypatches
`settings.output_dir` to a tmp dir — it never calls Gemini. Real generation/refinement is
verified manually through the Streamlit UI. A missing or invalid `GOOGLE_API_KEY` raises a
pydantic `ValidationError` at startup (by design — see `app/utils/config.py`); `conftest.py`
sets a dummy key before importing `app`.

## Architecture — strict layering

Work flows in one direction only; keep it that way.

- **`app/ui/streamlit_app.py`** renders widgets and maps exceptions to plain-English
  messages (`friendly_error`). It calls **only** the three services and the docx helpers
  (`generate_brd_docx` / `generate_hld_docx` / `generate_user_stories_docx`) — never parsers,
  the agents, or `VersionService` directly. Users must never see a traceback. Four tabs:
  Upload & Generate, BRD Workspace, HLD Workspace, User Story Workspace. `session_state` keys
  are prefixed per workspace — `hld_` for HLD, `us_` for user stories — so they never
  collide.
- **`…/business_analyst/service.py::BusinessAnalystService`** — orchestration point for the
  BRD: `extract → clean → agent → version`, plus manual edit, AI refine, finalize/lock.
- **`…/solution_architect/service.py::SolutionArchitectService`** and
  **`…/initial_user_story/service.py::InitialUserStoryService`** — orchestration points for
  the HLD and for draft user stories. Each is constructed with a `BusinessAnalystService`
  (`ba_service=`); each `generate_initial_*` requires `ba_service.get_final_brd()` to return
  a version (`is_final=True`; lock state irrelevant) or raises its own `NoFinalBRDError`
  (each module defines its **own** local copy — the User Story branch must not import from
  `solution_architect`). Their `VersionService` uses `subdir="hld"` / `subdir="user_stories"`.
  `brd_changed_since_hld()` / `brd_changed_since_stories()` + `source_brd_version()` back a
  non-blocking "may be stale" banner via the `source_ref` stamped on v1 (`"brd_v{n}"`) —
  display hint only, no dependency tracking / invalidation / regeneration.
- **`…/business_analyst/agent.py`**, **`…/solution_architect/agent.py`**, and
  **`…/initial_user_story/agent.py`** are the only modules that know about Gemini
  (`langchain_google_genai.ChatGoogleGenerativeAI`). `_extract_text` (normalizes Gemini 3+
  str/dict/list content blocks) and `_invoke` are intentionally duplicated across all three.
- **`app/services/version_service.py::VersionService`** is the only persistence layer. One
  JSON file per version stream: `outputs/<project_id>/versions.json` (BRD),
  `outputs/<project_id>/hld/versions.json` (HLD), and
  `outputs/<project_id>/user_stories/versions.json` (stories) — via the `subdir` kwarg. Each
  instance only ever reads/writes its own file, so the streams are fully isolated.
  `BRDVersion` is the shared record type (name kept for history); `source_ref` is optional
  provenance.
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
- **Lock semantics** (same for all three streams). `choose_final_brd` / `choose_final_hld` /
  `choose_final_stories` mark a version final *and* lock it. While locked, `save_manual_edit`
  and `refine_with_ai` raise `BRDLockedError` / `HLDLockedError` / `UserStoryLockedError`.
  `unlock_final*` releases the lock but keeps `is_final` and the content; any later edit
  creates a brand new version.
- **Version-line stamping.** `stamp_version_number` (in `app/services/version_text.py`)
  force-rewrites the in-document `**Version:** N` line to match the tracked version after
  every generate, edit, and refine — because the refine prompts deliberately tell the model
  to leave unrelated content untouched, so it would never bump that line itself.
- **`app/document_generator/brd_generator.py`** is the only markdown→docx path.
  `generate_hld_docx` and `generate_user_stories_docx` both delegate to `generate_brd_docx`
  (all three documents share markdown conventions). It is intentionally a minimal converter
  that handles only what the prompt templates emit: `#`/`##`/`###` headings, `-`/`*` bullets,
  `1.` numbered items, `|`-delimited tables, and `**Key:** value` metadata lines. Don't grow
  it into a full markdown engine — change the prompt templates' output shape instead.
- **Config and logging are singletons.** Import `settings` from `app/utils/config.py`
  rather than calling `os.getenv`. Get loggers via `app/utils/logger.py::get_logger(name)`
  (one rotating `logs/app.log` + console; idempotent so Streamlit reruns don't stack
  handlers).
- **Imports are absolute from the `app.` package root.** `streamlit_app.py` appends the
  repo root to `sys.path` so `streamlit run app/ui/streamlit_app.py` works from the root.

## Scope boundaries (Phases 1–3)

Deliberately **not** in this build — do not add without being asked: LLD / Technical
Architect Agent, the **User Story Refinement Agent** (later stage: BRD + HLD + LLD →
refined final stories), QA / Test Case Agent, Documentation and Project Closure Agents,
Project Memory, LangGraph multi-agent orchestration, RAG, a real database
(SQLite/H2/Postgres/Supabase/…), authentication / multi-user, and any generic multi-agent
framework or shared base agent/service class (including consolidating the duplicated
`_extract_text` / `_invoke` / `_derive_metadata_from_brd`). Persistence stays JSON; the
database decision is deferred to the Project Memory phase.

## Environment variables (`.env`)

`GOOGLE_API_KEY` (required) · `GEMINI_MODEL` (default `gemini-3.5-flash`) ·
`GEMINI_TEMPERATURE` (default `0.3`) · `UPLOAD_DIR` / `OUTPUT_DIR` / `LOG_DIR` ·
`LOG_LEVEL`. `outputs/`, `uploads/`, `logs/` are created on demand and are git-ignored.
