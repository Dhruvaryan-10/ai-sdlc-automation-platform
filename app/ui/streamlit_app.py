"""
Streamlit UI for Phase 1: SOW -> BRD Business Analyst Agent.

DESIGN NOTE (why this file stays "dumb"):
This module renders widgets and nothing else. Every piece of real work —
parsing, Gemini calls, version creation, lock management, DOCX export — is
delegated to BusinessAnalystService or the document generator. That keeps the
business logic testable without Streamlit, and means this UI could be swapped
for a real web frontend without touching any of the logic beneath it.

ERROR HANDLING POLICY:
Normal users must never see a Python traceback. Every action is wrapped in a
handler that maps known exception types to plain-English messages, logs the
full technical detail, and falls back to a generic message for anything
unexpected. API keys and secrets are never rendered or logged.

Run with:  streamlit run app/ui/streamlit_app.py
"""

import sys
import time
import uuid
from pathlib import Path

import streamlit as st

# Allow running as `streamlit run app/ui/streamlit_app.py` from the project root.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.agents.business_analyst.agent import BusinessAnalystAgentError, ProjectMetadata
from app.agents.business_analyst.service import (
    BRDLockedError,
    BusinessAnalystService,
    EmptyDocumentError,
    UnsupportedFileTypeError,
)
from app.agents.solution_architect.agent import SolutionArchitectAgentError
from app.agents.solution_architect.service import (
    HLDLockedError,
    NoFinalBRDError,
    SolutionArchitectService,
)
from app.agents.initial_user_story.agent import InitialUserStoryAgentError
from app.agents.initial_user_story.service import (
    InitialUserStoryService,
    UserStoryLockedError,
)
from app.agents.initial_user_story.service import (
    NoFinalBRDError as NoFinalBRDErrorForStories,
)
from app.agents.low_level_design.agent import LLDAgentError
from app.agents.low_level_design.service import (
    LLDLockedError,
    LowLevelDesignService,
    NoFinalHLDError,
)
from app.agents.user_story_refinement.agent import UserStoryRefinementAgentError
from app.agents.user_story_refinement.service import (
    NoInitialUserStoriesError,
    RefinementLockedError,
    UserStoryRefinementService,
)
from app.agents.user_story_refinement.service import (
    NoFinalBRDError as NoFinalBRDErrorForRefinement,
)
from app.agents.test_case.agent import TestCaseAgentError
from app.agents.test_case.service import (
    TestCaseLockedError,
    TestCaseService,
)
from app.agents.test_case.service import (
    NoFinalBRDError as NoFinalBRDErrorForQA,
)
from app.document_generator.brd_generator import (
    generate_brd_docx,
    generate_hld_docx,
    generate_lld_docx,
    generate_test_cases_docx,
    generate_user_stories_docx,
)
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

st.set_page_config(page_title="BA Agent - SOW to BRD", layout="wide")


# --- human-readable labels ------------------------------------------------------

SOURCE_LABELS = {
    "initial": "Initial Generation",
    "manual_edit": "Manual Edit",
    "ai_refine": "AI Refinement",
}


def story_version_label(v) -> str:
    """Human label for a user-story version.

    A Phase 5 artifact-refinement version has source == "ai_refine" AND a
    composite source_ref (contains ';'); the Phase 3 freeform "AI Refine" path
    leaves source_ref None, so the two are distinguishable in the workspace.
    """
    if v.source == "ai_refine" and v.source_ref and ";" in v.source_ref:
        return "Artifact Refinement"
    return SOURCE_LABELS.get(v.source, v.source)


def friendly_error(exc: Exception) -> str:
    """Map an exception to a user-facing message. Technical detail goes to logs only."""
    logger.error(f"{type(exc).__name__}: {exc}", exc_info=True)

    if isinstance(exc, UnsupportedFileTypeError):
        return "That file type isn't supported. Please upload a DOCX, PDF, or TXT file."
    if isinstance(exc, EmptyDocumentError):
        return ("No readable text was found in that document. If it's a scanned PDF, "
                "please upload a text-based version instead.")
    if isinstance(exc, (BusinessAnalystAgentError, SolutionArchitectAgentError,
                        InitialUserStoryAgentError, LLDAgentError,
                        UserStoryRefinementAgentError, TestCaseAgentError)):
        return ("Unable to reach the Gemini API. Please check your API key and network "
                "connection, then try again.")
    if isinstance(exc, (NoFinalBRDError, NoFinalBRDErrorForStories, NoFinalHLDError,
                        NoFinalBRDErrorForRefinement, NoInitialUserStoriesError,
                        NoFinalBRDErrorForQA)):
        return str(exc)
    if isinstance(exc, (BRDLockedError, HLDLockedError, UserStoryLockedError, LLDLockedError,
                        RefinementLockedError, TestCaseLockedError)):
        return str(exc)
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Could not write to disk. Please check folder permissions and try again."
    if isinstance(exc, OSError):
        return "A file system error occurred while saving. Please try again."
    return "Something went wrong while processing that request. Please try again."


# --- session bootstrapping --------------------------------------------------------

if "project_id" not in st.session_state:
    st.session_state.project_id = str(uuid.uuid4())[:8]

if "service" not in st.session_state:
    try:
        st.session_state.service = BusinessAnalystService(project_id=st.session_state.project_id)
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

service: BusinessAnalystService = st.session_state.service

if "sa_service" not in st.session_state:
    try:
        st.session_state.sa_service = SolutionArchitectService(
            project_id=st.session_state.project_id, ba_service=service
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

sa_service: SolutionArchitectService = st.session_state.sa_service

if "us_service" not in st.session_state:
    try:
        st.session_state.us_service = InitialUserStoryService(
            project_id=st.session_state.project_id, ba_service=service
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

us_service: InitialUserStoryService = st.session_state.us_service

if "lld_service" not in st.session_state:
    try:
        st.session_state.lld_service = LowLevelDesignService(
            project_id=st.session_state.project_id,
            sa_service=sa_service,
            ba_service=service,
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

lld_service: LowLevelDesignService = st.session_state.lld_service

if "usr_service" not in st.session_state:
    try:
        st.session_state.usr_service = UserStoryRefinementService(
            project_id=st.session_state.project_id
        )
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

usr_service: UserStoryRefinementService = st.session_state.usr_service

if "qa_service" not in st.session_state:
    try:
        st.session_state.qa_service = TestCaseService(project_id=st.session_state.project_id)
    except Exception as exc:
        st.error(friendly_error(exc))
        st.stop()

qa_service: TestCaseService = st.session_state.qa_service


def refresh_versions() -> None:
    try:
        st.session_state.versions = service.get_all_versions()
    except Exception as exc:
        st.session_state.versions = []
        st.error(friendly_error(exc))


def refresh_hld_versions() -> None:
    try:
        st.session_state.hld_versions = sa_service.get_all_versions()
    except Exception as exc:
        st.session_state.hld_versions = []
        st.error(friendly_error(exc))


def refresh_us_versions() -> None:
    try:
        st.session_state.us_versions = us_service.get_all_versions()
    except Exception as exc:
        st.session_state.us_versions = []
        st.error(friendly_error(exc))


def refresh_lld_versions() -> None:
    try:
        st.session_state.lld_versions = lld_service.get_all_versions()
    except Exception as exc:
        st.session_state.lld_versions = []
        st.error(friendly_error(exc))


def refresh_qa_versions() -> None:
    try:
        st.session_state.qa_versions = qa_service.get_all_versions()
    except Exception as exc:
        st.session_state.qa_versions = []
        st.error(friendly_error(exc))


if "versions" not in st.session_state:
    refresh_versions()

if "hld_versions" not in st.session_state:
    refresh_hld_versions()

if "us_versions" not in st.session_state:
    refresh_us_versions()

if "lld_versions" not in st.session_state:
    refresh_lld_versions()

if "qa_versions" not in st.session_state:
    refresh_qa_versions()

versions = st.session_state.versions
latest_version = versions[-1] if versions else None
final_version = next((v for v in versions if v.is_final), None)
is_locked = bool(final_version and final_version.is_locked)

hld_versions = st.session_state.hld_versions
hld_latest = hld_versions[-1] if hld_versions else None
hld_final = next((v for v in hld_versions if v.is_final), None)
hld_is_locked = bool(hld_final and hld_final.is_locked)

us_versions = st.session_state.us_versions
us_latest = us_versions[-1] if us_versions else None
us_final = next((v for v in us_versions if v.is_final), None)
us_is_locked = bool(us_final and us_final.is_locked)

lld_versions = st.session_state.lld_versions
lld_latest = lld_versions[-1] if lld_versions else None
lld_final = next((v for v in lld_versions if v.is_final), None)
lld_is_locked = bool(lld_final and lld_final.is_locked)

# --- Phase 5 refinement state (reads the SAME user_stories stream, no second store) ---
try:
    usr_recorded = usr_service.recorded_source_versions()
    usr_stale_sources = usr_service.stale_sources()
except Exception:
    usr_recorded, usr_stale_sources = None, []
usr_is_refined = usr_recorded is not None      # latest user-story version came from artifact refinement
usr_stale = bool(usr_stale_sources)            # a recorded source artifact changed since that refinement

qa_versions = st.session_state.qa_versions
qa_latest = qa_versions[-1] if qa_versions else None
qa_final = next((v for v in qa_versions if v.is_final), None)
qa_is_locked = bool(qa_final and qa_final.is_locked)

# --- Phase 6 test-case provenance / per-source staleness (own test_cases stream) ---
try:
    qa_recorded = qa_service.recorded_source_versions()
    qa_current = qa_service.current_source_versions()
    qa_stale_sources = qa_service.stale_sources()
except Exception:
    qa_recorded, qa_current, qa_stale_sources = None, {}, []
qa_stale = bool(qa_stale_sources)


# --- sidebar: project status + version history --------------------------------------

with st.sidebar:
    st.header("Project")
    st.caption(f"Project ID: `{st.session_state.project_id}`")

    if latest_version:
        st.metric("Current Version", f"v{latest_version.version}")
    if final_version:
        status = "Locked" if final_version.is_locked else "Unlocked"
        st.success(f"Accepted BRD: v{final_version.version}\n\nStatus: {status}")
    else:
        st.info("No final BRD selected yet.")

    st.divider()
    st.subheader("Version History")

    if not versions:
        st.caption("No versions yet. Generate a BRD to get started.")
    else:
        for v in reversed(versions):
            markers = []
            if latest_version and v.version == latest_version.version:
                markers.append("CURRENT")
            if v.is_final:
                markers.append("ACCEPTED")
            marker_text = f"  [{' / '.join(markers)}]" if markers else ""

            with st.expander(f"v{v.version} - {SOURCE_LABELS.get(v.source, v.source)}{marker_text}"):
                st.caption(f"Created: {v.created_at}")
                st.caption(f"Type: {SOURCE_LABELS.get(v.source, v.source)}")
                st.caption(f"Status: {'Accepted' if v.is_final else 'Draft'}")
                if v.note:
                    st.caption(f"Change: {v.note}")
                if st.button("View this version", key=f"view_{v.version}"):
                    st.session_state.viewing_version = v.version
                    st.rerun()

    st.divider()
    st.subheader("HLD")
    if hld_latest:
        st.metric("Current HLD Version", f"v{hld_latest.version}")
        if hld_final:
            hld_status = "Locked" if hld_final.is_locked else "Unlocked"
            st.success(f"Final HLD: v{hld_final.version}\n\nStatus: {hld_status}")
        else:
            st.info("No final HLD selected yet.")
    elif final_version:
        st.caption("Ready to generate. Open the HLD Workspace tab.")
    else:
        st.caption("Accept a BRD first to unlock the HLD stage.")

    st.divider()
    st.subheader("User Stories")
    if us_latest:
        st.metric("Current User Stories Version", f"v{us_latest.version}")
        if us_final:
            us_status = "Locked" if us_final.is_locked else "Unlocked"
            st.success(f"Final User Stories: v{us_final.version}\n\nStatus: {us_status}")
        else:
            st.info("No final user stories selected yet.")
    elif final_version:
        st.caption("Ready to generate. Open the User Story Workspace tab.")
    else:
        st.caption("Accept a BRD first to unlock the User Story stage.")

    st.divider()
    st.subheader("LLD")
    if lld_latest:
        st.metric("Current LLD Version", f"v{lld_latest.version}")
        if lld_final:
            lld_status = "Locked" if lld_final.is_locked else "Unlocked"
            st.success(f"Final LLD: v{lld_final.version}\n\nStatus: {lld_status}")
        else:
            st.info("No final LLD selected yet.")
    elif hld_final:
        st.caption("Ready to generate. Open the LLD Workspace tab.")
    else:
        st.caption("Accept an HLD first to unlock the LLD stage.")

    st.divider()
    st.subheader("User Story Refinement")
    if usr_is_refined and usr_recorded is not None:
        st.metric("Refined Stories Version", f"v{us_latest.version}")
        st.caption(
            "Source artifacts: "
            f"BRD v{usr_recorded['brd']} · "
            f"HLD v{usr_recorded['hld'] if usr_recorded['hld'] is not None else '—'} · "
            f"LLD v{usr_recorded['lld'] if usr_recorded['lld'] is not None else '—'}"
        )
        if usr_stale:
            st.warning(f"STALE — changed since refinement: {', '.join(usr_stale_sources)}")
        else:
            st.success("Up to date with the accepted BRD / HLD / LLD.")
    elif us_latest is not None and final_version is not None:
        st.caption("Ready to refine. Open the User Story Refinement tab.")
    elif final_version is not None:
        st.caption("Generate initial user stories (Step 4) first.")
    else:
        st.caption("Accept a BRD first to unlock refinement.")

    st.divider()
    st.subheader("QA / Test Cases")
    if qa_latest is not None:
        st.metric("Current Test Case Version", f"v{qa_latest.version}")
        if qa_recorded is not None:
            def _v(x):
                return f"v{x}" if x is not None else "—"
            st.caption(
                "Built from: "
                f"BRD v{qa_recorded['brd']} · "
                f"HLD {_v(qa_recorded['hld'])} · "
                f"LLD {_v(qa_recorded['lld'])} · "
                f"US {_v(qa_recorded['us'])}"
            )
        if qa_final:
            qa_status = "Locked" if qa_final.is_locked else "Unlocked"
            st.success(f"Final Test Cases: v{qa_final.version}\n\nStatus: {qa_status}")
        if qa_stale:
            st.warning(f"STALE — changed since generation: {', '.join(qa_stale_sources)}")
        elif not qa_final:
            st.info("No final test cases selected yet.")
    elif final_version is not None:
        st.caption("Ready to generate. Open the QA / Test Case tab.")
    else:
        st.caption("Accept a BRD first to unlock test-case generation.")

    st.divider()
    if st.button("Start New Project"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()


# --- main area ------------------------------------------------------------------------

st.title("Business Analyst Agent - SOW to BRD")
st.caption("Phase 1: Statement of Work to Business Requirement Document")

(tab_generate, tab_workspace, tab_hld, tab_stories,
 tab_lld, tab_usr, tab_qa) = st.tabs(
    ["Step 1: Upload & Generate", "Step 2: BRD Workspace", "Step 3: HLD Workspace",
     "Step 4: User Story Workspace", "Step 5: LLD Workspace",
     "Step 6: User Story Refinement", "Step 7: QA / Test Case Workspace"]
)


# --- STEP 1: upload + generate ----------------------------------------------------------

with tab_generate:
    if latest_version is not None:
        st.info("A BRD already exists for this project. Open the BRD Workspace tab to "
                "review it, or start a new project from the sidebar to upload a different SOW.")
    else:
        st.subheader("Project Details")
        col1, col2 = st.columns(2)
        with col1:
            project_name = st.text_input("Project Name", placeholder="e.g. Customer Portal Revamp")
            client_name = st.text_input("Client Name", placeholder="e.g. Acme Corp")
        with col2:
            project_type = st.selectbox(
                "Project Type",
                ["Web Application", "Mobile Application", "Data Platform",
                 "API / Integration", "Internal Tool", "Other"],
            )
            industry = st.text_input("Industry", placeholder="e.g. Banking, Retail, Healthcare")

        st.subheader("Upload Statement of Work (SOW)")
        uploaded_file = st.file_uploader("Supported formats: DOCX, PDF, TXT", type=["docx", "pdf", "txt"])

        ready = bool(uploaded_file and project_name and client_name and industry)

        if st.button("Generate BRD", disabled=not ready, type="primary"):
            with st.spinner("Extracting document, cleaning text, and generating your BRD..."):
                try:
                    upload_path = (settings.resolved_upload_dir()
                                   / f"{st.session_state.project_id}_{uploaded_file.name}")
                    upload_path.write_bytes(uploaded_file.getbuffer())
                    logger.info(f"SOW uploaded: '{uploaded_file.name}' -> '{upload_path}'")

                    metadata = ProjectMetadata(
                        project_name=project_name,
                        client_name=client_name,
                        project_type=project_type,
                        industry=industry,
                    )

                    start = time.time()
                    version = service.generate_initial_brd(upload_path, metadata)
                    elapsed = time.time() - start
                    logger.info(f"BRD v{version.version} generated in {elapsed:.1f}s")

                    refresh_versions()
                    st.session_state.viewing_version = version.version
                    st.success(f"BRD Version {version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 2: workspace ---------------------------------------------------------------------

with tab_workspace:
    if latest_version is None:
        st.info("Upload a SOW and generate a BRD first (Step 1).")
    else:
        viewing_number = st.session_state.get("viewing_version", latest_version.version)
        try:
            viewing_version = service.get_version(viewing_number) or latest_version
        except Exception as exc:
            st.error(friendly_error(exc))
            viewing_version = latest_version

        # This specific version is only editable when it's the newest one AND
        # nothing is locked — editing an older version would silently fork history.
        is_current = viewing_version.version == latest_version.version
        editable = is_current and not is_locked

        # --- status banner ---
        status_cols = st.columns([2, 2, 2])
        with status_cols[0]:
            st.metric("Viewing", f"v{viewing_version.version}")
        with status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(viewing_version.source, viewing_version.source))
        with status_cols[2]:
            st.metric("Status", "Accepted" if viewing_version.is_final else "Draft")

        if is_locked:
            if viewing_version.is_final:
                st.success(f"This is the Accepted BRD (v{viewing_version.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Accepted BRD (v{final_version.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not is_current:
            st.info(f"You are viewing an older version (v{viewing_version.version}). "
                    f"Editing is only available on the current version "
                    f"(v{latest_version.version}).")

        st.divider()

        tab_preview, tab_edit, tab_refine, tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        # --- PREVIEW: render markdown as a real document ---
        with tab_preview:
            st.markdown(viewing_version.content)

        # --- EDIT: manual editing, saving creates a new version ---
        with tab_edit:
            if not editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Accepted BRD to continue." if is_locked
                           else "Switch to the current version to edit."))
                st.text_area("BRD content (read-only)", value=viewing_version.content,
                             height=500, disabled=True, key=f"ro_{viewing_version.version}")
            else:
                st.caption("Edit the BRD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                edited_text = st.text_area(
                    "BRD content (markdown)",
                    value=viewing_version.content,
                    height=500,
                    key=f"editor_{viewing_version.version}",
                )
                change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Corrected stakeholder list",
                    key=f"note_{viewing_version.version}",
                )
                if st.button("Save as New Version", type="primary"):
                    try:
                        new_version = service.save_manual_edit(
                            edited_text, note=change_note.strip() or "Manual edit"
                        )
                        refresh_versions()
                        st.session_state.viewing_version = new_version.version
                        st.success(f"Saved as Version {new_version.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        # --- AI REFINE: current BRD + feedback -> new version ---
        with tab_refine:
            if not editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Accepted BRD to continue." if is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your changes in plain English. The AI receives the CURRENT "
                           "BRD plus your feedback - it does not regenerate from the original SOW. "
                           "Unaffected sections are preserved.")
                feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add Multi-Factor Authentication as a functional requirement",
                    key="feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary", disabled=not feedback.strip()):
                    with st.spinner("Sending the current BRD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_version = service.refine_with_ai(feedback)
                            elapsed = time.time() - start
                            logger.info(f"BRD refined to v{new_version.version} in {elapsed:.1f}s")

                            refresh_versions()
                            st.session_state.viewing_version = new_version.version
                            st.success(f"Created Version {new_version.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        # --- HISTORY: full list with selection ---
        with tab_history:
            st.caption("All versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if latest_version and v.version == latest_version.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"hist_view_{v.version}"):
                    st.session_state.viewing_version = v.version
                    st.rerun()

        st.divider()

        # --- FINAL BRD: choose / unlock / download ---
        st.subheader("Final BRD")
        final_cols = st.columns([2, 2, 2])

        with final_cols[0]:
            if not viewing_version.is_final:
                if st.button(f"Choose v{viewing_version.version} as Final BRD"):
                    try:
                        service.choose_final_brd(viewing_version.version)
                        refresh_versions()
                        st.success(f"Version {viewing_version.version} is now the Accepted BRD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Accepted BRD.")

        with final_cols[1]:
            if is_locked:
                if st.session_state.get("confirm_unlock"):
                    st.warning("Unlock the Accepted BRD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock"):
                        try:
                            service.unlock_final_brd()
                            st.session_state.confirm_unlock = False
                            refresh_versions()
                            st.success("Final BRD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel"):
                        st.session_state.confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final BRD"):
                        st.session_state.confirm_unlock = True
                        st.rerun()

        with final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download"):
                with st.spinner("Formatting Word document..."):
                    try:
                        docx_path = (Path(settings.resolved_output_dir())
                                     / st.session_state.project_id
                                     / f"BRD_v{viewing_version.version}.docx")
                        generate_brd_docx(viewing_version.content, docx_path)
                        st.session_state.docx_ready_path = str(docx_path)
                        st.session_state.docx_ready_version = viewing_version.version
                        logger.info(f"DOCX exported for v{viewing_version.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            ready_path = st.session_state.get("docx_ready_path")
            ready_version = st.session_state.get("docx_ready_version")
            if ready_path and Path(ready_path).exists() and ready_version == viewing_version.version:
                try:
                    with open(ready_path, "rb") as f:
                        st.download_button(
                            f"Download BRD v{viewing_version.version}.docx",
                            data=f.read(),
                            file_name=f"BRD_v{viewing_version.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 3: HLD workspace (Solution Architect Agent) --------------------------------------

with tab_hld:
    st.caption("Phase 2: accepted BRD to High-Level Design")

    if final_version is None:
        st.warning("Accept a BRD before generating the HLD.")
        st.caption("Go to the BRD Workspace, choose a version as the Final BRD, and it will "
                   "become available here. The HLD is only ever generated from the accepted "
                   "BRD - never from the SOW or a draft.")
        st.button("Generate HLD", disabled=True)

    elif hld_latest is None:
        st.subheader("Generate the High-Level Design")
        st.caption(f"The HLD will be generated from the Accepted BRD (v{final_version.version}). "
                   "This creates HLD Version 1.")
        if st.button("Generate HLD", type="primary"):
            with st.spinner("Sending the accepted BRD to Gemini and drafting the HLD..."):
                try:
                    start = time.time()
                    hld_version = sa_service.generate_initial_hld()
                    elapsed = time.time() - start
                    logger.info(f"HLD v{hld_version.version} generated in {elapsed:.1f}s")

                    refresh_hld_versions()
                    st.session_state.hld_viewing_version = hld_version.version
                    st.success(f"HLD Version {hld_version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        hld_viewing_number = st.session_state.get("hld_viewing_version", hld_latest.version)
        try:
            hld_viewing = sa_service.get_version(hld_viewing_number) or hld_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            hld_viewing = hld_latest

        hld_is_current = hld_viewing.version == hld_latest.version
        hld_editable = hld_is_current and not hld_is_locked

        # --- stale-vs-BRD hint (non-blocking; no auto-regeneration) ---
        try:
            if sa_service.brd_changed_since_hld():
                src = sa_service.source_brd_version()
                src_text = f"BRD v{src}" if src is not None else "an earlier BRD version"
                st.warning(
                    f"This HLD was generated from {src_text}, but the Accepted BRD is now "
                    f"v{final_version.version}. The HLD may be stale. Review it, refine it, "
                    "or start a new project to regenerate from scratch - nothing is changed "
                    "automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        hld_status_cols = st.columns([2, 2, 2])
        with hld_status_cols[0]:
            st.metric("Viewing", f"v{hld_viewing.version}")
        with hld_status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(hld_viewing.source, hld_viewing.source))
        with hld_status_cols[2]:
            st.metric("Status", "Accepted" if hld_viewing.is_final else "Draft")

        if hld_is_locked:
            if hld_viewing.is_final:
                st.success(f"This is the Final HLD (v{hld_viewing.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Final HLD (v{hld_final.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not hld_is_current:
            st.info(f"You are viewing an older HLD version (v{hld_viewing.version}). "
                    f"Editing is only available on the current version (v{hld_latest.version}).")

        st.divider()

        hld_tab_preview, hld_tab_edit, hld_tab_refine, hld_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with hld_tab_preview:
            st.markdown(hld_viewing.content)

        with hld_tab_edit:
            if not hld_editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Final HLD to continue." if hld_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("HLD content (read-only)", value=hld_viewing.content,
                             height=500, disabled=True, key=f"hld_ro_{hld_viewing.version}")
            else:
                st.caption("Edit the HLD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                hld_edited_text = st.text_area(
                    "HLD content (markdown)",
                    value=hld_viewing.content,
                    height=500,
                    key=f"hld_editor_{hld_viewing.version}",
                )
                hld_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Clarified deployment topology",
                    key=f"hld_note_{hld_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="hld_save_edit"):
                    try:
                        new_hld = sa_service.save_manual_edit(
                            hld_edited_text, note=hld_change_note.strip() or "Manual edit"
                        )
                        refresh_hld_versions()
                        st.session_state.hld_viewing_version = new_hld.version
                        st.success(f"Saved as HLD Version {new_hld.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with hld_tab_refine:
            if not hld_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Final HLD to continue." if hld_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "HLD plus your feedback - unaffected sections are preserved.")
                hld_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a caching layer for frequently accessed product data.",
                    key="hld_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not hld_feedback.strip(), key="hld_refine_btn"):
                    with st.spinner("Sending the current HLD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_hld = sa_service.refine_with_ai(hld_feedback)
                            elapsed = time.time() - start
                            logger.info(f"HLD refined to v{new_hld.version} in {elapsed:.1f}s")

                            refresh_hld_versions()
                            st.session_state.hld_viewing_version = new_hld.version
                            st.success(f"Created HLD Version {new_hld.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with hld_tab_history:
            st.caption("All HLD versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(hld_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if hld_latest and v.version == hld_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"hld_hist_view_{v.version}"):
                    st.session_state.hld_viewing_version = v.version
                    st.rerun()

        st.divider()

        st.subheader("Final HLD")
        hld_final_cols = st.columns([2, 2, 2])

        with hld_final_cols[0]:
            if not hld_viewing.is_final:
                if st.button(f"Choose v{hld_viewing.version} as Final HLD", key="hld_choose_final"):
                    try:
                        sa_service.choose_final_hld(hld_viewing.version)
                        refresh_hld_versions()
                        st.success(f"HLD Version {hld_viewing.version} is now the Final HLD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Final HLD.")

        with hld_final_cols[1]:
            if hld_is_locked:
                if st.session_state.get("hld_confirm_unlock"):
                    st.warning("Unlock the Final HLD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock", key="hld_unlock_yes"):
                        try:
                            sa_service.unlock_final_hld()
                            st.session_state.hld_confirm_unlock = False
                            refresh_hld_versions()
                            st.success("Final HLD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel", key="hld_unlock_cancel"):
                        st.session_state.hld_confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final HLD", key="hld_unlock_btn"):
                        st.session_state.hld_confirm_unlock = True
                        st.rerun()

        with hld_final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="hld_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        hld_docx_path = (Path(settings.resolved_output_dir())
                                         / st.session_state.project_id
                                         / "hld"
                                         / f"HLD_v{hld_viewing.version}.docx")
                        generate_hld_docx(hld_viewing.content, hld_docx_path)
                        st.session_state.hld_docx_ready_path = str(hld_docx_path)
                        st.session_state.hld_docx_ready_version = hld_viewing.version
                        logger.info(f"HLD DOCX exported for v{hld_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            hld_ready_path = st.session_state.get("hld_docx_ready_path")
            hld_ready_version = st.session_state.get("hld_docx_ready_version")
            if (hld_ready_path and Path(hld_ready_path).exists()
                    and hld_ready_version == hld_viewing.version):
                try:
                    with open(hld_ready_path, "rb") as f:
                        st.download_button(
                            f"Download HLD v{hld_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"HLD_v{hld_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="hld_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 4: User Story workspace (Initial User Story Agent) ------------------------------

with tab_stories:
    st.caption("Phase 3: accepted BRD to draft user stories")

    if final_version is None:
        st.warning("Accept a BRD before generating user stories.")
        st.caption("Go to the BRD Workspace, choose a version as the Final BRD, and it will "
                   "become available here. Draft user stories are generated only from the "
                   "accepted BRD - never from the SOW, a draft BRD, the HLD, or an LLD.")
        st.button("Generate Draft User Stories", disabled=True)

    elif us_latest is None:
        st.subheader("Generate the Draft User Stories")
        st.caption(f"The user stories will be generated from the Accepted BRD "
                   f"(v{final_version.version}). This creates User Stories Version 1.")
        if st.button("Generate Draft User Stories", type="primary"):
            with st.spinner("Sending the accepted BRD to Gemini and drafting user stories..."):
                try:
                    start = time.time()
                    us_version = us_service.generate_initial_stories()
                    elapsed = time.time() - start
                    logger.info(f"User stories v{us_version.version} generated in {elapsed:.1f}s")

                    refresh_us_versions()
                    st.session_state.us_viewing_version = us_version.version
                    st.success(f"User Stories Version {us_version.version} generated "
                               f"in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        us_viewing_number = st.session_state.get("us_viewing_version", us_latest.version)
        try:
            us_viewing = us_service.get_version(us_viewing_number) or us_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            us_viewing = us_latest

        us_is_current = us_viewing.version == us_latest.version
        us_editable = us_is_current and not us_is_locked

        # --- stale-vs-BRD hint (Phase 3; non-blocking, no auto-regeneration) ---
        try:
            if us_service.brd_changed_since_stories():
                src = us_service.source_brd_version()
                src_text = f"BRD v{src}" if src is not None else "an earlier BRD version"
                st.warning(
                    f"These user stories were generated from {src_text}, but the Accepted "
                    f"BRD is now v{final_version.version}. They may be stale. Refine them "
                    "here (freeform), or reconcile them against BRD / HLD / LLD in "
                    "**Step 6: User Story Refinement** - nothing is changed automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        us_status_cols = st.columns([2, 2, 2])
        with us_status_cols[0]:
            st.metric("Viewing", f"v{us_viewing.version}")
        with us_status_cols[1]:
            st.metric("Type", story_version_label(us_viewing))
        with us_status_cols[2]:
            st.metric("Status", "Accepted" if us_viewing.is_final else "Draft")

        if us_is_locked:
            if us_viewing.is_final:
                st.success(f"This is the Final User Stories set (v{us_viewing.version}) and it "
                           "is locked against further changes.")
            else:
                st.warning(f"The Final User Stories (v{us_final.version}) are locked. "
                           "Unlock them below to make further changes.")
        elif not us_is_current:
            st.info(f"You are viewing an older user stories version (v{us_viewing.version}). "
                    f"Editing is only available on the current version (v{us_latest.version}).")

        st.divider()

        us_tab_preview, us_tab_edit, us_tab_refine, us_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with us_tab_preview:
            st.markdown(us_viewing.content)

        with us_tab_edit:
            if not us_editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Final User Stories to continue." if us_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("User stories content (read-only)", value=us_viewing.content,
                             height=500, disabled=True, key=f"us_ro_{us_viewing.version}")
            else:
                st.caption("Edit the user stories below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                us_edited_text = st.text_area(
                    "User stories content (markdown)",
                    value=us_viewing.content,
                    height=500,
                    key=f"us_editor_{us_viewing.version}",
                )
                us_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Reworded the checkout story",
                    key=f"us_note_{us_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="us_save_edit"):
                    try:
                        new_us = us_service.save_manual_edit(
                            us_edited_text, note=us_change_note.strip() or "Manual edit"
                        )
                        refresh_us_versions()
                        st.session_state.us_viewing_version = new_us.version
                        st.success(f"Saved as User Stories Version {new_us.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with us_tab_refine:
            if not us_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Final User Stories to continue." if us_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "user stories plus your feedback - unaffected stories are preserved.")
                us_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a story for password reset via email.",
                    key="us_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not us_feedback.strip(), key="us_refine_btn"):
                    with st.spinner("Sending the current user stories and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_us = us_service.refine_with_ai(us_feedback)
                            elapsed = time.time() - start
                            logger.info(f"User stories refined to v{new_us.version} in {elapsed:.1f}s")

                            refresh_us_versions()
                            st.session_state.us_viewing_version = new_us.version
                            st.success(f"Created User Stories Version {new_us.version} "
                                       f"in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with us_tab_history:
            st.caption("All user stories versions are permanent. "
                       "Nothing is ever overwritten or deleted.")
            for v in reversed(us_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(story_version_label(v))
                cols[2].caption(v.note or "-")
                badges = []
                if us_latest and v.version == us_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"us_hist_view_{v.version}"):
                    st.session_state.us_viewing_version = v.version
                    st.rerun()

        st.divider()

        st.subheader("Final User Stories")
        us_final_cols = st.columns([2, 2, 2])

        with us_final_cols[0]:
            if not us_viewing.is_final:
                if st.button(f"Choose v{us_viewing.version} as Final User Stories",
                             key="us_choose_final"):
                    try:
                        us_service.choose_final_stories(us_viewing.version)
                        refresh_us_versions()
                        st.success(f"User Stories Version {us_viewing.version} is now Final.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Final User Stories set.")

        with us_final_cols[1]:
            if us_is_locked:
                if st.session_state.get("us_confirm_unlock"):
                    st.warning("Unlock the Final User Stories? They stay in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock", key="us_unlock_yes"):
                        try:
                            us_service.unlock_final_stories()
                            st.session_state.us_confirm_unlock = False
                            refresh_us_versions()
                            st.success("Final User Stories unlocked. Further edits will create "
                                       "a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel", key="us_unlock_cancel"):
                        st.session_state.us_confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final User Stories", key="us_unlock_btn"):
                        st.session_state.us_confirm_unlock = True
                        st.rerun()

        with us_final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="us_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        us_docx_path = (Path(settings.resolved_output_dir())
                                        / st.session_state.project_id
                                        / "user_stories"
                                        / f"UserStories_v{us_viewing.version}.docx")
                        generate_user_stories_docx(us_viewing.content, us_docx_path)
                        st.session_state.us_docx_ready_path = str(us_docx_path)
                        st.session_state.us_docx_ready_version = us_viewing.version
                        logger.info(f"User stories DOCX exported for v{us_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            us_ready_path = st.session_state.get("us_docx_ready_path")
            us_ready_version = st.session_state.get("us_docx_ready_version")
            if (us_ready_path and Path(us_ready_path).exists()
                    and us_ready_version == us_viewing.version):
                try:
                    with open(us_ready_path, "rb") as f:
                        st.download_button(
                            f"Download UserStories v{us_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"UserStories_v{us_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="us_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 5: LLD workspace (Low-Level Design Agent) ------------------------------------

with tab_lld:
    st.caption("Phase 4: accepted HLD (+ BRD / optional draft user stories) to Low-Level Design")

    if hld_final is None:
        st.warning("Accept an HLD before generating the LLD.")
        st.caption("Go to the HLD Workspace, choose a version as the Final HLD, and it will "
                   "become available here. The LLD is generated from the accepted HLD; the BRD "
                   "is supporting context and draft user stories are optional context - user "
                   "stories are never required.")
        st.button("Generate LLD", disabled=True)

    elif lld_latest is None:
        st.subheader("Generate the Low-Level Design")
        us_note = (f" Draft user stories (v{us_latest.version}) will be included as optional "
                   "context." if us_latest is not None
                   else " No draft user stories exist yet - the LLD will be generated from the "
                        "HLD and BRD only.")
        st.caption(f"The LLD will be generated from the Accepted HLD (v{hld_final.version})."
                   + us_note + " This creates LLD Version 1.")
        if st.button("Generate LLD", type="primary"):
            with st.spinner("Sending the accepted HLD and context to Gemini and drafting the LLD..."):
                try:
                    start = time.time()
                    lld_version = lld_service.generate_initial_lld()
                    elapsed = time.time() - start
                    logger.info(f"LLD v{lld_version.version} generated in {elapsed:.1f}s")

                    refresh_lld_versions()
                    st.session_state.lld_viewing_version = lld_version.version
                    st.success(f"LLD Version {lld_version.version} generated in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

    else:
        lld_viewing_number = st.session_state.get("lld_viewing_version", lld_latest.version)
        try:
            lld_viewing = lld_service.get_version(lld_viewing_number) or lld_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            lld_viewing = lld_latest

        lld_is_current = lld_viewing.version == lld_latest.version
        lld_editable = lld_is_current and not lld_is_locked

        # --- stale-vs-HLD hint (non-blocking; no auto-regeneration) ---
        try:
            if lld_service.hld_changed_since_lld():
                src = lld_service.source_hld_version()
                src_text = f"HLD v{src}" if src is not None else "an earlier HLD version"
                st.warning(
                    f"This LLD was generated from {src_text}, but the Accepted HLD is now "
                    f"v{hld_final.version}. The LLD may be stale. Review it, refine it, or "
                    "start a new project to regenerate from scratch - nothing is changed "
                    "automatically."
                )
        except Exception as exc:
            st.error(friendly_error(exc))

        # --- status banner ---
        lld_status_cols = st.columns([2, 2, 2])
        with lld_status_cols[0]:
            st.metric("Viewing", f"v{lld_viewing.version}")
        with lld_status_cols[1]:
            st.metric("Type", SOURCE_LABELS.get(lld_viewing.source, lld_viewing.source))
        with lld_status_cols[2]:
            st.metric("Status", "Accepted" if lld_viewing.is_final else "Draft")

        if lld_is_locked:
            if lld_viewing.is_final:
                st.success(f"This is the Final LLD (v{lld_viewing.version}) and it is locked "
                           "against further changes.")
            else:
                st.warning(f"The Final LLD (v{lld_final.version}) is locked. "
                           "Unlock it below to make further changes.")
        elif not lld_is_current:
            st.info(f"You are viewing an older LLD version (v{lld_viewing.version}). "
                    f"Editing is only available on the current version (v{lld_latest.version}).")

        st.divider()

        lld_tab_preview, lld_tab_edit, lld_tab_refine, lld_tab_history = st.tabs(
            ["Preview", "Edit", "AI Refine", "History"]
        )

        with lld_tab_preview:
            st.markdown(lld_viewing.content)

        with lld_tab_edit:
            if not lld_editable:
                st.info("Editing is disabled for this version. "
                        + ("Unlock the Final LLD to continue." if lld_is_locked
                           else "Switch to the current version to edit."))
                st.text_area("LLD content (read-only)", value=lld_viewing.content,
                             height=500, disabled=True, key=f"lld_ro_{lld_viewing.version}")
            else:
                st.caption("Edit the LLD below. Saving creates a NEW version - "
                           "the current version is never overwritten.")
                lld_edited_text = st.text_area(
                    "LLD content (markdown)",
                    value=lld_viewing.content,
                    height=500,
                    key=f"lld_editor_{lld_viewing.version}",
                )
                lld_change_note = st.text_input(
                    "Change description (optional)",
                    placeholder="e.g. Added idempotency key to the registration endpoint",
                    key=f"lld_note_{lld_viewing.version}",
                )
                if st.button("Save as New Version", type="primary", key="lld_save_edit"):
                    try:
                        new_lld = lld_service.save_manual_edit(
                            lld_edited_text, note=lld_change_note.strip() or "Manual edit"
                        )
                        refresh_lld_versions()
                        st.session_state.lld_viewing_version = new_lld.version
                        st.success(f"Saved as LLD Version {new_lld.version}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        with lld_tab_refine:
            if not lld_editable:
                st.info("AI refinement is disabled for this version. "
                        + ("Unlock the Final LLD to continue." if lld_is_locked
                           else "Switch to the current version to refine."))
            else:
                st.caption("Describe your change in plain English. The AI receives the CURRENT "
                           "LLD plus your feedback - unaffected sections are preserved.")
                lld_feedback = st.text_area(
                    "Refinement instruction",
                    placeholder="e.g. Add a caching table for product lookups.",
                    key="lld_feedback_input",
                    height=120,
                )
                if st.button("Refine with AI", type="primary",
                             disabled=not lld_feedback.strip(), key="lld_refine_btn"):
                    with st.spinner("Sending the current LLD and your feedback to Gemini..."):
                        try:
                            start = time.time()
                            new_lld = lld_service.refine_with_ai(lld_feedback)
                            elapsed = time.time() - start
                            logger.info(f"LLD refined to v{new_lld.version} in {elapsed:.1f}s")

                            refresh_lld_versions()
                            st.session_state.lld_viewing_version = new_lld.version
                            st.success(f"Created LLD Version {new_lld.version} in {elapsed:.1f}s.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

        with lld_tab_history:
            st.caption("All LLD versions are permanent. Nothing is ever overwritten or deleted.")
            for v in reversed(lld_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                cols[2].caption(v.note or "-")
                badges = []
                if lld_latest and v.version == lld_latest.version:
                    badges.append("Current")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"lld_hist_view_{v.version}"):
                    st.session_state.lld_viewing_version = v.version
                    st.rerun()

        st.divider()

        st.subheader("Final LLD")
        lld_final_cols = st.columns([2, 2, 2])

        with lld_final_cols[0]:
            if not lld_viewing.is_final:
                if st.button(f"Choose v{lld_viewing.version} as Final LLD", key="lld_choose_final"):
                    try:
                        lld_service.choose_final_lld(lld_viewing.version)
                        refresh_lld_versions()
                        st.success(f"LLD Version {lld_viewing.version} is now the Final LLD.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))
            else:
                st.caption("This version is the Final LLD.")

        with lld_final_cols[1]:
            if lld_is_locked:
                if st.session_state.get("lld_confirm_unlock"):
                    st.warning("Unlock the Final LLD? It stays in history unchanged; "
                               "any new edit creates a new version.")
                    yes_col, no_col = st.columns(2)
                    if yes_col.button("Yes, unlock", key="lld_unlock_yes"):
                        try:
                            lld_service.unlock_final_lld()
                            st.session_state.lld_confirm_unlock = False
                            refresh_lld_versions()
                            st.success("Final LLD unlocked. Further edits will create a new version.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                    if no_col.button("Cancel", key="lld_unlock_cancel"):
                        st.session_state.lld_confirm_unlock = False
                        st.rerun()
                else:
                    if st.button("Unlock Final LLD", key="lld_unlock_btn"):
                        st.session_state.lld_confirm_unlock = True
                        st.rerun()

        with lld_final_cols[2]:
            # Export always uses the version being viewed, so what you see is what you download.
            if st.button("Prepare .docx for download", key="lld_prepare_docx"):
                with st.spinner("Formatting Word document..."):
                    try:
                        lld_docx_path = (Path(settings.resolved_output_dir())
                                         / st.session_state.project_id
                                         / "lld"
                                         / f"LLD_v{lld_viewing.version}.docx")
                        generate_lld_docx(lld_viewing.content, lld_docx_path)
                        st.session_state.lld_docx_ready_path = str(lld_docx_path)
                        st.session_state.lld_docx_ready_version = lld_viewing.version
                        logger.info(f"LLD DOCX exported for v{lld_viewing.version}")
                    except Exception as exc:
                        st.error(friendly_error(exc))

            lld_ready_path = st.session_state.get("lld_docx_ready_path")
            lld_ready_version = st.session_state.get("lld_docx_ready_version")
            if (lld_ready_path and Path(lld_ready_path).exists()
                    and lld_ready_version == lld_viewing.version):
                try:
                    with open(lld_ready_path, "rb") as f:
                        st.download_button(
                            f"Download LLD v{lld_viewing.version}.docx",
                            data=f.read(),
                            file_name=f"LLD_v{lld_viewing.version}.docx",
                            mime=("application/vnd.openxmlformats-officedocument"
                                  ".wordprocessingml.document"),
                            key="lld_download_btn",
                        )
                except Exception as exc:
                    st.error(friendly_error(exc))


# --- STEP 6: User Story Refinement workspace (User Story Refinement Agent) ----------------
#
# Standalone stage. Reconciles the LATEST user-story version against the accepted BRD
# (primary) plus the accepted HLD / LLD (optional context). It writes a NEW version into
# the SAME `user_stories` stream via UserStoryRefinementService.refine() - no second store.

with tab_usr:
    st.caption("Phase 5: reconcile the current user stories against BRD (primary) + "
               "HLD / LLD (optional context). Produces a new version in the same "
               "user-story stream.")

    if final_version is None:
        st.warning("User Story Refinement is unavailable: no accepted BRD.")
        st.caption("Accept a BRD in the BRD Workspace first. The accepted BRD is the "
                   "primary business source for refinement.")

    elif us_latest is None:
        st.warning("User Story Refinement is unavailable: no user stories exist yet.")
        st.caption("Generate the initial user stories in Step 4 first. Refinement always "
                   "starts from the latest existing user-story version.")

    else:
        usr_viewing_number = st.session_state.get("usr_viewing_version", us_latest.version)
        try:
            usr_viewing = us_service.get_version(usr_viewing_number) or us_latest
        except Exception as exc:
            st.error(friendly_error(exc))
            usr_viewing = us_latest

        # --- 1. Source Artifacts -------------------------------------------------
        st.subheader("Source Artifacts")
        src_cols = st.columns(4)
        src_cols[0].metric("BRD", f"v{final_version.version}", "Accepted (required)")
        src_cols[1].metric(
            "HLD",
            f"v{hld_final.version}" if hld_final is not None else "—",
            "Accepted (context)" if hld_final is not None else "none — optional",
        )
        src_cols[2].metric(
            "LLD",
            f"v{lld_final.version}" if lld_final is not None else "—",
            "Accepted (context)" if lld_final is not None else "none — optional",
        )
        us_state = ("Locked" if us_is_locked else
                    ("Accepted" if (us_final and us_final.version == us_latest.version) else "Draft"))
        src_cols[3].metric("User Stories", f"v{us_latest.version}", us_state)
        st.caption("HLD and LLD are optional context — refinement proceeds without them "
                   "(the agent receives a sentinel). Only accepted/final versions are used.")

        # --- 4/5. Refinement result + provenance + staleness -------------------
        if usr_is_refined and usr_recorded is not None:
            prov = (f"BRD v{usr_recorded['brd']}, "
                    f"HLD v{usr_recorded['hld'] if usr_recorded['hld'] is not None else '—'}, "
                    f"LLD v{usr_recorded['lld'] if usr_recorded['lld'] is not None else '—'}, "
                    f"source stories v{usr_recorded['us']}")
            st.success(f"Latest version **v{us_latest.version} — Artifact Refinement**. "
                       f"Built from: {prov}.")
            if us_latest.note:
                st.caption(f"Note: {us_latest.note}")

            if usr_stale:
                st.warning(
                    f"These refined stories may be **stale**: the following changed since "
                    f"the refinement — **{', '.join(usr_stale_sources)}**. Nothing is "
                    "regenerated automatically; click **Refine Again** to reconcile against "
                    "the current artifacts (creates a new version; this one stays in History)."
                )
        else:
            st.info("The latest user-story version has not been refined from artifacts yet.")

        # --- 3. Artifact Refinement action -----------------------------------
        st.divider()
        st.subheader("Artifact Refinement")
        if us_is_locked:
            st.info("The Final User Stories are locked. Unlock them in Step 4 "
                    "(User Story Workspace) before refining.")
        st.caption(f"Refinement starts from the current latest version "
                   f"(v{us_latest.version}), whatever its origin (initial, manual edit, or a "
                   "previous refinement). Existing US-NNN IDs and unaffected stories are "
                   "preserved; only evidence-based changes are made.")
        refine_label = "Refine Again" if usr_is_refined else "Refine Stories from Artifacts"
        if st.button(refine_label, type="primary", disabled=us_is_locked,
                     key="usr_refine_btn"):
            with st.spinner("Reconciling the user stories against BRD / HLD / LLD..."):
                try:
                    start = time.time()
                    new_us = usr_service.refine()
                    elapsed = time.time() - start
                    logger.info(f"User stories refined from artifacts to v{new_us.version} "
                                f"in {elapsed:.1f}s")
                    refresh_us_versions()
                    st.session_state.usr_viewing_version = new_us.version
                    st.session_state.us_viewing_version = new_us.version
                    st.success(f"Created User Stories Version {new_us.version} "
                               f"(Artifact Refinement) in {elapsed:.1f}s.")
                    st.rerun()
                except Exception as exc:
                    st.error(friendly_error(exc))

        # --- 2. Current User Stories: Preview + History --------------------
        st.divider()
        st.subheader("User Stories")
        usr_tab_preview, usr_tab_history = st.tabs(["Preview", "History"])

        with usr_tab_preview:
            if usr_viewing.version != us_latest.version:
                st.info(f"Viewing v{usr_viewing.version}. The latest version is "
                        f"v{us_latest.version}.")
            st.markdown(usr_viewing.content)

        with usr_tab_history:
            st.caption("The whole user-story history — initial generation, manual edits, "
                       "freeform AI refinement, and artifact refinement all share this stream.")
            for v in reversed(us_versions):
                cols = st.columns([1, 2, 3, 2, 1])
                cols[0].markdown(f"**v{v.version}**")
                cols[1].markdown(story_version_label(v))
                cols[2].caption(v.note or "-")
                badges = []
                if v.version == us_latest.version:
                    badges.append("Current")
                    if usr_stale:
                        badges.append("STALE")
                if v.is_final:
                    badges.append("Accepted")
                    if v.is_locked:
                        badges.append("Locked")
                cols[3].caption(" / ".join(badges) if badges else "Draft")
                if cols[4].button("View", key=f"usr_hist_view_{v.version}"):
                    st.session_state.usr_viewing_version = v.version
                    st.rerun()


# --- STEP 7: QA / Test Case workspace (QA / Test Case Agent) -----------------------------
#
# Standalone stage. Generates test cases from the accepted BRD (required) plus the
# accepted HLD / LLD / User Stories (optional context). Every generate / regenerate /
# manual edit / AI refine appends a NEW version to the OWN test_cases stream
# (outputs/<pid>/test_cases/versions.json) via TestCaseService - no other stream is
# written and no second copy of any artifact is created.

with tab_qa:
    st.caption("Phase 6: generate QA test cases from the accepted BRD (required) plus "
               "the accepted HLD / LLD / User Stories (optional context). Own version "
               "stream; nothing is regenerated automatically.")

    if final_version is None:
        st.warning("QA / Test Case generation is unavailable: no accepted BRD.")
        st.caption("Accept a BRD in the BRD Workspace first. The accepted BRD is the "
                   "required source of truth for test cases.")

    else:
        qa_viewing_number = st.session_state.get(
            "qa_viewing_version", qa_latest.version if qa_latest else 0
        )
        qa_viewing = None
        if qa_latest is not None:
            try:
                qa_viewing = qa_service.get_version(qa_viewing_number) or qa_latest
            except Exception as exc:
                st.error(friendly_error(exc))
                qa_viewing = qa_latest

        qa_is_current = qa_viewing is not None and qa_viewing.version == qa_latest.version
        qa_editable = qa_is_current and not qa_is_locked

        # --- 1. Source Artifacts ------------------------------------------------
        st.subheader("Source Artifacts")
        qa_src_cols = st.columns(4)
        qa_src_cols[0].metric("BRD", f"v{final_version.version}", "Accepted / Required")
        qa_src_cols[1].metric(
            "HLD",
            f"v{hld_final.version}" if hld_final is not None else "—",
            "Accepted / Context" if hld_final is not None else "none / optional",
        )
        qa_src_cols[2].metric(
            "LLD",
            f"v{lld_final.version}" if lld_final is not None else "—",
            "Accepted / Context" if lld_final is not None else "none / optional",
        )
        if us_final is not None:
            us_label, us_state = f"v{us_final.version}", "Final / Context"
        elif us_latest is not None:
            us_label, us_state = f"v{us_latest.version}", "Latest / Context"
        else:
            us_label, us_state = "—", "none / optional"
        qa_src_cols[3].metric("User Stories", us_label, us_state)
        st.caption("HLD, LLD and User Stories are optional context - their absence never "
                   "blocks BRD-based generation. Only accepted/final versions are used "
                   "(User Stories fall back to the latest available).")

        if qa_latest is None:
            # --- 3. First generation ----------------------------------------
            st.divider()
            st.subheader("Generate Test Cases")
            st.caption(f"Test cases will be generated from the Accepted BRD "
                       f"(v{final_version.version}) and whatever optional context is "
                       "available. This creates Test Cases Version 1.")
            if st.button("Generate Test Cases", type="primary", key="qa_generate_btn"):
                with st.spinner("Sending the artifacts to Gemini and drafting test cases..."):
                    try:
                        start = time.time()
                        qa_v = qa_service.generate()
                        elapsed = time.time() - start
                        logger.info(f"Test cases v{qa_v.version} generated in {elapsed:.1f}s")
                        refresh_qa_versions()
                        st.session_state.qa_viewing_version = qa_v.version
                        st.success(f"Test Cases Version {qa_v.version} generated in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

        else:
            # --- 5. Stale-source warning (non-blocking; no auto-regeneration) ---
            if qa_stale and qa_recorded is not None:
                parts = []
                for label, key in (("BRD", "brd"), ("HLD", "hld"), ("LLD", "lld"),
                                   ("User Stories", "us")):
                    if label in qa_stale_sources:
                        rec = qa_recorded.get(key)
                        cur = qa_current.get(key)
                        rec_txt = f"v{rec}" if rec is not None else "unavailable"
                        cur_txt = f"v{cur}" if cur is not None else "unavailable"
                        parts.append(f"{label} {rec_txt} -> {cur_txt}")
                st.warning(
                    "These test cases may be **stale** - a source artifact changed since "
                    f"they were generated: **{'; '.join(parts)}**. Nothing is regenerated "
                    "automatically. Use **Regenerate from Artifacts** or **AI Refine** to "
                    "rebuild against the current artifacts (creates a new version; this "
                    "one stays in History)."
                )

            # --- 4. Provenance of the current version ---
            if qa_recorded is not None:
                st.info(f"Current version **v{qa_latest.version}** built from: {qa_latest.note}")

            # --- status banner ---
            qa_status_cols = st.columns([2, 2, 2])
            with qa_status_cols[0]:
                st.metric("Viewing", f"v{qa_viewing.version}")
            with qa_status_cols[1]:
                st.metric("Type", SOURCE_LABELS.get(qa_viewing.source, qa_viewing.source))
            with qa_status_cols[2]:
                st.metric("Status", "Accepted" if qa_viewing.is_final else "Draft")

            if qa_is_locked:
                if qa_viewing.is_final:
                    st.success(f"This is the Final Test Cases set (v{qa_viewing.version}) "
                               "and it is locked against further changes.")
                else:
                    st.warning(f"The Final Test Cases (v{qa_final.version}) are locked. "
                               "Unlock them below to make further changes.")
            elif not qa_is_current:
                st.info(f"You are viewing an older test-case version (v{qa_viewing.version}). "
                        f"Editing is only available on the current version "
                        f"(v{qa_latest.version}).")

            st.divider()

            qa_tab_preview, qa_tab_edit, qa_tab_refine, qa_tab_history = st.tabs(
                ["Preview", "Edit", "AI Refine", "History"]
            )

            with qa_tab_preview:
                st.markdown(qa_viewing.content)

            with qa_tab_edit:
                if not qa_editable:
                    st.info("Editing is disabled for this version. "
                            + ("Unlock the Final Test Cases to continue." if qa_is_locked
                               else "Switch to the current version to edit."))
                    st.text_area("Test cases (read-only)", value=qa_viewing.content,
                                 height=500, disabled=True, key=f"qa_ro_{qa_viewing.version}")
                else:
                    st.caption("Edit the test cases below (Markdown). Saving creates a NEW "
                               "version - the current version is never overwritten. Keep "
                               "the '## TC-NNN' headings.")
                    qa_edited = st.text_area(
                        "Test cases (markdown)",
                        value=qa_viewing.content,
                        height=500,
                        key=f"qa_editor_{qa_viewing.version}",
                    )
                    qa_note = st.text_input(
                        "Change description (optional)",
                        placeholder="e.g. Corrected TC-004 expected result",
                        key=f"qa_note_{qa_viewing.version}",
                    )
                    if st.button("Save as New Version", type="primary", key="qa_save_edit"):
                        try:
                            new_qa = qa_service.save_manual_edit(
                                qa_edited, note=qa_note.strip() or "Manual edit"
                            )
                            refresh_qa_versions()
                            st.session_state.qa_viewing_version = new_qa.version
                            st.success(f"Saved as Test Cases Version {new_qa.version}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))

            with qa_tab_refine:
                if not qa_editable:
                    st.info("AI refinement is disabled for this version. "
                            + ("Unlock the Final Test Cases to continue." if qa_is_locked
                               else "Switch to the current version to refine."))
                else:
                    st.caption("Describe your change in plain English. The AI receives the "
                               "CURRENT test cases plus the artifacts - unaffected cases and "
                               "their TC-NNN ids are preserved.")
                    qa_feedback = st.text_area(
                        "Refinement instruction",
                        placeholder="e.g. Add boundary cases for the password length rule.",
                        key="qa_feedback_input",
                        height=120,
                    )
                    if st.button("Refine with AI", type="primary",
                                 disabled=not qa_feedback.strip(), key="qa_refine_btn"):
                        with st.spinner("Sending the current test cases and your feedback "
                                        "to Gemini..."):
                            try:
                                start = time.time()
                                new_qa = qa_service.refine_with_ai(qa_feedback)
                                elapsed = time.time() - start
                                logger.info(f"Test cases refined to v{new_qa.version} in "
                                            f"{elapsed:.1f}s")
                                refresh_qa_versions()
                                st.session_state.qa_viewing_version = new_qa.version
                                st.success(f"Created Test Cases Version {new_qa.version} in "
                                           f"{elapsed:.1f}s.")
                                st.rerun()
                            except Exception as exc:
                                st.error(friendly_error(exc))

            with qa_tab_history:
                st.caption("All test-case versions are permanent. "
                           "Nothing is ever overwritten or deleted.")
                for v in reversed(qa_versions):
                    cols = st.columns([1, 2, 3, 2, 1])
                    cols[0].markdown(f"**v{v.version}**")
                    cols[1].markdown(SOURCE_LABELS.get(v.source, v.source))
                    cols[2].caption(v.note or "-")
                    badges = []
                    if v.version == qa_latest.version:
                        badges.append("Current")
                        if qa_stale:
                            badges.append("STALE")
                    if v.is_final:
                        badges.append("Accepted")
                        if v.is_locked:
                            badges.append("Locked")
                    cols[3].caption(" / ".join(badges) if badges else "Draft")
                    if cols[4].button("View", key=f"qa_hist_view_{v.version}"):
                        st.session_state.qa_viewing_version = v.version
                        st.rerun()

            st.divider()

            st.subheader("Regenerate")
            st.caption("Rebuild the test cases from scratch against the CURRENT artifacts "
                       "(ignores the current test-case content). Creates a new version.")
            if st.button("Regenerate from Artifacts", disabled=qa_is_locked,
                         key="qa_regen_btn"):
                with st.spinner("Rebuilding test cases from the current artifacts..."):
                    try:
                        start = time.time()
                        new_qa = qa_service.regenerate()
                        elapsed = time.time() - start
                        logger.info(f"Test cases regenerated to v{new_qa.version} in "
                                    f"{elapsed:.1f}s")
                        refresh_qa_versions()
                        st.session_state.qa_viewing_version = new_qa.version
                        st.success(f"Created Test Cases Version {new_qa.version} in "
                                   f"{elapsed:.1f}s.")
                        st.rerun()
                    except Exception as exc:
                        st.error(friendly_error(exc))

            st.divider()

            st.subheader("Final Test Cases")
            qa_final_cols = st.columns([2, 2, 2])

            with qa_final_cols[0]:
                if not qa_viewing.is_final:
                    if st.button(f"Choose v{qa_viewing.version} as Final Test Cases",
                                 key="qa_choose_final"):
                        try:
                            qa_service.choose_final(qa_viewing.version)
                            refresh_qa_versions()
                            st.success(f"Test Cases Version {qa_viewing.version} is now Final.")
                            st.rerun()
                        except Exception as exc:
                            st.error(friendly_error(exc))
                else:
                    st.caption("This version is the Final Test Cases set.")

            with qa_final_cols[1]:
                if qa_is_locked:
                    if st.session_state.get("qa_confirm_unlock"):
                        st.warning("Unlock the Final Test Cases? They stay in history "
                                   "unchanged; any new edit creates a new version.")
                        yes_col, no_col = st.columns(2)
                        if yes_col.button("Yes, unlock", key="qa_unlock_yes"):
                            try:
                                qa_service.unlock_final()
                                st.session_state.qa_confirm_unlock = False
                                refresh_qa_versions()
                                st.success("Final Test Cases unlocked. Further edits will "
                                           "create a new version.")
                                st.rerun()
                            except Exception as exc:
                                st.error(friendly_error(exc))
                        if no_col.button("Cancel", key="qa_unlock_cancel"):
                            st.session_state.qa_confirm_unlock = False
                            st.rerun()
                    else:
                        if st.button("Unlock Final Test Cases", key="qa_unlock_btn"):
                            st.session_state.qa_confirm_unlock = True
                            st.rerun()

            with qa_final_cols[2]:
                if st.button("Prepare .docx for download", key="qa_prepare_docx"):
                    with st.spinner("Formatting Word document..."):
                        try:
                            qa_docx_path = (Path(settings.resolved_output_dir())
                                            / st.session_state.project_id
                                            / "test_cases"
                                            / f"TestCases_v{qa_viewing.version}.docx")
                            generate_test_cases_docx(qa_viewing.content, qa_docx_path)
                            st.session_state.qa_docx_ready_path = str(qa_docx_path)
                            st.session_state.qa_docx_ready_version = qa_viewing.version
                            logger.info(f"Test cases DOCX exported for v{qa_viewing.version}")
                        except Exception as exc:
                            st.error(friendly_error(exc))

                qa_ready_path = st.session_state.get("qa_docx_ready_path")
                qa_ready_version = st.session_state.get("qa_docx_ready_version")
                if (qa_ready_path and Path(qa_ready_path).exists()
                        and qa_ready_version == qa_viewing.version):
                    try:
                        with open(qa_ready_path, "rb") as f:
                            st.download_button(
                                f"Download TestCases v{qa_viewing.version}.docx",
                                data=f.read(),
                                file_name=f"TestCases_v{qa_viewing.version}.docx",
                                mime=("application/vnd.openxmlformats-officedocument"
                                      ".wordprocessingml.document"),
                                key="qa_download_btn",
                            )
                    except Exception as exc:
                        st.error(friendly_error(exc))
