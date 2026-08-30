"""
Phase 6 independence guarantees.

- The test_cases stream is its own append-only stream; generation/refinement
  writes ONLY that file.
- No second copy of any other stream is created.
- The test_case package imports no other agent package's implementation.
"""

import ast
from pathlib import Path

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.test_case.service import TestCaseService

PID = "proj"


def _seed(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    us.choose_final_stories(1)

    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)

    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    return ba, us, sa, lld


def test_full_qa_lifecycle_touches_only_test_case_stream(
    isolated_output_dir, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent,
    stub_tc_agent, sow_file, sample_metadata,
):
    ba, us, sa, lld = _seed(stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent,
                            sow_file, sample_metadata)

    brd_snap = [v.model_dump() for v in ba.get_all_versions()]
    us_snap = [v.model_dump() for v in us.get_all_versions()]
    hld_snap = [v.model_dump() for v in sa.get_all_versions()]
    lld_snap = [v.model_dump() for v in lld.get_all_versions()]

    qa = TestCaseService(project_id=PID, agent=stub_tc_agent)
    qa.generate()
    qa.save_manual_edit(qa.get_version(1).content + "\n\n## TC-090 — extra\n**Expected Result:** ok\n")
    qa.refine_with_ai("add boundary cases")
    qa.regenerate()
    qa.choose_final(4)
    qa.unlock_final()

    # every other stream byte-identical
    assert [v.model_dump() for v in ba.get_all_versions()] == brd_snap
    assert [v.model_dump() for v in us.get_all_versions()] == us_snap
    assert [v.model_dump() for v in sa.get_all_versions()] == hld_snap
    assert [v.model_dump() for v in lld.get_all_versions()] == lld_snap

    # exactly the five expected stream files exist — no refined_user_stories/, no dup
    proj = isolated_output_dir / PID
    subdirs = sorted(p.name for p in proj.iterdir() if p.is_dir())
    assert subdirs == ["hld", "lld", "test_cases", "user_stories"]
    files = {
        (proj / "versions.json").read_text(),
        (proj / "hld" / "versions.json").read_text(),
        (proj / "lld" / "versions.json").read_text(),
        (proj / "user_stories" / "versions.json").read_text(),
        (proj / "test_cases" / "versions.json").read_text(),
    }
    assert len(files) == 5  # pairwise distinct


def test_test_case_package_imports_no_other_agent_package():
    import app.agents.test_case.agent as tc_agent_mod
    import app.agents.test_case.service as tc_service_mod

    forbidden = (
        "solution_architect",
        "initial_user_story",
        "user_story_refinement",
        "low_level_design",
    )
    for mod in (tc_agent_mod, tc_service_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any(f in name for name in imported for f in forbidden), imported
        # only shared value type + prompt infra may come from business_analyst
        ba_imports = [n for n in imported if n.startswith("app.agents.business_analyst")]
        assert set(ba_imports) <= {
            "app.agents.business_analyst.agent",
            "app.agents.business_analyst.prompt_manager",
        }, ba_imports
