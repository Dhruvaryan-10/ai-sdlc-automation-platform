# BA Agent — Phase 1 (SOW → BRD)

This is **Phase 1** of the AI-powered SDLC platform: a single AI Agent (the
**Business Analyst Agent**) that turns an uploaded Statement of Work (SOW)
into a Business Requirement Document (BRD), with manual editing, AI-assisted
refinement, full version history, final-version locking, and Word export.

No databases, no LangGraph, no other agents — just this one, done properly.

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
    parsers/
      detector.py            <- detects .docx / .pdf / .txt
      docx_parser.py
      pdf_parser.py
      text_parser.py
      text_cleaner.py        <- preprocessing (removes headers/footers/page numbers)
    document_generator/
      brd_generator.py       <- markdown BRD -> formatted .docx
    services/
      version_service.py     <- version history (JSON file per project, no DB yet)
    utils/
      config.py              <- reads .env via pydantic-settings
      logger.py               <- centralized logging to logs/app.log
    ui/
      streamlit_app.py       <- the UI, calls the service layer only
  uploads/                   <- uploaded SOW files land here
  outputs/                   <- generated BRDs + exported .docx files
  logs/                      <- app.log (rotating)
  requirements.txt
  .env.example
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

## 7. What's Deliberately NOT in Phase 1

Per scope, this build does **not** include: HLD/LLD generation, User Story or
Test Case generation, LangGraph multi-agent orchestration, a real database,
authentication/multi-user support, or the Solution Architect Agent. Those are
Phase 2+.
