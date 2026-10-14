"""
Free-tier stabilisation — per-session duplicate-generation guard, and the
Light/Dark theme switcher.

Pure tests over plain dicts standing in for `st.session_state` (the guard and
theme helpers never import Streamlit), plus static checks on
`app/ui/streamlit_app.py` that every Gemini-triggering control is guarded.
No Gemini calls, no browser.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.ui import generation_guard as gg
from app.ui import theme as th


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class _RerunInterrupt(BaseException):
    """Stands in for Streamlit's RerunException / StopException (BaseException)."""


# --- duplicate-generation guard -------------------------------------------------

def _click(state, op, work, clock):
    """Simulates one script run in which the button for `op` reads True —
    exactly the UI pattern `if st.button(...) and _begin_generation(op): with slot:`."""
    decision = gg.try_begin(state, op, clock=clock)
    if decision.allowed:
        with gg.slot(state, op, clock=clock):
            work()
    return decision


def test_double_submission_runs_the_work_once():
    state, clock, calls = {}, _Clock(), []

    def work():
        calls.append(1)
        clock.t += 40.0             # a long Gemini call; the 2nd click is queued meanwhile

    first = _click(state, "brd_generate", work, clock)
    clock.t += 0.05                 # Streamlit replays the queued click right after
    second = _click(state, "brd_generate", work, clock)

    assert first.allowed and not second.allowed
    assert second.reason == "duplicate"
    assert calls == [1]


def test_exception_resets_the_in_progress_state():
    state, clock = {}, _Clock()

    def boom():
        raise RuntimeError("Gemini API call failed")

    with pytest.raises(RuntimeError):
        _click(state, "hld_generate", boom, clock)
    assert gg.is_running(state, clock=clock) is False


def test_streamlit_interruption_resets_the_in_progress_state():
    state, clock = {}, _Clock()

    def interrupted():
        raise _RerunInterrupt()

    with pytest.raises(_RerunInterrupt):
        _click(state, "pipeline_run", interrupted, clock)
    assert gg.is_running(state, clock=clock) is False


def test_success_resets_and_a_later_deliberate_click_is_allowed():
    state, clock, calls = {}, _Clock(), []
    _click(state, "brd_refine", lambda: calls.append(1), clock)
    clock.t += gg.DUPLICATE_WINDOW_S + 0.1
    assert _click(state, "brd_refine", lambda: calls.append(1), clock).allowed
    assert calls == [1, 1]


def test_a_different_operation_is_not_treated_as_a_duplicate():
    state, clock = {}, _Clock()
    _click(state, "brd_refine", lambda: None, clock)
    assert _click(state, "hld_generate", lambda: None, clock).allowed


def test_a_running_generation_blocks_any_other_start():
    state, clock = {}, _Clock()
    assert gg.try_begin(state, "lld_generate", clock=clock).allowed
    blocked = gg.try_begin(state, "qa_generate", clock=clock)
    assert not blocked.allowed and blocked.reason == "running"


def test_a_stale_running_marker_never_blocks_forever():
    state, clock = {}, _Clock()
    gg.try_begin(state, "lld_generate", clock=clock)       # process died mid-run
    clock.t += gg.STALE_RUNNING_S + 1
    assert gg.try_begin(state, "lld_generate", clock=clock).allowed


def test_rate_limit_cooldown_blocks_until_googles_window_passes():
    state, clock = {}, _Clock()
    gg.note_retry_after(state, 42.0, clock=clock)
    refused = gg.try_begin(state, "brd_generate", clock=clock)
    assert not refused.allowed and refused.reason == "cooldown"
    assert "42 seconds" in refused.message
    clock.t += 42.1
    assert gg.try_begin(state, "brd_generate", clock=clock).allowed


def test_cooldown_is_capped_and_ignores_bad_values():
    state, clock = {}, _Clock()
    gg.note_retry_after(state, None, clock=clock)
    gg.note_retry_after(state, -5, clock=clock)
    assert gg.cooldown_remaining(state, clock=clock) == 0
    gg.note_retry_after(state, 10 ** 6, clock=clock)
    assert gg.cooldown_remaining(state, clock=clock) == gg.MAX_COOLDOWN_S


def test_sessions_are_isolated():
    alice, bob, clock = {}, {}, _Clock()
    gg.try_begin(alice, "brd_generate", clock=clock)
    gg.note_retry_after(alice, 30, clock=clock)
    assert gg.try_begin(bob, "brd_generate", clock=clock).allowed


def test_guard_leaves_project_and_artifact_state_untouched():
    state = {"project_id": "abc12345", "versions": ["v1", "v2"], "viewing_version": 2}
    snapshot = dict(state)
    clock = _Clock()
    _click(state, "brd_refine", lambda: None, clock)
    _click(state, "brd_refine", lambda: None, clock)           # refused duplicate
    assert {k: v for k, v in state.items() if k != gg.STATE_KEY} == snapshot


def test_guard_module_holds_no_global_mutable_state():
    src = inspect.getsource(gg)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            assert not isinstance(value, (ast.Dict, ast.List, ast.Set)), ast.unparse(node)


# --- friendly_error records Google's retry window for the session ---------------------

def test_friendly_error_starts_a_session_cooldown_for_rate_limits(monkeypatch):
    import app.ui.streamlit_app as ui
    from app.agents.business_analyst.agent import BusinessAnalystAgentError

    fake_state: dict = {}
    monkeypatch.setattr(ui.st, "session_state", fake_state, raising=False)
    msg = ui.friendly_error(BusinessAnalystAgentError(
        "Gemini API call failed: 429 RESOURCE_EXHAUSTED. Please retry in 45s."))
    assert "45 seconds" in msg
    assert gg.cooldown_remaining(fake_state) > 40


# --- every Gemini-triggering control in the UI is guarded ------------------------------

_UI_SRC = Path(inspect.getfile(__import__("app.ui.streamlit_app", fromlist=["x"]))).read_text(
    encoding="utf-8")
_UI_TREE = ast.parse(_UI_SRC)


def _with_items(node):
    return [ast.unparse(item.context_expr) for item in node.items]


def test_every_gemini_spinner_and_the_pipeline_status_hold_a_generation_slot():
    gemini_blocks = []
    for node in ast.walk(_UI_TREE):
        if isinstance(node, ast.With):
            items = _with_items(node)
            joined = " ".join(items)
            if ("st.spinner(" in joined and "Formatting Word" not in joined) or "st.status(" in joined:
                gemini_blocks.append(items)
    assert len(gemini_blocks) == 15
    for items in gemini_blocks:
        assert items[0].startswith("_generation_slot("), items


def test_every_generation_button_goes_through_begin_generation():
    ops = set()
    for node in ast.walk(_UI_TREE):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_begin_generation":
            ops.add(node.args[0].value)
    assert ops == {
        "pipeline_run", "brd_generate", "brd_refine", "hld_generate", "hld_refine",
        "us_generate", "us_refine", "lld_generate", "lld_refine", "usr_refine",
        "qa_generate", "qa_refine", "qa_regenerate", "closure_generate",
        "closure_regenerate",
    }


# --- theme switcher ---------------------------------------------------------------------

def test_dark_is_the_first_session_default():
    state: dict = {}
    assert th.ensure_theme(state, {}) == th.DARK
    assert state[th.THEME_KEY] == "Dark"


def test_light_mode_can_be_selected():
    state = {th.THEME_KEY: "Light"}
    assert th.ensure_theme(state, {}) == th.LIGHT
    css = th.theme_css(th.LIGHT)
    assert 'data-sdlc-theme="light"' in css and "color-scheme: light" in css
    assert "--sdlc-bg: #ffffff" in css


def test_dark_mode_can_be_selected():
    state = {th.THEME_KEY: "Dark"}
    assert th.ensure_theme(state, {"theme": "light"}) == th.DARK   # explicit choice wins
    css = th.theme_css(th.DARK)
    assert 'data-sdlc-theme="dark"' in css and "color-scheme: dark" in css
    assert "--sdlc-bg: #0e1117" in css


def test_theme_persists_across_ordinary_reruns():
    state: dict = {}
    th.ensure_theme(state, {})
    state[th.THEME_KEY] = "Light"            # the radio's on-change writes its key
    for _ in range(5):                       # five reruns
        assert th.ensure_theme(state, {}) == th.LIGHT


def test_theme_survives_a_session_wipe_via_the_url():
    # project switching wipes session_state; ?theme= restores the explicit choice
    assert th.ensure_theme({}, {"theme": "light"}) == th.LIGHT
    assert th.query_value(th.LIGHT) == "light"


@pytest.mark.parametrize("bad", ["", "purple", None, 3, ["blue"]])
def test_invalid_values_fall_back_to_dark(bad):
    assert th.ensure_theme({th.THEME_KEY: bad}, {"theme": bad}) == th.DARK


def test_both_themes_define_the_same_tokens():
    assert set(th.tokens(th.LIGHT)) == set(th.tokens(th.DARK))


def _contrast(fg: str, bg: str) -> float:
    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("theme", [th.LIGHT, th.DARK])
def test_text_tokens_meet_wcag_aa(theme):
    t = th.tokens(theme)
    pairs = [
        ("--sdlc-text", "--sdlc-bg"), ("--sdlc-text", "--sdlc-bg-secondary"),
        ("--sdlc-text-muted", "--sdlc-bg"), ("--sdlc-text-muted", "--sdlc-bg-secondary"),
        ("--sdlc-accent", "--sdlc-bg"), ("--sdlc-on-primary", "--sdlc-primary-bg"),
        ("--sdlc-info-text", "--sdlc-info-bg"), ("--sdlc-success-text", "--sdlc-success-bg"),
        ("--sdlc-warning-text", "--sdlc-warning-bg"), ("--sdlc-error-text", "--sdlc-error-bg"),
        ("--sdlc-delta-up", "--sdlc-bg"), ("--sdlc-delta-down", "--sdlc-bg"),
    ]
    for fg, bg in pairs:
        assert _contrast(t[fg], t[bg]) >= 4.5, (theme, fg, bg)
    assert _contrast(t["--sdlc-border-strong"], t["--sdlc-bg"]) >= 3.0   # WCAG 1.4.11
    assert _contrast(t["--sdlc-focus"], t["--sdlc-bg"]) >= 3.0


def test_theme_css_is_safe_and_uses_stable_selectors_only():
    for theme in th.OPTIONS:
        css = th.theme_css(theme)
        assert "<script" not in css.lower()
        assert "st-emotion-cache" not in css and ".css-" not in css   # no generated classes
        assert ":focus-visible" in css                                 # visible focus
        assert "prefers-reduced-motion" in css and "prefers-contrast" in css


def test_ui_places_the_switch_near_the_top_and_preserves_it_on_project_switch():
    title_at = _UI_SRC.index('st.title("AI-SDLC Pipeline")')
    radio_at = _UI_SRC.index("key=_theme.THEME_KEY")
    pipeline_at = _UI_SRC.index("# --- SDLC Pipeline panel (Phase 8B-6)")
    assert title_at < radio_at < pipeline_at
    assert '"Light"' in inspect.getsource(th) and '"Dark"' in inspect.getsource(th)
    css_at = _UI_SRC.index("_theme.theme_css(_active_theme)")
    assert css_at < title_at                       # stylesheet first: no wrong-theme flash
    switch_src = _UI_SRC[_UI_SRC.index("def switch_project"):_UI_SRC.index("_METADATA_LINE_RE")]
    assert "_theme.THEME_KEY" in switch_src and "_gen_guard.STATE_KEY" in switch_src


def test_native_base_theme_is_dark_in_config():
    cfg = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"
    import tomllib

    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["theme"]["base"] == "dark"
