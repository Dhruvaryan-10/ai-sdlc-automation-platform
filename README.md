# SDLC Agent — Phase 1 (SOW → BRD) + Phase 2 (BRD → HLD) + Phase 3 (BRD → User Stories)

The AI-powered SDLC platform, built one agent at a time:

- **Phase 1 — Business Analyst Agent:** turns an uploaded Statement of Work
  (SOW) into a Business Requirement Document (BRD).
- **Phase 2 — Solution Architect Agent:** turns the **accepted/final BRD**
  into a High-Level Design (HLD).
- **Phase 3 — Initial User Story Agent:** turns the **accepted/final BRD**
  into draft user stories. This is an *independent* branch from the BRD — it
  runs in parallel with the Solution Architect Agent and does **not** need the
  HLD (or any later LLD).

Every stage shares the same lifecycle: manual editing, AI-assisted refinement,
full version history, final-version locking/unlocking, and Word export. BRD,
HLD, and user-story versions are each stored in their own independent stream.

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
   - **Edit**, **AI Refine** (e.g. *"Add a story for password reset via
     email."*), **History**, **Choose Final**, **Unlock**, and **Download
     UserStories.docx** work exactly like the BRD and HLD workspaces.
   - If the accepted BRD changes after the stories were generated, a
     non-blocking warning says they may be stale. Nothing is regenerated,
     invalidated, or deleted — the stories stay independently versioned under
     `outputs/<project_id>/user_stories/versions.json`.

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
    parsers/
      detector.py            <- detects .docx / .pdf / .txt
      docx_parser.py
      pdf_parser.py
      text_parser.py
      text_cleaner.py        <- preprocessing (removes headers/footers/page numbers)
    document_generator/
      brd_generator.py       <- markdown -> .docx (generate_brd_docx / _hld_docx / _user_stories_docx)
    services/
      version_service.py     <- version history (JSON file per project/stream, no DB yet)
      version_text.py        <- shared "**Version:** N" stamping helper (BRD + HLD + stories)
    utils/
      config.py              <- reads .env via pydantic-settings
      logger.py               <- centralized logging to logs/app.log
    ui/
      streamlit_app.py       <- the UI, calls the service layer only
  tests/                     <- deterministic pytest suite (stub agents, no Gemini calls)
  uploads/                   <- uploaded SOW files land here
  outputs/                   <- <id>/versions.json (BRD) + <id>/hld/ + <id>/user_stories/ + .docx exports
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

## 7. What's Deliberately NOT in Phases 1–3

Phase 1 delivered the Business Analyst Agent (SOW → BRD). Phase 2 added the
Solution Architect Agent (accepted BRD → HLD). Phase 3 adds the Initial User
Story Agent (accepted BRD → draft user stories). Still **not** included, by
scope: LLD / Technical Architect Agent, the **User Story Refinement Agent**
(the later stage that combines BRD + HLD + LLD into technically refined final
stories), QA / Test Case Agent, Documentation and Project Closure Agents,
Project Memory, LangGraph multi-agent orchestration, RAG, a real database, and
authentication / multi-user support. Persistence is still plain JSON files; the
database decision is deferred to the Project Memory phase.
