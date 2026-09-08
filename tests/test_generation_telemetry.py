"""
Phase 13A — observability & generation telemetry.

Deterministic: fake LLM objects (no Gemini, no network), the real
`Stub*Agent`s + real services for the `run_step` cases. Verifies run
correlation, per-provider-invocation generation ids, token extraction, latency,
outcome, `run_step` start/complete/failure logging, security (no secret / prompt
/ model-output / ValidationError leakage), and that none of the retry-classifier
machinery was touched.
"""

import logging
import threading
from pathlib import Path

import pytest

from app.orchestration.graph import run_step
from app.utils import metrics
from app.utils.metrics import instrumented_invoke, log_llm_call
from app.utils.run_context import (
    current_project_id,
    current_run_id,
    new_generation_id,
    new_run_id,
    run_context,
)

_TELEMETRY_LOGGER = "app.telemetry"
_GRAPH_LOGGER = "app.orchestration.graph"


# --- fakes ---------------------------------------------------------------

class FakeMessage:
    def __init__(self, content="ok", usage_metadata=None, response_metadata=None):
        self.content = content
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata
        if response_metadata is not None:
            self.response_metadata = response_metadata


class FakeLLM:
    """Records every prompt it is invoked with; returns a message or raises."""

    def __init__(self, *, message=None, error=None):
        self._message = message if message is not None else FakeMessage()
        self._error = error
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if self._error is not None:
            raise self._error
        return self._message


# --- helpers ----------------------------------------------------------

def _visible(monkeypatch, caplog, name, level=logging.INFO):
    monkeypatch.setattr(logging.getLogger(name), "propagate", True)
    return caplog.at_level(level, logger=name)


def _telemetry_lines(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.name == _TELEMETRY_LOGGER and r.getMessage().startswith("event=llm_call ")
    ]


def _parse(line):
    out = {}
    for tok in line.split(" "):
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


# =====================================================================
# A. run_id correlation
# =====================================================================

def test_run_step_creates_a_uuid_run_id_and_logs_start_and_complete(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent,
    stub_closure_agent, sow_file, sample_metadata, monkeypatch, caplog,
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.closure_report.service import ClosureReportService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService

    ba = BusinessAnalystService(project_id="t13a1", agent=stub_ba_agent)
    sa = SolutionArchitectService(project_id="t13a1", ba_service=ba, agent=stub_sa_agent)
    us = InitialUserStoryService(project_id="t13a1", ba_service=ba, agent=stub_us_agent)
    lld = LowLevelDesignService(project_id="t13a1", sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    tc = TestCaseService(project_id="t13a1", agent=stub_tc_agent)
    cr = ClosureReportService(project_id="t13a1", agent=stub_closure_agent)

    with _visible(monkeypatch, caplog, _GRAPH_LOGGER):
        state = run_step(
            "t13a1", "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
            ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
            tc_service=tc, closure_service=cr,
        )

    lines = [r.getMessage() for r in caplog.records if r.name == _GRAPH_LOGGER]
    start = next(l for l in lines if l.startswith("run_step start run_id="))
    done = next(l for l in lines if l.startswith("run_step complete run_id="))
    rid = start.split("run_id=")[1].split(" ")[0]
    assert len(rid) == 36 and rid.count("-") == 4          # UUID4 string
    assert f"run_id={rid}" in done and "elapsed_ms=" in done
    assert state.get("status") == "awaiting_approval"       # unchanged behaviour


def test_nested_llm_call_during_run_step_sees_the_same_run_id(
    stub_sa_agent, stub_us_agent, stub_lld_agent, stub_tc_agent, stub_closure_agent,
    sow_file, sample_metadata, monkeypatch, caplog,
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.closure_report.service import ClosureReportService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService

    seen = {}

    class TelemetryBAAgent:
        """A BA agent that performs a (fake) instrumented provider call, so we
        can prove the telemetry record carries run_step's run_id."""

        def generate_brd(self, clean_sow, metadata):
            seen["run_id_in_agent"] = current_run_id()
            seen["project_id_in_agent"] = current_project_id()
            instrumented_invoke(
                FakeLLM(message=FakeMessage(
                    usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
                )),
                "prompt", stage="brd",
            )
            return (
                "# Test Project — Business Requirement Document\n\n"
                "**Version:** 0\n**Client:** Acme Corp\n**Project Type:** Web Application\n\n"
                "## 8. Functional Requirements\nFR-1. The system shall do the thing.\n"
            )

    ba = BusinessAnalystService(project_id="t13a2", agent=TelemetryBAAgent())
    sa = SolutionArchitectService(project_id="t13a2", ba_service=ba, agent=stub_sa_agent)
    us = InitialUserStoryService(project_id="t13a2", ba_service=ba, agent=stub_us_agent)
    lld = LowLevelDesignService(project_id="t13a2", sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    tc = TestCaseService(project_id="t13a2", agent=stub_tc_agent)
    cr = ClosureReportService(project_id="t13a2", agent=stub_closure_agent)

    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER), \
            _visible(monkeypatch, caplog, _GRAPH_LOGGER):
        run_step(
            "t13a2", "ensure_brd", sow_path=str(sow_file), metadata=sample_metadata,
            ba_service=ba, sa_service=sa, us_service=us, lld_service=lld,
            tc_service=tc, closure_service=cr,
        )

    start = next(
        r.getMessage() for r in caplog.records
        if r.name == _GRAPH_LOGGER and r.getMessage().startswith("run_step start")
    )
    rid = start.split("run_id=")[1].split(" ")[0]
    rec = _parse(_telemetry_lines(caplog)[0])
    assert seen["run_id_in_agent"] == rid
    assert seen["project_id_in_agent"] == "t13a2"
    assert rec["run_id"] == rid and rec["project_id"] == "t13a2" and rec["stage"] == "brd"


def test_run_id_is_not_leaked_between_sequential_runs():
    r1 = new_run_id()
    with run_context(run_id=r1, project_id="p1"):
        assert current_run_id() == r1 and current_project_id() == "p1"
    assert current_run_id() == "-" and current_project_id() == "-"   # restored

    r2 = new_run_id()
    with run_context(run_id=r2):
        assert current_run_id() == r2
    assert current_run_id() == "-"
    assert r1 != r2


def test_concurrent_contexts_do_not_share_run_ids():
    results = {}
    barrier = threading.Barrier(3)

    def worker(name):
        rid = new_run_id()
        with run_context(run_id=rid, project_id=name):
            barrier.wait()  # force overlap
            results[name] = (current_run_id(), current_project_id())

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {rid for rid, _ in results.values()}
    assert len(ids) == 3                       # every thread had its own id
    for name, (_, pid) in results.items():
        assert pid == name                     # no cross-talk
    assert current_run_id() == "-"             # main thread untouched


def test_direct_instrumented_invoke_works_without_a_run_context():
    llm = FakeLLM(message=FakeMessage(content="direct"))
    out = instrumented_invoke(llm, "p", stage="hld")   # no run_context active
    assert out.content == "direct"
    assert llm.calls == ["p"]


# =====================================================================
# B. generation_id
# =====================================================================

def test_generation_id_is_unique_per_provider_invocation(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        for _ in range(5):
            instrumented_invoke(FakeLLM(), "p", stage="lld")
    gen_ids = [_parse(l)["generation_id"] for l in _telemetry_lines(caplog)]
    assert len(gen_ids) == 5 and len(set(gen_ids)) == 5


def test_retry_attempts_have_distinct_generation_ids_and_increasing_attempt(
    stub_ba_agent, sow_file, sample_metadata, monkeypatch, caplog,
):
    """The structured retry loop calls `instrumented_invoke` once per attempt;
    each must get its own generation_id and the recorded `attempt` counter."""
    from app.agents.test_case.agent import TestCaseAgent
    from app.agents.test_case.service import TestCaseService

    # a transient error on the first two attempts, success on the third
    class FlakyStructuredLLM:
        def __init__(self):
            self.n = 0

        def invoke(self, prompt):
            self.n += 1
            if self.n < 3:
                raise RuntimeError("503 UNAVAILABLE - the model is overloaded")
            from app.agents.test_case.schema import TestCaseList
            return TestCaseList(test_cases=[{
                "id": "TC-001", "title": "t", "requirement_or_story_ref": "FR-1",
                "test_steps": ["s"], "expected_result": "ok",
                "priority": "High", "test_type": "Functional",
            }])

    agent = TestCaseAgent()
    agent._structured_llm = FlakyStructuredLLM()   # inject; bypass real client build
    monkeypatch.setattr("app.agents.test_case.agent.time.sleep", lambda *_: None)

    _final_brd(stub_ba_agent, sow_file, sample_metadata, "t13b2")  # seed a final BRD
    svc = TestCaseService(project_id="t13b2", agent=agent)

    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        v1 = svc.generate()

    assert v1.version == 1                          # generation still succeeded
    recs = [_parse(l) for l in _telemetry_lines(caplog)]
    assert [r["attempt"] for r in recs] == ["1", "2", "3"]
    assert [r["outcome"] for r in recs] == ["error", "error", "success"]
    assert len({r["generation_id"] for r in recs}) == 3
    assert {r["stage"] for r in recs} == {"test_cases"}


def _final_brd(stub_ba_agent, sow_file, sample_metadata, project_id):
    from app.agents.business_analyst.service import BusinessAnalystService

    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(str(sow_file), sample_metadata)
    ba.choose_final_brd(1)
    return ba


# =====================================================================
# C. token extraction
# =====================================================================

def test_normal_usage_metadata_extracts_all_three(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(
            FakeLLM(message=FakeMessage(usage_metadata={
                "input_tokens": 100, "output_tokens": 40, "total_tokens": 140
            })),
            "p", stage="brd",
        )
    rec = _parse(_telemetry_lines(caplog)[0])
    assert (rec["prompt_tokens"], rec["completion_tokens"], rec["total_tokens"]) == ("100", "40", "140")


def test_missing_usage_metadata_is_safe(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(FakeLLM(message=FakeMessage()), "p", stage="brd")
    rec = _parse(_telemetry_lines(caplog)[0])
    assert rec["prompt_tokens"] == "-" and rec["completion_tokens"] == "-" and rec["total_tokens"] == "-"
    assert rec["outcome"] == "success"


def test_partial_usage_metadata_derives_total(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(
            FakeLLM(message=FakeMessage(usage_metadata={"input_tokens": 12, "output_tokens": 8})),
            "p", stage="hld",
        )
    rec = _parse(_telemetry_lines(caplog)[0])
    assert (rec["prompt_tokens"], rec["completion_tokens"], rec["total_tokens"]) == ("12", "8", "20")


def test_malformed_usage_metadata_never_raises(monkeypatch, caplog):
    bad = [
        {"input_tokens": "lots", "output_tokens": None},
        {"input_tokens": -5, "output_tokens": 3.5},
        {"nonsense": object()},
        "not a dict",
        123,
    ]
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        for um in bad:
            out = instrumented_invoke(FakeLLM(message=FakeMessage(usage_metadata=um)), "p", stage="lld")
            assert out.content == "ok"           # value unchanged, no exception
    for line in _telemetry_lines(caplog):
        rec = _parse(line)
        assert rec["prompt_tokens"] == "-" and rec["total_tokens"] == "-"


def test_usage_from_response_metadata_fallback(monkeypatch, caplog):
    msg = FakeMessage(response_metadata={"usage_metadata": {
        "prompt_token_count": 9, "candidates_token_count": 6, "total_token_count": 15
    }})
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(FakeLLM(message=msg), "p", stage="brd")
    rec = _parse(_telemetry_lines(caplog)[0])
    assert (rec["prompt_tokens"], rec["completion_tokens"], rec["total_tokens"]) == ("9", "6", "15")


def test_extract_usage_never_raises_on_hostile_object():
    class Hostile:
        @property
        def usage_metadata(self):
            raise RuntimeError("boom")

    assert metrics.extract_usage(Hostile()) == (None, None, None)
    assert metrics.extract_usage(None) == (None, None, None)


# =====================================================================
# D. latency
# =====================================================================

def test_latency_is_recorded_and_return_value_is_unchanged(monkeypatch, caplog):
    sentinel = FakeMessage(content="unchanged-payload")
    llm = FakeLLM(message=sentinel)
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        out = instrumented_invoke(llm, "the-prompt", stage="brd")
    assert out is sentinel                                  # identity preserved
    assert llm.calls == ["the-prompt"]                      # prompt forwarded verbatim
    rec = _parse(_telemetry_lines(caplog)[0])
    assert rec["latency_ms"].isdigit()


def test_telemetry_logging_failure_does_not_break_the_call(monkeypatch, caplog):
    monkeypatch.setattr(
        metrics, "log_llm_call",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("telemetry sink exploded")),
    )
    # instrumented_invoke calls log_llm_call; if that raised through, this fails.
    out = instrumented_invoke(FakeLLM(message=FakeMessage(content="still ok")), "p", stage="brd")
    assert out.content == "still ok"


# =====================================================================
# E. outcome
# =====================================================================

def test_successful_call_logs_outcome_success(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(FakeLLM(), "p", stage="closure_report")
    assert _parse(_telemetry_lines(caplog)[0])["outcome"] == "success"


def test_failed_call_logs_outcome_error_and_propagates_original_exception(monkeypatch, caplog):
    boom = ValueError("original provider failure")
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        with pytest.raises(ValueError) as ei:
            instrumented_invoke(FakeLLM(error=boom), "p", stage="lld")
    assert ei.value is boom                                 # SAME exception object
    rec = _parse(_telemetry_lines(caplog)[0])
    assert rec["outcome"] == "error" and rec["error_type"] == "ValueError"


# =====================================================================
# F. run_step
# =====================================================================

def test_run_step_failure_logs_run_id_stage_and_re_raises(
    stub_ba_agent, sow_file, sample_metadata, monkeypatch, caplog,
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.solution_architect.agent import SolutionArchitectAgentError
    from app.agents.solution_architect.service import SolutionArchitectService

    ba = BusinessAnalystService(project_id="t13f1", agent=stub_ba_agent)
    ba.generate_initial_brd(str(sow_file), sample_metadata)
    ba.choose_final_brd(1)

    class BoomSA:
        def generate_hld(self, brd_text, metadata):
            raise SolutionArchitectAgentError("HLD Gemini exploded (secret AIzaLEAK inside)")

    sa = SolutionArchitectService(project_id="t13f1", ba_service=ba, agent=BoomSA())

    with _visible(monkeypatch, caplog, _GRAPH_LOGGER, level=logging.ERROR):
        with pytest.raises(SolutionArchitectAgentError):
            run_step("t13f1", "ensure_brd", sow_path=str(sow_file),
                     metadata=sample_metadata, ba_service=ba, sa_service=sa)

    failed = next(
        r.getMessage() for r in caplog.records
        if r.name == _GRAPH_LOGGER and r.getMessage().startswith("run_step failed")
    )
    assert "run_id=" in failed and "stage=hld" in failed
    assert "error_type=SolutionArchitectAgentError" in failed and "elapsed_ms=" in failed
    assert "AIzaLEAK" not in failed                          # no chained exception text


# =====================================================================
# G. security — nothing sensitive in telemetry
# =====================================================================

_FAKE_KEY = "AIzaSyD" + "Z" * 33
_FAKE_BEARER = "Authorization: Bearer sk-live-" + "9" * 30
_PROMPT = "SECRET_PROMPT_BODY_do_not_log_me_12345"
_MODEL_OUT = "SECRET_MODEL_OUTPUT_do_not_log_me_98765"


def test_no_prompt_or_model_output_in_telemetry_on_success(monkeypatch, caplog):
    msg = FakeMessage(content=_MODEL_OUT, usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10})
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        instrumented_invoke(FakeLLM(message=msg), _PROMPT, stage="brd")
    line = _telemetry_lines(caplog)[0]
    assert _PROMPT not in line and _MODEL_OUT not in line


def test_no_key_bearer_or_provider_blob_in_telemetry_on_error(monkeypatch, caplog):
    boom = RuntimeError(f"429 RESOURCE_EXHAUSTED key={_FAKE_KEY} {_FAKE_BEARER}")
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        with pytest.raises(RuntimeError):
            instrumented_invoke(FakeLLM(error=boom), "p", stage="hld")
    line = _telemetry_lines(caplog)[0]
    assert _FAKE_KEY not in line and "Bearer" not in line and "sk-live-" not in line
    assert "Authorization" not in line and "RESOURCE_EXHAUSTED" not in line
    assert "error_type=RuntimeError" in line                 # type name only


def test_validation_error_values_do_not_reach_telemetry(monkeypatch, caplog):
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        x: int

    try:
        M(x="SECRET_VALIDATION_INPUT_VALUE_ABC")
    except ValidationError as ve:
        with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
            with pytest.raises(ValidationError):
                instrumented_invoke(FakeLLM(error=ve), "p", stage="test_cases", attempt=1)
        line = _telemetry_lines(caplog)[0]
        assert "SECRET_VALIDATION_INPUT_VALUE_ABC" not in line
        assert "error_type=ValidationError" in line and "attempt=1" in line


def test_log_app_error_style_fields_are_bounded_and_machine_readable(monkeypatch, caplog):
    with _visible(monkeypatch, caplog, _TELEMETRY_LOGGER):
        log_llm_call(stage="brd", generation_id=new_generation_id(),
                     latency_ms=42, outcome="success",
                     response=FakeMessage(usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}))
    line = _telemetry_lines(caplog)[0]
    rec = _parse(line)
    assert set(rec) >= {
        "event", "run_id", "generation_id", "project_id", "stage", "model",
        "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms",
        "attempt", "outcome",
    }
    assert rec["event"] == "llm_call" and "\n" not in line and "{" not in line


# =====================================================================
# H. regression — retry classifier & marker defs untouched by 13A
# =====================================================================

def test_retry_classifier_and_markers_unchanged_by_phase_13a():
    import app.agents.closure_report.agent as cra
    import app.agents.test_case.agent as tca

    for mod in (tca, cra):
        assert isinstance(mod._TRANSIENT_MARKERS, tuple)
        assert callable(mod._is_transient_llm_error)
        assert mod._RETRY_MAX_ATTEMPTS == 3
    assert tca._TRANSIENT_MARKERS == cra._TRANSIENT_MARKERS

    # the telemetry modules never reference the retry classifier
    for mod in (metrics, __import__("app.utils.run_context", fromlist=["x"])):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "_TRANSIENT_MARKERS" not in src
        assert "_is_transient_llm_error" not in src


def test_is_transient_llm_error_still_classifies_the_same(monkeypatch):
    from app.agents.test_case.agent import _is_transient_llm_error

    assert _is_transient_llm_error(RuntimeError("503 UNAVAILABLE")) is True
    assert _is_transient_llm_error(RuntimeError("server disconnected")) is True
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        x: int

    try:
        M(x="bad")
    except ValidationError as ve:
        assert _is_transient_llm_error(ve) is False
