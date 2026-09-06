"""
Phase 7 — Closure Report package independence + stream isolation.

* `app/agents/closure_report/{agent,service,schema}.py` import NO other agent
  package's implementation (only the shared `ProjectMetadata` value type +
  `PromptManager` infrastructure from `business_analyst`, and `app.quality.*`).
* `ClosureReportService.generate()` writes ONLY
  `outputs/<pid>/closure_report/versions.json` - every upstream stream is
  byte-identical afterwards.
"""

import ast
import json
from pathlib import Path

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.closure_report.service import ClosureReportService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService
from tests.conftest import (
    StubBAAgent,
    StubClosureReportAgent,
    StubLLDAgent,
    StubSAAgent,
    StubTestCaseAgent,
    StubUserStoryAgent,
)

_PKG = Path(__file__).resolve().parents[1] / "app" / "agents" / "closure_report"

_FORBIDDEN_MODULE_PREFIXES = (
    "app.agents.solution_architect",
    "app.agents.initial_user_story",
    "app.agents.low_level_design",
    "app.agents.user_story_refinement",
    "app.agents.test_case",
)
_ALLOWED_BUSINESS_ANALYST = {
    "app.agents.business_analyst.agent",          # ProjectMetadata (shared value type)
    "app.agents.business_analyst.prompt_manager",  # PromptManager (shared infra)
}


def _imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def test_closure_report_package_imports_no_other_agent_implementation():
    for py_file in sorted(_PKG.glob("*.py")):
        mods = _imported_modules(py_file)
        for mod in mods:
            assert not mod.startswith(_FORBIDDEN_MODULE_PREFIXES), (
                f"{py_file.name} imports forbidden module {mod!r}"
            )
            if mod.startswith("app.agents.business_analyst"):
                assert mod in _ALLOWED_BUSINESS_ANALYST, (
                    f"{py_file.name} imports {mod!r}; only the shared "
                    f"ProjectMetadata / PromptManager are allowed"
                )


def test_closure_report_reads_evidence_only_through_quality_layer():
    service_mods = _imported_modules(_PKG / "service.py")
    # Phase 11A: closure now assembles its evidence via the SINGLE combined
    # entry point `build_project_reports_for_project` (from
    # app.quality.project_quality_report), which internally composes the
    # traceability report. It no longer imports app.quality.traceability
    # directly - the coupling to the quality layer is narrower, not wider.
    assert "app.quality.project_quality_report" in service_mods
    assert "app.quality.traceability" not in service_mods
    # still reads EVERYTHING through app.quality.* - no re-implementation
    quality_mods = [m for m in service_mods if m.startswith("app.quality")]
    assert quality_mods == ["app.quality.project_quality_report"]
    # never constructs the upstream agent services itself
    assert not any(m.startswith(_FORBIDDEN_MODULE_PREFIXES) for m in service_mods)


def _full_pipeline(pid, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=pid, agent=StubBAAgent())
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=StubSAAgent())
    sa.generate_initial_hld()
    sa.choose_final_hld(1)
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=StubUserStoryAgent())
    us.generate_initial_stories()
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=StubLLDAgent())
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    tc = TestCaseService(project_id=pid, agent=StubTestCaseAgent())
    tc.generate()
    tc.choose_final(1)
    return ba, sa, us, lld, tc


def test_generate_writes_only_the_closure_report_stream(sow_file, sample_metadata, isolated_output_dir):
    pid = "clr_iso"
    _full_pipeline(pid, sow_file, sample_metadata)
    proj = isolated_output_dir / pid

    before = {
        str(p.relative_to(proj)).replace("\\", "/"): p.read_bytes()
        for p in proj.rglob("versions.json")
    }
    assert "closure_report/versions.json" not in before

    cr = ClosureReportService(project_id=pid, agent=StubClosureReportAgent())
    cr.generate()
    cr.regenerate()

    after = {
        str(p.relative_to(proj)).replace("\\", "/"): p.read_bytes()
        for p in proj.rglob("versions.json")
    }
    # every pre-existing stream is byte-identical
    for rel, blob in before.items():
        assert after[rel] == blob, f"{rel} was mutated by closure-report generation"
    # the only new file is the closure report's own stream
    assert set(after) - set(before) == {"closure_report/versions.json"}
    recs = json.loads(after["closure_report/versions.json"].decode("utf-8"))
    assert [r["version"] for r in recs] == [1, 2]
