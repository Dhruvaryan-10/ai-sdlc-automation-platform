"""BRD, HLD, and User Story version streams must be stored and mutated independently."""

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.initial_user_story.service import InitialUserStoryService
from app.agents.solution_architect.service import SolutionArchitectService


def _final_brd(stub_ba_agent, sow_file, sample_metadata, project_id="proj"):
    ba = BusinessAnalystService(project_id=project_id, agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    return ba


def test_three_separate_storage_files(
    isolated_output_dir, stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    SolutionArchitectService(
        project_id="proj", ba_service=ba, agent=stub_sa_agent
    ).generate_initial_hld()
    InitialUserStoryService(
        project_id="proj", ba_service=ba, agent=stub_us_agent
    ).generate_initial_stories()

    brd_file = isolated_output_dir / "proj" / "versions.json"
    hld_file = isolated_output_dir / "proj" / "hld" / "versions.json"
    stories_file = isolated_output_dir / "proj" / "user_stories" / "versions.json"

    assert brd_file.exists() and hld_file.exists() and stories_file.exists()
    contents = {brd_file.read_text(), hld_file.read_text(), stories_file.read_text()}
    assert len(contents) == 3  # pairwise distinct


def test_story_ops_do_not_touch_brd_or_hld(
    stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    sa = SolutionArchitectService(project_id="proj", ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()

    brd_snapshot = [v.model_dump() for v in ba.get_all_versions()]
    hld_snapshot = [v.model_dump() for v in sa.get_all_versions()]

    us = InitialUserStoryService(project_id="proj", ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    us.save_manual_edit(us.get_version(1).content + "\nmore\n")
    us.refine_with_ai("add a story")
    us.choose_final_stories(3)
    us.unlock_final_stories()

    assert [v.model_dump() for v in ba.get_all_versions()] == brd_snapshot
    assert [v.model_dump() for v in sa.get_all_versions()] == hld_snapshot


def test_brd_and_hld_ops_do_not_touch_stories(
    stub_ba_agent, stub_sa_agent, stub_us_agent, sow_file, sample_metadata
):
    ba = _final_brd(stub_ba_agent, sow_file, sample_metadata)
    us = InitialUserStoryService(project_id="proj", ba_service=ba, agent=stub_us_agent)
    us.generate_initial_stories()
    us.save_manual_edit(us.get_version(1).content + "\nedit\n")
    stories_snapshot = [v.model_dump() for v in us.get_all_versions()]

    sa = SolutionArchitectService(project_id="proj", ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\nhld edit\n")

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\nnew\n")
    ba.choose_final_brd(2)

    assert [v.model_dump() for v in us.get_all_versions()] == stories_snapshot
