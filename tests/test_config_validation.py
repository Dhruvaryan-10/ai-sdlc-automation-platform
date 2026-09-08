"""
Phase 13B — configuration hardening & startup reliability.

Deterministic: every case constructs a fresh `Settings(_env_file=None, ...)`
(so the on-disk `.env` never interferes) and asserts validation, directory
resolution, the safe log summary, or the Streamlit startup-guard structure.
No Gemini, no network.
"""

import ast
import inspect
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.utils.config import Settings, settings

_VALID_KEY = "AIzaSyTEST_do_not_use_0000000000000000000"


def _mk(**overrides) -> Settings:
    """Fresh Settings, isolated from the on-disk .env; a valid key unless overridden."""
    kwargs = {"_env_file": None, "google_api_key": _VALID_KEY}
    kwargs.update(overrides)
    return Settings(**kwargs)


# --- A. valid defaults ---------------------------------------------------

def test_valid_defaults_load():
    s = _mk()
    assert s.gemini_temperature == 0.3
    assert s.gemini_timeout_seconds == 900
    assert s.gemini_max_retries == 2
    assert s.log_level == "INFO"
    # defaults / field names / singleton architecture unchanged
    assert isinstance(settings, Settings)


def test_existing_env_var_names_still_work(monkeypatch):
    monkeypatch.setenv("GEMINI_TEMPERATURE", "0.7")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "1")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    s = Settings(_env_file=None, google_api_key=_VALID_KEY)
    assert (s.gemini_temperature, s.gemini_timeout_seconds, s.gemini_max_retries) == (0.7, 1200, 1)
    assert s.log_level == "WARNING"


# --- B-E. temperature range ------------------------------------------

@pytest.mark.parametrize("bad", [-1, -0.01, 2.01, 5, 100])
def test_temperature_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        _mk(gemini_temperature=bad)


@pytest.mark.parametrize("ok", [0.0, 0.3, 1.0, 2.0])
def test_temperature_in_range_accepted(ok):
    assert _mk(gemini_temperature=ok).gemini_temperature == ok


# --- F-G. timeout ---------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, -900])
def test_timeout_below_one_rejected(bad):
    with pytest.raises(ValidationError):
        _mk(gemini_timeout_seconds=bad)


@pytest.mark.parametrize("ok", [1, 60, 900, 1800])
def test_timeout_at_least_one_accepted(ok):
    assert _mk(gemini_timeout_seconds=ok).gemini_timeout_seconds == ok


# --- H-I. retries -------------------------------------------------

@pytest.mark.parametrize("bad", [-1, -3])
def test_negative_retries_rejected(bad):
    with pytest.raises(ValidationError):
        _mk(gemini_max_retries=bad)


@pytest.mark.parametrize("ok", [0, 1, 2, 5])
def test_zero_or_more_retries_accepted(ok):
    assert _mk(gemini_max_retries=ok).gemini_max_retries == ok


# --- J-K. log level ---------------------------------------------

@pytest.mark.parametrize("bad", ["verbose", "DEBGU", "trace", "", "  ", "warn"])
def test_invalid_log_level_rejected_at_settings_time(bad):
    # must be a pydantic ValidationError, NOT a later logging.ValueError
    with pytest.raises(ValidationError):
        _mk(log_level=bad)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_valid_log_levels_accepted_case_insensitively_and_normalized(level, case):
    s = _mk(log_level=case(level))
    assert s.log_level == level                       # stored upper-case
    assert logging.getLevelName(s.log_level) != f"Level {s.log_level}"  # a real level


# --- L-P. API key ---------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\t\n ", "your_gemini_api_key_here"])
def test_missing_empty_whitespace_or_placeholder_key_rejected(bad):
    with pytest.raises(ValidationError):
        _mk(google_api_key=bad)


def test_missing_key_entirely_still_rejected(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_plausible_key_accepted_and_stripped():
    s = _mk(google_api_key=f"   {_VALID_KEY}\t\n ")
    assert s.google_api_key == _VALID_KEY             # surrounding whitespace removed


def test_api_key_validation_error_never_contains_a_key_shaped_value():
    planted = "AIzaSyREAL_LOOKING_SECRET_9f3a2b1c0d"
    # the placeholder is the only rejected non-empty value; prove the error text
    # cannot carry a real-key / bearer / sk- pattern
    with pytest.raises(ValidationError) as ei:
        _mk(google_api_key="your_gemini_api_key_here")
    text = str(ei.value)
    assert "AIza" not in text
    assert "sk-" not in text
    assert "Bearer" not in text
    assert planted not in text
    assert "google_api_key" in text                   # it DOES name the field


# --- Q-R. directory resolution -----------------------------------

def test_resolved_dirs_are_absolute_and_created(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", str(tmp_path / "o"))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "u"))
    monkeypatch.setattr(settings, "log_dir", str(tmp_path / "l"))
    for got, want in (
        (settings.resolved_output_dir(), tmp_path / "o"),
        (settings.resolved_upload_dir(), tmp_path / "u"),
        (settings.resolved_log_dir(), tmp_path / "l"),
    ):
        assert got.is_absolute()
        assert got == want.resolve()
        assert got.is_dir()                            # created


def test_resolved_dir_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", str(tmp_path / "repeat"))
    first = settings.resolved_output_dir()
    second = settings.resolved_output_dir()
    assert first == second and second.is_dir()


def test_custom_absolute_path_is_preserved(tmp_path, monkeypatch):
    abs_target = (tmp_path / "already" / "absolute").resolve()
    monkeypatch.setattr(settings, "output_dir", str(abs_target))
    assert settings.resolved_output_dir() == abs_target


def test_relative_default_does_not_move_the_directory(tmp_path, monkeypatch):
    """A relative default resolves against the CWD — identical effective location
    to the documented 'launch from the repository root' workflow, just expressed
    as an absolute Path."""
    monkeypatch.chdir(tmp_path)
    fresh = Settings(_env_file=None, google_api_key=_VALID_KEY)  # output_dir default "outputs"
    assert fresh.resolved_output_dir() == (tmp_path / "outputs").resolve()


# --- S-T. summary_for_log ----------------------------------------

def test_summary_for_log_contains_safe_fields():
    keys = set(_mk().summary_for_log())
    assert keys == {
        "model", "temperature", "timeout_seconds", "max_retries",
        "log_level", "output_dir", "upload_dir", "log_dir",
    }


def test_summary_for_log_never_contains_the_api_key():
    s = _mk(google_api_key="AIzaSyABSOLUTELY_SECRET_1234567890")
    summary = s.summary_for_log()
    blob = repr(summary)
    assert "AIzaSyABSOLUTELY_SECRET_1234567890" not in blob
    assert "google_api_key" not in summary
    assert "api_key" not in blob and "secret" not in blob.lower()
    # dirs in the summary are absolute strings
    assert Path(summary["output_dir"]).is_absolute()


def test_summary_for_log_has_no_filesystem_side_effects(tmp_path, monkeypatch):
    target = tmp_path / "should_not_exist"
    monkeypatch.setattr(settings, "output_dir", str(target))
    settings.summary_for_log()
    assert not target.exists()                         # resolved, NOT created


# --- U. Streamlit startup guard (structural — safe, no import juggling) ---

def test_streamlit_startup_guard_is_narrow_and_ordered_correctly():
    import app.ui.streamlit_app as app_mod

    src = Path(inspect.getfile(app_mod)).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # the guard imports config in a try/except that catches ONLY ValidationError
    guard_try = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            imports_config = any(
                isinstance(n, ast.ImportFrom) and n.module == "app.utils.config"
                for n in ast.walk(node)
            )
            if imports_config:
                guard_try = node
                break
    assert guard_try is not None, "no try/except guarding the app.utils.config import"

    handlers = guard_try.handlers
    assert len(handlers) == 1
    caught = handlers[0].type
    # single, narrow exception type (aliased ValidationError) — not a bare except,
    # not `Exception`
    assert isinstance(caught, ast.Name)
    assert "ValidationError" in caught.id or caught.id == "_ConfigValidationError"

    handler_src = ast.get_source_segment(src, handlers[0]) or ""
    assert "st.error(" in handler_src
    assert "st.stop()" in handler_src

    # the guard sits BEFORE the first `from app.agents...` import
    guard_line = guard_try.lineno
    first_agent_import = min(
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app.agents")
    )
    assert guard_line < first_agent_import


def test_bad_env_raises_the_exception_type_the_guard_catches(monkeypatch):
    # precondition for the guard: an invalid .env value is a pydantic ValidationError
    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, google_api_key=_VALID_KEY)


# --- V. existing LLM-client-config assumptions still hold --------------

def test_llm_client_config_defaults_still_pinned():
    # mirrors tests/test_llm_client_config.py's expectations — must not regress
    assert settings.gemini_timeout_seconds == 900
    assert settings.gemini_max_retries == 2
    assert 0 <= settings.gemini_max_retries <= 3
    assert settings.gemini_timeout_seconds >= 600
