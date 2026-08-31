# SDLC Agent — BRD · HLD · User Stories · LLD · Story Refinement · Test Cases

The AI-powered SDLC platform, built one agent at a time:

- **Phase 1 — Business Analyst Agent:** turns an uploaded Statement of Work
  (SOW) into a Business Requirement Document (BRD).
- **Phase 2 — Solution Architect Agent:** turns the **accepted/final BRD**
  into a High-Level Design (HLD).
- **Phase 3 — Initial User Story Agent:** turns the **accepted/final BRD**
  into draft user stories. An *independent* branch from the BRD — parallel to
  the Solution Architect Agent, needs no HLD or LLD.
- **Phase 4 — Low-Level Design Agent:** turns the **accepted/final HLD** into a
  Low-Level Design. The accepted HLD is the only hard prerequisite; the BRD is
  supporting context and draft user stories, *if they exist*, are optional
  functional context. It does **not** depend on the User Story Agent.
- **Phase 5 — User Story Refinement Agent:** reconciles the existing user
  stories against the accepted BRD (primary) plus the accepted HLD and LLD
  (optional context), producing **refined user stories appended to the same
  user-story stream** — no second history. It reads BRD/HLD/LLD only through
  the shared version-store interface and imports no other agent package. If the
  BRD, HLD, or LLD later changes, the refined stories are *flagged stale*
  (never mutated); only an explicit "Refine Again" produces a new version. A
  refined document carries its own provenance: `Source: Artifact Refinement`
  and a `Built From:` line listing the exact source versions used.
- **Phase 6 — QA / Test Case Agent:** generates QA test cases from the
  **accepted/final BRD** (the only hard prerequisite) plus the accepted HLD,
  LLD, and final/refined-or-latest User Stories (all **optional context** — a
  missing one never blocks BRD-based generation and is recorded as
  `unavailable` in provenance). The LLM returns strict JSON; the service
  validates it and renders a Markdown test-case document (stable `TC-NNN` ids)
  into its **own** append-only `test_cases` stream. Per-source staleness:
  a used artifact whose authoritative version later changes flags the test
  cases stale (never auto-regenerated); a previously-absent optional artifact
  appearing later does **not**.

Every stage shares the same lifecycle: manual editing, AI-assisted refinement,
full version history, final-version locking/unlocking, and Word export. The BRD,
HLD, user-story, LLD, and test-case version streams are each independent; Phase
3 and Phase 5 both write to the one user-story stream, and the QA agent only
ever writes the test-case stream.

No databases, no LangGraph, no RAG — plain JSON persistence, done properly.

---

## 1. Prerequisites

You need:

1. **Python 3.11 or newer** installed.
   - Check with: `python --version` (Windows) or `python3 --version` (Mac/Linux)
   - If not installed: https://www.python.org/downloads/ (on Windows, tick
     "Add Python to PATH" during install).
2. **VS Code** installed, with the **Python extension** (from the Extensions
   tab, search "Python", install the Microsoft one).
3. A **Google Gemini API key** (free tier available):
   - Go to https://aistudio.google.com/app/apikey
   - Sign in with a Google account, click "Create API key", copy it.

---

## 2. Project Setup (do this once)

Open the project folder in VS Code (`File → Open Folder…` → select
`sdlc-ba-agent`), then open a terminal inside VS Code
(`Terminal → New Terminal`) and run the following, one line at a time.

### Step 2.1 — Create a virtual environment

A virtual environment keeps this project's Python packages separate from
everything else on your machine.

**Windows (PowerShell):**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```
If PowerShell blocks the activation script, run this once first:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Mac / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

You'll know it worked because your terminal prompt now starts with `(venv)`.

> You must run the activation command every time you open a new terminal to
> work on this project.

### Step 2.2 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs Streamlit (UI), LangChain + Gemini SDK (AI), python-docx and
PyMuPDF (file parsing/export), and pydantic (config/validation).

### Step 2.3 — Configure your API key

1. Copy `.env.example` to a new file named `.env` in the project root.
   - Windows: `copy .env.example .env`
   - Mac/Linux: `cp .env.example .env`
2. Open `.env` in VS Code and replace `your_gemini_api_key_here` with the
   real API key you copied from Google AI Studio.
3. Save the file. **Never commit `.env` to git** — it contains your secret key.

---

## 3. Running the App

With your virtual environment activated (prompt shows `(venv)`):

```bash
streamlit run app/ui/streamlit_app.py
```

Your browser should open automatically to `http://localhost:8501`. If not,
open that URL manually.

To stop the app, go back to the terminal and press `Ctrl + C`.

---

## 4. Using the App

1. **Tab 1 — Upload & Generate**
   - Fill in Project Name, Client Name, Project Type, Industry.
   - Upload a `.docx`, `.pdf`, or `.txt` SOW file.
   - Click **Generate BRD**. This extracts the text, cleans it, and sends it
     to Gemini to produce BRD Version 1.

2. **Tab 2 — Review, Edit & Finalize**
   - View the generated BRD in an editable text box.
   - **Manual Edit**: change the text directly, click "Save Manual Edit" →
     creates a new version.
   - **AI Refine**: type an instruction like *"Add Multi-Factor
     Authentication"* or *"Replace MySQL with PostgreSQL"*, click
     "Refine with AI" → Gemini updates only the relevant sections and a new
     version is created.
   - **Sidebar**: browse all past versions, view any of them, or mark one as
     the **Final BRD** (locks it from further edits).
   - **Download**: click "Prepare .docx for download" then
     "Download BRD.docx" to get a professionally formatted Word document.

3. **Start New Project** (sidebar button) resets the session so you can
   upload a different SOW. Each project's version history is saved under
   `outputs/<project_id>/versions.json`.

4. **Tab 3 — HLD Workspace** (Phase 2, Solution Architect Agent)
   - Available only once a BRD has been marked **Final** (Tab 2). Until then
     the tab shows *"Accept a BRD before generating the HLD."* and the button
     is disabled — the HLD is never built from the SOW or a draft BRD.
   - Click **Generate HLD** to produce **HLD Version 1** from the accepted
     BRD. It contains Architecture Overview, Architecture Description, Modules /
     Components, Responsibilities, Interactions, API Overview, Deployment,
     Database Design Overview, Security, Scalability/Performance, and
     Assumptions & Architectural Decisions — kept deliberately high-level.
   - **Edit**, **AI Refine** (e.g. *"Add a caching layer for frequently
     accessed product data."*), **History**, **Choose Final HLD**, **Unlock**,
     and **Download HLD.docx** work exactly like the BRD workspace.
   - If the accepted BRD is changed after the HLD was generated, a
     non-blocking warning appears saying the HLD may be stale. Nothing is
     regenerated or deleted automatically — the HLD stays independently
     versioned under `outputs/<project_id>/hld/versions.json`.

5. **Tab 4 — User Story Workspace** (Phase 3, Initial User Story Agent)
   - Available once a BRD has been marked **Final** (Tab 2). Independent of the
     HLD — you do **not** need to generate an HLD first. Until a Final BRD
     exists the tab shows *"Accept a BRD before generating user stories."* and
     the button is disabled; stories are never built from the SOW, a draft
     BRD, the HLD, or an LLD.
   - Click **Generate Draft User Stories** to produce **User Stories Version 1**
     from the accepted BRD. Each story has an ID (`US-001`, …), Title, the
     *"As a … I want … so that …"* statement, testable Acceptance Criteria,
     Priority, and — where the BRD supports it — a BRD Reference, Dependencies,
     and Notes. The stories are business-focused; technical detail is added
     later by the future User Story Refinement stage.
   - **Edit**, **AI Refine** (freeform: *"Add a story for password reset via
     email."*), **History**, **Choose Final**, **Unlock**, and **Download
     UserStories.docx** work exactly like the BRD and HLD workspaces.
   - **Refine (Artifacts)** sub-tab (Phase 5): click **Refine Stories from
     Artifacts** to reconcile the current latest version against the accepted
     BRD (primary) plus the accepted HLD and LLD (optional context). Existing
     `US-NNN` IDs and unaffected stories are preserved; only evidence-based
     changes are made. This appends a new version (labelled *Artifact
     Refinement*) to the **same** user-story stream — it does not replace
     Phase 3's generation and does not start a second history.
   - **Three-way staleness:** each artifact-refinement version records the BRD,
     HLD and LLD versions it used. If any of those later changes, a non-blocking
     banner names the changed sources (*"…the following changed: BRD, LLD…"*)
     and offers **Refine Again**. The existing refined version is never mutated
     and stays viewable in History; only an explicit Refine Again creates a new
     version that re-records the current source versions.
   - If the accepted BRD changes after generation (before any artifact
     refinement), the Phase 3 BRD-only stale hint still applies. Everything
     stays independently versioned under
     `outputs/<project_id>/user_stories/versions.json`.

6. **Tab 5 — LLD Workspace** (Phase 4, Low-Level Design Agent)
   - Available once an HLD has been marked **Final** (Tab 3). The accepted HLD
     is the **only** hard prerequisite — user stories are *not* required. Until
     a Final HLD exists the tab shows *"Accept an HLD before generating the
     LLD."* and the button is disabled.
   - Click **Generate LLD** to produce **LLD Version 1** from the accepted HLD
     (primary technical source), with the accepted BRD as supporting context
     and — *if a user-story version exists* — the most relevant draft user
     stories as optional functional context. If no user stories exist, the LLD
     is generated from the HLD and BRD alone.
   - The LLD contains implementation-level detail: detailed component design,
     classes/responsibilities/interfaces, service & API specifications,
     request/response structures and data models, database schema where
     applicable, sequence flows, validation rules, error handling, processing
     logic, dependencies, security considerations, and a requirement/user-story
     → implementation mapping.
   - **Edit**, **AI Refine** (e.g. *"Add a caching table for product
     lookups."*), **History**, **Choose Final LLD**, **Unlock**, and **Download
     LLD.docx** work exactly like the other workspaces.
   - If the accepted **HLD** changes after the LLD was generated, a
     non-blocking warning says the LLD may be stale. Nothing is regenerated,
     invalidated, or deleted — the LLD stays independently versioned under
     `outputs/<project_id>/lld/versions.json`. (BRD changes are already
     surfaced in the HLD workspace; re-finalising the HLD then triggers this
     LLD warning.)

7. **Tab 7 — QA / Test Case Workspace** (Phase 6, QA / Test Case Agent)
   - Available once a BRD has been marked **Final**. The **Source Artifacts**
     panel shows BRD (Accepted / Required), HLD / LLD (Accepted / Context /
     Optional or *none / optional*), and User Stories (Final or Latest /
     Context / Optional). Optional artifacts never block generation.
   - Click **Generate Test Cases** to produce **Test Cases Version 1** from the
     accepted BRD plus whatever optional context exists. Each case has a stable
     `TC-NNN` id, Title, Requirement/User-Story reference, Preconditions, Test
     Data, Test Steps, Expected Result, Priority and Test Type.
   - **Edit** (Markdown), **AI Refine** (feedback-driven; unaffected cases and
     their `TC-NNN` ids preserved), **Regenerate from Artifacts** (fresh
     rebuild), **History**, **Choose Final**, **Unlock**, and **Download
     TestCases.docx** work like the other workspaces.
   - Per-source **stale** warning: if a source artifact that was *used* changes
     its authoritative version (`BRD v1 → v2`, …), the test cases are flagged
     stale and never regenerated automatically; a previously-absent optional
     artifact appearing later is not stale. Everything is independently
     versioned under `outputs/<project_id>/test_cases/versions.json`.

---

## 5. Project Structure

```
sdlc-ba-agent/
  app/
    agents/
      business_analyst/
        agent.py             <- Gemini/LangChain wrapper (generate + refine)
        service.py           <- orchestrates parsing -> cleaning -> agent -> versions
        prompt_manager.py    <- loads & renders prompt templates (no hardcoded prompts)
        prompts/
          generate_brd.txt
          refine_brd.txt
      solution_architect/    <- Phase 2: accepted BRD -> HLD
        agent.py             <- Gemini/LangChain wrapper (generate_hld + refine_hld)
        service.py           <- BRD gate + orchestrates agent -> HLD versions (subdir "hld")
        prompts/
          generate_hld.txt
          refine_hld.txt
      initial_user_story/    <- Phase 3: accepted BRD -> draft user stories (independent of HLD)
        agent.py             <- Gemini/LangChain wrapper (generate_stories + refine_stories)
        service.py           <- BRD gate + orchestrates agent -> story versions (subdir "user_stories")
        prompts/
          generate_user_stories.txt
          refine_user_stories.txt
      low_level_design/      <- Phase 4: accepted HLD (+ BRD / optional stories) -> LLD
        agent.py             <- Gemini/LangChain wrapper (generate_lld + refine_lld)
        service.py           <- HLD gate + reads user_stories stream via VersionService -> LLD versions (subdir "lld")
        prompts/
          generate_lld.txt
          refine_lld.txt
      user_story_refinement/ <- Phase 5: BRD (+ optional HLD/LLD) reconciles user stories
        agent.py             <- Gemini/LangChain wrapper (refine_user_stories)
        service.py           <- BRD gate + reads BRD/HLD/LLD via VersionService; appends to the "user_stories" stream; three-way staleness + Source/Built From stamping
        prompts/
          refine_from_artifacts.txt
      test_case/             <- Phase 6: accepted BRD (+ optional HLD/LLD/User Stories) -> test cases
        agent.py             <- Gemini/LangChain wrapper (generate_test_cases + refine_test_cases; JSON output)
        service.py           <- BRD gate + reads BRD/HLD/LLD/User Stories via VersionService; validates JSON, renders Markdown; own "test_cases" stream; per-source staleness
        prompts/
          generate_test_cases.txt
          refine_test_cases.txt
    parsers/
      detector.py            <- detects .docx / .pdf / .txt
      docx_parser.py
      pdf_parser.py
      text_parser.py
      text_cleaner.py        <- preprocessing (removes headers/footers/page numbers)
    document_generator/
      brd_generator.py       <- markdown -> .docx (generate_brd_docx / _hld_docx / _user_stories_docx / _lld_docx / _test_cases_docx)
    services/
      version_service.py     <- version history (JSON file per project/stream, no DB yet)
      version_text.py        <- shared "**Version:** N" stamping helper (all document streams)
    utils/
      config.py              <- reads .env via pydantic-settings
      logger.py               <- centralized logging to logs/app.log
    ui/
      streamlit_app.py       <- the UI, calls the service layer only
  tests/                     <- deterministic pytest suite (stub agents, no Gemini calls)
  uploads/                   <- uploaded SOW files land here
  outputs/                   <- <id>/versions.json (BRD) + <id>/hld/ + <id>/user_stories/ + <id>/lld/ + <id>/test_cases/ + .docx exports
  logs/                      <- app.log (rotating)
  requirements.txt
  pytest.ini
  .env.example
```

Run the test suite (no API key or network needed):

```bash
pytest
```

---

## 6. Troubleshooting

- **`ValidationError: google_api_key Field required`** — you haven't created
  `.env` yet, or it's missing `GOOGLE_API_KEY`. See Step 2.3.
- **`streamlit: command not found`** — your virtual environment isn't
  activated. Re-run the activation command from Step 2.1.
- **Gemini errors about quota/rate limit** — the free tier has request
  limits; wait a minute and try again, or check your usage at
  https://aistudio.google.com.
- **Uploaded PDF produces empty/garbled text** — if the PDF is a scanned
  image (not real text), PyMuPDF can't extract it; this Phase 1 build
  doesn't include OCR. Use a text-based PDF, DOCX, or TXT instead.

---

## 7. What's Deliberately NOT in Phases 1–6

Phases 1–6 deliver: Business Analyst (SOW → BRD), Solution Architect
(BRD → HLD), Initial User Story (BRD → draft stories), Low-Level Design
(HLD → LLD), User Story Refinement (BRD + HLD + LLD reconcile the stories), and
QA / Test Case (BRD + optional HLD/LLD/User Stories → test cases).
Still **not** included, by scope: a Documentation Agent, a Project Closure /
Report Agent, Project Memory, LangGraph / multi-agent orchestration, RAG, a
real database, load/perf/security-penetration test generation, and
authentication / multi-user support. Persistence is still plain JSON files; the
database decision is deferred to the Project Memory phase. The known
`_extract_text` / `_invoke` / metadata-helper duplication across the six agent
packages is a deliberately deferred cleanup.
