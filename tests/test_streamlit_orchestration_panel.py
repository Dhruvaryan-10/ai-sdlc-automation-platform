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
    _pipeline_summary,
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
    assert "BRD approval" in _awaiting_approval_message(_empty_status(next_step="approve_brd"))
    assert "HLD approval" in _awaiting_approval_message(_empty_status(next_step="approve_hld"))
    assert "LLD approval" in _awaiting_approval_message(_empty_status(next_step="approve_lld"))
    assert "Test Case approval" in _awaiting_approval_message(_empty_status(next_step="approve_test_cases"))


# --- C/D/E. _pipeline_summary() for fresh / approval-gate / completed states --

def test_pipeline_summary_handles_a_fresh_project():
    lines = _pipeline_summary(_empty_status())
    assert lines == [
        "BRD: not generated",
        "HLD: not generated",
        "User Stories: not generated",
        "LLD: not generated",
        "Test Cases: not generated",
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
        next_step=None,
    )
    lines = _pipeline_summary(status)
    assert lines == [
        "BRD: v1 (final: v1)",
        "HLD: v1 (final: v1)",
        "User Stories: v1",
        "LLD: v1 (final: v1)",
        "Test Cases: v1 (final: v1)",
    ]
    assert _next_step_label(status) == (
        "SDLC pipeline complete — no further orchestrated action is required."
    )


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
    for fn in (_next_step_label, _awaiting_approval_message, _pipeline_summary):
        assert "st." not in inspect.getsource(fn)
