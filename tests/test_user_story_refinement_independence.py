"""
Phase 5 independence guarantees.

- Refinement writes ONLY into the user_stories stream; the BRD / HLD / LLD
  streams are untouched.
- The refinement package imports no other agent package.
- The Phase 3 InitialUserStoryService keeps operating the same user_stories
  stream unchanged after refinement versions exist (single source of truth).
"""

import ast
from pathlib import Path

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService
from app.agents.user_story_refinement.service import UserStoryRefinementService

PID = "proj"


def _seed(stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    sa = SolutionArchitectService(project_id=PID, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)

    lld = LowLevelDesignService(project_id=PID, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    lld.generate_initial_lld()
    lld.choose_final_lld(1)
    return ba, us, sa, lld


def test_refinement_writes_only_user_story_stream(isolated_output_dir, stub_ba_agent, stub_us_agent,
                                                 stub_sa_agent, stub_lld_agent, stub_usr_agent,
                                                 sow_file, sample_metadata):
    ba, us, sa, lld = _seed(stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent,
                            sow_file, sample_metadata)

    brd_before = (isolated_output_dir / PID / "versions.json").read_text()
    hld_before = (isolated_output_dir / PID / "hld" / "versions.json").read_text()
    lld_before = (isolated_output_dir / PID / "lld" / "versions.json").read_text()

    UserStoryRefinementService(project_id=PID, agent=stub_usr_agent).refine()

    assert (isolated_output_dir / PID / "versions.json").read_text() == brd_before
    assert (isolated_output_dir / PID / "hld" / "versions.json").read_text() == hld_before
    assert (isolated_output_dir / PID / "lld" / "versions.json").read_text() == lld_before

    # four distinct stream files
    stories_file = isolated_output_dir / PID / "user_stories" / "versions.json"
    files = {
        (isolated_output_dir / PID / "versions.json").read_text(),
        hld_before,
        lld_before,
        stories_file.read_text(),
    }
    assert len(files) == 4


def test_source_stream_snapshots_unchanged_by_full_refinement_lifecycle(
    stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent, stub_usr_agent,
    sow_file, sample_metadata,
):
    ba, us, sa, lld = _seed(stub_ba_agent, stub_us_agent, stub_sa_agent, stub_lld_agent,
                            sow_file, sample_metadata)
    brd_snap = [v.model_dump() for v in ba.get_all_versions()]
    hld_snap = [v.model_dump() for v in sa.get_all_versions()]
    lld_snap = [v.model_dump() for v in lld.get_all_versions()]

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    usr.refine()
    usr.refine()

    assert [v.model_dump() for v in ba.get_all_versions()] == brd_snap
    assert [v.model_dump() for v in sa.get_all_versions()] == hld_snap
    assert [v.model_dump() for v in lld.get_all_versions()] == lld_snap


def test_phase3_service_still_operates_stream_after_refinement(
    stub_ba_agent, stub_us_agent, stub_usr_agent, sow_file, sample_metadata,
):
    ba = BusinessAnalystService(project_id=PID, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    us = InitialUserStoryService(project_id=PID, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()

    usr = UserStoryRefinementService(project_id=PID, agent=stub_usr_agent)
    usr.refine()  # v2 (an artifact-refinement version)

    # a fresh Phase 3 service sees the whole stream, including the refinement version
    us2 = InitialUserStoryService(project_id=PID, ba_service=ba)
    assert [v.version for v in us2.get_all_versions()] == [1, 2]
    assert us2.get_version(2).source == "ai_refine"

    # and it can still append a manual edit on top -> v3
    v3 = us2.save_manual_edit(us2.get_version(2).content + "\nhand tweak\n")
    assert v3.version == 3
    assert us2.get_version(1).source == "initial"  # unchanged


def test_refinement_package_imports_no_other_agent_package():
    import app.agents.user_story_refinement.agent as usr_agent_mod
    import app.agents.user_story_refinement.service as usr_service_mod

    forbidden = ("initial_user_story", "solution_architect", "low_level_design")
    for mod in (usr_agent_mod, usr_service_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any(f in name for name in imported for f in forbidden), imported
