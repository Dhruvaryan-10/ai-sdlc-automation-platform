"""BRD, HLD, User Story, and LLD version streams must be stored and mutated independently."""

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.low_level_design.service import LowLevelDesignService
from app.agents.solution_architect.service import SolutionArchitectService


def _stack(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata, project_id="proj"):
    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    sa = SolutionArchitectService(project_id=project_id, ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.choose_final_hld(1)

    us = InitialUserStoryService(project_id=project_id, ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    return ba, sa, us


def test_four_separate_storage_files(
    isolated_output_dir, stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent,
    sow_file, sample_metadata,
):
    ba, sa, us = _stack(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata)
    LowLevelDesignService(
        project_id="proj", sa_service=sa, ba_service=ba, agent=stub_lld_agent
    ).generate_initial_lld()

    brd_file = isolated_output_dir / "proj" / "versions.json"
    hld_file = isolated_output_dir / "proj" / "hld" / "versions.json"
    stories_file = isolated_output_dir / "proj" / "user_stories" / "versions.json"
    lld_file = isolated_output_dir / "proj" / "lld" / "versions.json"

    assert brd_file.exists() and hld_file.exists() and stories_file.exists() and lld_file.exists()
    contents = {
        brd_file.read_text(),
        hld_file.read_text(),
        stories_file.read_text(),
        lld_file.read_text(),
    }
    assert len(contents) == 4  # pairwise distinct


def test_lld_ops_do_not_touch_brd_hld_or_stories(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us = _stack(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata)
    brd_snapshot = [v.model_dump() for v in ba.get_all_versions()]
    hld_snapshot = [v.model_dump() for v in sa.get_all_versions()]
    stories_snapshot = [v.model_dump() for v in us.get_all_versions()]

    lld = LowLevelDesignService(
        project_id="proj", sa_service=sa, ba_service=ba, agent=stub_lld_agent
    )
    lld.generate_initial_lld()
    lld.save_manual_edit(lld.get_version(1).content + "\nmore\n")
    lld.refine_with_ai("add a table")
    lld.choose_final_lld(3)
    lld.unlock_final_lld()

    assert [v.model_dump() for v in ba.get_all_versions()] == brd_snapshot
    assert [v.model_dump() for v in sa.get_all_versions()] == hld_snapshot
    assert [v.model_dump() for v in us.get_all_versions()] == stories_snapshot


def test_upstream_ops_do_not_touch_lld(
    stub_ba_agent, stub_sa_agent, stub_us_agent, stub_lld_agent, sow_file, sample_metadata
):
    ba, sa, us = _stack(stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata)
    lld = LowLevelDesignService(
        project_id="proj", sa_service=sa, ba_service=ba, agent=stub_lld_agent
    )
    lld.generate_initial_lld()
    lld.save_manual_edit(lld.get_version(1).content + "\nedit\n")
    lld_snapshot = [v.model_dump() for v in lld.get_all_versions()]

    sa.unlock_final_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\nhld edit\n")
    sa.choose_final_hld(2)

    us.save_manual_edit(us.get_version(1).content + "\nstory edit\n")

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\nnew\n")
    ba.choose_final_brd(2)

    assert [v.model_dump() for v in lld.get_all_versions()] == lld_snapshot
