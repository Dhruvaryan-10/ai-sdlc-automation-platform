"""
Phase 8B-6 — SDLC Pipeline panel added to the existing Streamlit UI.

Deterministic tests only: pure formatting-helper unit tests, monkeypatch-spy
tests on the small `run_pipeline_step()` handler, and static (`ast`-based)
structural checks on `app/ui/streamlit_app.py`'s own source. No Gemini calls,
no network, no real Streamlit browser/session — consistent with the existing
`tests/test_streamlit_render.py` style (import the module once under the
autouse `isolated_output_dir` fixture + dummy `GOOGLE_API_KEY`, then test the
pure pieces extracted from it).
"""

import ast
import inspect
from pathlib import Path

import app.ui.streamlit_app as streamlit_app
from app.ui.streamlit_app import (
    _awaiting_approval_message,
    _next_step_label,
    _pipeline_steps,
    _pipeline_summary,
    _render_step_rail,
    run_pipeline_step,
)

_SOURCE_PATH = Path(inspect.getfile(streamlit_app))
_SOURCE_TEXT = _SOURCE_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE_TEXT)


def _empty_status(**overrides) -> dict:
    """A fully-populated, all-empty sdlc_status()-shaped dict (real field set)."""
    base = {
        "project_id": "p",
        "brd_exists": False, "brd_latest_version": None, "brd_final_version": None,
        "awaiting_brd_approval": False,
        "hld_exists": False, "hld_latest_version": None, "hld_final_version": None,
        "awaiting_hld_approval": False,
        "us_exists": False, "us_latest_version": None,
        "lld_exists": False, "lld_latest_version": None, "lld_final_version": None,
        "awaiting_lld_approval": False,
        "tc_exists": False, "tc_latest_version": None, "tc_final_version": None,
        "awaiting_test_cases_approval": False,
        "closure_exists": False, "closure_latest_version": None,
        "closure_final_version": None, "awaiting_closure_approval": False,
        "closure_report_stale": False, "closure_report_stale_sources": [],
        "next_step": "generate_brd",
    }
    base.update(overrides)
    return base


# --- A. existing module still imports successfully --------------------------

def test_streamlit_app_module_still_imports_and_exposes_the_new_helpers():
    assert hasattr(streamlit_app, "_next_step_label")
    assert hasattr(streamlit_app, "_awaiting_approval_message")
    assert hasattr(streamlit_app, "_pipeline_summary")
    assert hasattr(streamlit_app, "run_pipeline_step")
    assert hasattr(streamlit_app, "sdlc_status")
    assert hasattr(streamlit_app, "run_step")


# --- B. _next_step_label() maps every current next_step value ---------------

def test_next_step_label_covers_every_known_value():
    expected = {
        "generate_brd": "Next: Generate the BRD",
        "approve_brd": "Next: Review and approve the BRD in Step 2",
        "generate_hld": "Next: Generate the HLD",
        "approve_hld": "Next: Review and approve the HLD in Step 3",
        "generate_lld": "Next: Generate the LLD",
        "approve_lld": "Next: Review and approve the LLD in Step 5",
        "generate_test_cases": "Next: Generate Test Cases in Step 7",
        "approve_test_cases": "Next: Review and approve Test Cases in Step 7",
        "generate_closure_report": "Next: Generate the Closure Report in Step 9",
        "approve_closure_report": "Next: Review and approve the Closure Report in Step 9",
        "review_closure_report": (
            "Next: Review the Closure Report in Step 9 — its evidence changed since it "
            "was finalized"
        ),
        None: "SDLC pipeline complete — no further orchestrated action is required.",
    }
    for next_step, label in expected.items():
        assert _next_step_label(_empty_status(next_step=next_step)) == label


def test_next_step_label_falls_back_for_an_unrecognized_value():
    # Never invents a new status value / never raises on an unexpected one.
    assert _next_step_label(_empty_status(next_step="something_new")) == "Status unavailable."


def test_awaiting_approval_message_only_set_for_approve_states():
    assert _awaiting_approval_message(_empty_status(next_step="generate_brd")) is None
    assert _awaiting_approval_message(_empty_status(next_step=None)) is None
    # a stale FINAL closure report routes to review, but has its OWN dedicated
    # staleness banner — it must not also raise the amber "awaiting approval" one.
    assert _awaiting_approval_message(_empty_status(next_step="review_closure_report")) is None
    assert "BRD approval" in _awaiting_approval_message(_empty_status(next_step="approve_brd"))
    assert "HLD approval" in _awaiting_approval_message(_empty_status(next_step="approve_hld"))
    assert "LLD approval" in _awaiting_approval_message(_empty_status(next_step="approve_lld"))
    assert "Test Case approval" in _awaiting_approval_message(_empty_status(next_step="approve_test_cases"))
    assert "Closure Report approval" in _awaiting_approval_message(
        _empty_status(next_step="approve_closure_report")
    )


# --- C/D/E. _pipeline_summary() for fresh / approval-gate / completed states --

def test_pipeline_summary_handles_a_fresh_project():
    lines = _pipeline_summary(_empty_status())
    assert lines == [
        "BRD: not generated",
        "HLD: not generated",
        "User Stories: not generated",
        "LLD: not generated",
        "Test Cases: not generated",
        "Closure Report: not generated",
    ]


def test_pipeline_summary_handles_an_approval_gate_state():
    status = _empty_status(
        brd_exists=True, brd_latest_version=1, brd_final_version=1,
        hld_exists=True, hld_latest_version=1, hld_final_version=None,
        awaiting_hld_approval=True,
        us_exists=True, us_latest_version=1,
        next_step="approve_hld",
    )
    lines = _pipeline_summary(status)
    assert lines[0] == "BRD: v1 (final: v1)"
    assert lines[1] == "HLD: v1 (awaiting approval)"
    assert lines[2] == "User Stories: v1"          # no final-version concept, by design
    assert lines[3] == "LLD: not generated"
    assert lines[4] == "Test Cases: not generated"


def test_pipeline_summary_handles_a_completed_pipeline():
    status = _empty_status(
        brd_exists=True, brd_latest_version=1, brd_final_version=1,
        hld_exists=True, hld_latest_version=1, hld_final_version=1,
        us_exists=True, us_latest_version=1,
        lld_exists=True, lld_latest_version=1, lld_final_version=1,
        tc_exists=True, tc_latest_version=1, tc_final_version=1,
        closure_exists=True, closure_latest_version=1, closure_final_version=1,
        next_step=None,
    )
    lines = _pipeline_summary(status)
    assert lines == [
        "BRD: v1 (final: v1)",
        "HLD: v1 (final: v1)",
        "User Stories: v1",
        "LLD: v1 (final: v1)",
        "Test Cases: v1 (final: v1)",
        "Closure Report: v1 (final: v1)",
    ]
    assert _next_step_label(status) == (
        "SDLC pipeline complete — no further orchestrated action is required."
    )


def test_pipeline_summary_flags_stale_closure_evidence():
    status = _empty_status(
        brd_exists=True, brd_latest_version=2, brd_final_version=2,
        closure_exists=True, closure_latest_version=1, closure_final_version=1,
        closure_report_stale=True, closure_report_stale_sources=["BRD"],
        next_step=None,
    )
    line = _pipeline_summary(status)[5]
    assert line.startswith("Closure Report: v1 (final: v1)")
    assert "evidence may be stale" in line


def test_pipeline_summary_never_exposes_a_us_final_version_key():
    # sdlc_status() intentionally has no us_final_version - the panel must not
    # invent one, or crash trying to read one.
    status = _empty_status(us_exists=True, us_latest_version=3)
    assert "us_final_version" not in status
    assert _pipeline_summary(status)[2] == "User Stories: v3"


# --- F/G. the handler calls run_step() with the right project + services ----

def test_run_pipeline_step_calls_run_step_with_correct_project_id(monkeypatch):
    captured = {}

    def _fake_run_step(project_id, **kwargs):
        captured["project_id"] = project_id
        captured["kwargs"] = kwargs
        return "final-state-sentinel"

    monkeypatch.setattr(streamlit_app, "run_step", _fake_run_step)

    ba, sa, us, lld, tc = object(), object(), object(), object(), object()
    result = run_pipeline_step("proj-xyz", ba, sa, us, lld, tc)

    assert captured["project_id"] == "proj-xyz"
    assert result == "final-state-sentinel"


def test_run_pipeline_step_passes_the_existing_session_service_instances(monkeypatch):
    """The handler must forward the SAME service objects it was given - never
    construct fresh replacements."""
    captured = {}
    monkeypatch.setattr(
        streamlit_app, "run_step",
        lambda project_id, **kwargs: captured.update(kwargs) or None,
    )

    ba, sa, us, lld, tc = object(), object(), object(), object(), object()
    run_pipeline_step("proj-xyz", ba, sa, us, lld, tc)

    assert captured["ba_service"] is ba
    assert captured["sa_service"] is sa
    assert captured["us_service"] is us
    assert captured["lld_service"] is lld
    assert captured["tc_service"] is tc
    assert captured["request"] == "ensure_brd"


# --- H. the handler never calls any finalization method ---------------------

def test_run_pipeline_step_source_contains_no_finalization_call():
    source = inspect.getsource(run_pipeline_step)
    for forbidden in (
        "choose_final_brd", "choose_final_hld", "choose_final_stories",
        "choose_final_lld", "choose_final(", "mark_final", "unlock_final",
    ):
        assert forbidden not in source, f"found forbidden call: {forbidden}"


def test_pipeline_panel_block_contains_no_finalization_call():
    """Static check on the actual panel block inside streamlit_app.py (not just
    the helper function) - the whole SDLC Pipeline section between its header
    comment and the following st.divider() must never finalize anything.

    Comment/docstring lines are excluded (they legitimately document the
    invariant, e.g. "Never calls choose_final_* / mark_final / unlock_final*") -
    only executable-code lines are checked, per the instruction that matches in
    comments/docstrings are fine.
    """
    start = _SOURCE_TEXT.index("# --- SDLC Pipeline panel (Phase 8B-6)")
    end = _SOURCE_TEXT.index("st.divider()", start)
    panel_block = _SOURCE_TEXT[start:end]
    code_lines = "\n".join(
        line for line in panel_block.splitlines() if not line.strip().startswith("#")
    )
    for forbidden in (
        "choose_final_brd", "choose_final_hld", "choose_final_stories",
        "choose_final_lld", "choose_final(", "mark_final", "unlock_final",
    ):
        assert forbidden not in code_lines, f"found forbidden call in panel: {forbidden}"


# --- run_step is reachable ONLY through the explicit button click -----------

def _find_calls(tree: ast.AST, func_name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == func_name
    ]


def test_run_step_is_called_exactly_once_and_only_inside_run_pipeline_step():
    calls = _find_calls(_TREE, "run_step")
    assert len(calls) == 1

    target = None
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "run_pipeline_step":
            target = node
            break
    assert target is not None
    assert target.lineno <= calls[0].lineno <= (target.end_lineno or calls[0].lineno)


def test_run_pipeline_step_is_only_invoked_inside_the_explicit_button_click():
    """The ONE call to `run_pipeline_step(...)` (excluding its own `def`) must be
    lexically nested inside an `if st.button("Run SDLC Pipeline", ...):` block -
    i.e. it can never execute on import, page load, or a plain rerun."""
    calls = _find_calls(_TREE, "run_pipeline_step")
    assert len(calls) == 1  # the one real call site; the `def` is not a Call node

    call_node = calls[0]
    enclosing_if = None
    for node in ast.walk(_TREE):
        if isinstance(node, ast.If) and _is_button_test(node.test, "Run SDLC Pipeline"):
            if node.lineno <= call_node.lineno <= (node.end_lineno or call_node.lineno):
                enclosing_if = node
                break
    assert enclosing_if is not None, "run_pipeline_step(...) is not guarded by the button click"


def _is_button_test(test_node: ast.AST, expected_label: str) -> bool:
    """True if `test_node` is (roughly) `st.button("expected_label", ...)`."""
    if not isinstance(test_node, ast.Call):
        return False
    func = test_node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "button"):
        return False
    for arg in test_node.args:
        if isinstance(arg, ast.Constant) and arg.value == expected_label:
            return True
    return False


# --- J. project isolation: no hard-coded project id, no global state --------

def test_pipeline_panel_uses_the_session_project_id_not_a_literal():
    start = _SOURCE_TEXT.index("# --- SDLC Pipeline panel (Phase 8B-6)")
    end = _SOURCE_TEXT.index("st.divider()", start)
    panel_block = _SOURCE_TEXT[start:end]
    assert "st.session_state.project_id" in panel_block
    # sdlc_status's first positional argument must be the session's project id,
    # not a hard-coded string.
    assert 'sdlc_status(\n            st.session_state.project_id' in panel_block


# --- I. Step 6 / refinement behavior is untouched ---------------------------

def test_step6_still_calls_refine_directly_and_panel_never_imports_refine_step():
    assert "usr_service.refine()" in _SOURCE_TEXT
    assert _SOURCE_TEXT.count("usr_service.refine()") == 1
    assert "refine_user_stories_step" not in _SOURCE_TEXT


# --- pipeline summary is a pure function: no `st.` calls inside it ----------

def test_pure_helpers_contain_no_streamlit_calls():
    for fn in (_next_step_label, _awaiting_approval_message, _pipeline_summary,
               _pipeline_steps, _render_step_rail):
        assert "st." not in inspect.getsource(fn)


# --- SDLC step rail (all 9 steps, state derived from sdlc_status()) ---------

def test_pipeline_steps_returns_all_nine_steps_in_order():
    steps = _pipeline_steps(_empty_status(next_step="generate_brd"))
    assert [s["n"] for s in steps] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [s["name"] for s in steps] == [
        "SOW → BRD", "BRD Workspace", "HLD Workspace", "User Story Workspace",
        "LLD Workspace", "User Story Refinement", "QA / Test Case Workspace",
        "Traceability & Quality", "Closure Report",
    ]


def test_rail_step_names_match_the_step_tab_labels():
    # The nine rail steps line up 1:1 with the nine "Step N: <label>" tabs.
    assert len(streamlit_app._RAIL_STEPS) == 9
    for i in range(1, 10):
        assert f'"Step {i}: ' in _SOURCE_TEXT, f"missing Step {i} tab label"
    assert '"Step 6: User Story Refinement"' in _SOURCE_TEXT
    assert '"Step 7: QA / Test Case Workspace"' in _SOURCE_TEXT
    assert '"Step 8: Traceability & Quality"' in _SOURCE_TEXT
    assert '"Step 9: Closure Report"' in _SOURCE_TEXT
    # the old 8-step closure label must be gone
    assert '"Step 8: Closure Report"' not in _SOURCE_TEXT


def test_pipeline_steps_fresh_project_marks_step_one_current():
    steps = _pipeline_steps(_empty_status(next_step="generate_brd"))
    assert steps[0]["state"] == "current"
    assert all(s["state"] == "todo" for s in steps[1:])


def test_pipeline_steps_derives_done_current_readonly_from_status():
    # BRD..Test Cases final, User Stories generated, closure drafted + awaiting.
    status = _empty_status(
        brd_exists=True, brd_final_version=1,
        hld_exists=True, hld_final_version=1,
        us_exists=True, us_latest_version=2,
        lld_exists=True, lld_final_version=1,
        tc_exists=True, tc_final_version=2,
        closure_exists=True, closure_final_version=None,
        awaiting_closure_approval=True, next_step="approve_closure_report",
    )
    steps = _pipeline_steps(status)
    states = [s["state"] for s in steps]
    #        1       2       3       4       5       6(refine)   7       8(t&q)     9(closure)
    assert states == ["done", "done", "done", "done", "done", "readonly",
                      "done", "readonly", "current"]
    assert steps[8]["status"] == "Current"        # step 9, current (approve)


def test_pipeline_steps_stale_closure_is_current_not_done():
    status = _empty_status(
        brd_exists=True, brd_final_version=2,
        hld_exists=True, hld_final_version=1,
        us_exists=True, us_latest_version=1,
        lld_exists=True, lld_final_version=1,
        tc_exists=True, tc_final_version=1,
        closure_exists=True, closure_final_version=1,
        closure_report_stale=True, closure_report_stale_sources=["BRD"],
        next_step=None,
    )
    step9 = _pipeline_steps(status)[8]
    assert step9["n"] == 9
    assert step9["state"] == "current"            # NOT "done" while stale
    assert "stale" in step9["status"].lower()


def test_pipeline_steps_all_final_marks_every_step_done_or_readonly():
    status = _empty_status(
        brd_exists=True, brd_final_version=1,
        hld_exists=True, hld_final_version=1,
        us_exists=True, us_latest_version=1,
        lld_exists=True, lld_final_version=1,
        tc_exists=True, tc_final_version=1,
        closure_exists=True, closure_final_version=1,
        next_step=None,
    )
    states = [s["state"] for s in _pipeline_steps(status)]
    #        1..5 done, 6 readonly (US refinement optional), 7 done, 8 readonly, 9 done
    assert states == ["done"] * 5 + ["readonly", "done", "readonly", "done"]


def test_pipeline_steps_traceability_step_is_todo_before_any_brd():
    # Step 8 (index 7) = Traceability & Quality — read-only once a BRD exists,
    # "todo" before that.
    assert _pipeline_steps(_empty_status(next_step="generate_brd"))[7]["state"] == "todo"


def test_render_step_rail_is_scoped_html_with_all_nine_steps():
    html = _render_step_rail(_empty_status(next_step="generate_brd"))
    assert html.startswith('<div class="sdlc-steprail-wrap">')
    assert 'class="sdlc-steprail"' in html
    assert html.count('class="sdlc-step ') == 9            # one card per step
    for _n, name in streamlit_app._RAIL_STEPS:
        assert name in html
    assert "<script" not in html.lower()


def test_step_rail_css_is_fully_scoped_and_has_no_script():
    css = streamlit_app._STEP_RAIL_CSS
    assert "<script" not in css.lower()
    for line in css.splitlines():
        line = line.strip()
        if line.endswith("{"):
            assert "sdlc-steprail-wrap" in line, f"unscoped CSS rule: {line}"


def test_pipeline_panel_renders_the_step_rail_not_the_old_caption_columns():
    start = _SOURCE_TEXT.index("# --- SDLC Pipeline panel (Phase 8B-6)")
    end = _SOURCE_TEXT.index("st.divider()", start)
    panel_block = _SOURCE_TEXT[start:end]
    assert "_render_step_rail(pipeline_status)" in panel_block
    assert "_STEP_RAIL_CSS" in panel_block
    assert "st.columns(5)" not in panel_block  # old truncating caption row is gone


# ============================================================
# Phase 10B — Step 8 Traceability & Quality workspace (read-only)
# ============================================================

def _traceability_block() -> str:
    start = _SOURCE_TEXT.index("# --- STEP 8: Traceability & Quality (READ-ONLY)")
    end = _SOURCE_TEXT.index("# --- STEP 9: Closure Report", start)
    return _SOURCE_TEXT[start:end]


def test_step8_traceability_tab_exists_and_is_wired_to_the_existing_reports():
    block = _traceability_block()
    assert "with tab_traceability:" in block
    # uses the EXISTING deterministic functions, not a re-implementation
    assert "build_project_quality_report_for_project(" in block
    assert "build_project_traceability_report(" in block


def test_step8_traceability_tab_is_strictly_read_only():
    block = _traceability_block()
    code_lines = "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("#")
    )
    for forbidden in (
        "choose_final", "mark_final", "unlock_final",
        ".generate(", ".regenerate(", ".refine_with_ai(", ".save_manual_edit(",
        "generate_initial_", ".refine()",
        "ChatGoogleGenerativeAI", "import genai", "langchain",
    ):
        assert forbidden not in code_lines, f"read-only violation: {forbidden}"


def test_step8_traceability_tab_renders_every_required_section():
    block = _traceability_block()
    for header in (
        '"Artifact status"',
        '"Requirement coverage"',
        '"User story coverage"',
        '"Test case reference population"',
        '"Grounding findings"',
        '"Orphan references"',
        '"Traceability matrix"',
    ):
        assert f"st.subheader({header}" in block, f"missing section header {header}"
    # matrix columns the brief mandates
    for col in ("requirement_id", "requirement_kind", "requirement_title",
                "user_story_ids", "test_case_ids", "test_case_ids_direct",
                "test_case_ids_via_story", "has_user_stories", "has_test_cases",
                "is_covered"):
        assert col in block, f"matrix column {col} not surfaced"
    # by-kind requirement breakdown
    assert "Functional Requirements" in block
    assert "Non-Functional Requirements" in block
    assert "Business Requirements" in block


def test_step8_traceability_tab_handles_empty_and_partial_projects():
    block = _traceability_block()
    # empty project (no BRD): a friendly info state, not a crash
    assert "if latest_version is None:" in block
    assert "st.info(" in block
    # partial project note keyed off an existing artifact_status flag
    assert 'artifact_status"]["test_cases"]["exists"]' in block or \
           '_astat["test_cases"]["exists"]' in block


def test_step8_matrix_is_not_editable():
    block = _traceability_block()
    assert "st.data_editor" not in block          # never an editable grid
    assert "st.dataframe(" in block               # read-only table/dataframe
