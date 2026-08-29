"""BRD and HLD version streams must be stored and mutated independently."""

from app.agents.business_analyst.service import BusinessAnalystService
from app.agents.solution_architect.service import SolutionArchitectService


def test_storage_paths_are_separate(isolated_output_dir, stub_ba_agent, stub_sa_agent,
                                    sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    sa = SolutionArchitectService(project_id="proj", ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()

    brd_file = isolated_output_dir / "proj" / "versions.json"
    hld_file = isolated_output_dir / "proj" / "hld" / "versions.json"
    assert brd_file.exists() and hld_file.exists()
    assert brd_file.read_text() != hld_file.read_text()


def test_hld_edits_do_not_touch_brd(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)
    brd_snapshot = [v.model_dump() for v in ba.get_all_versions()]

    sa = SolutionArchitectService(project_id="proj", ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\nmore\n")
    sa.refine_with_ai("add caching")
    sa.choose_final_hld(3)
    sa.unlock_final_hld()

    assert [v.model_dump() for v in ba.get_all_versions()] == brd_snapshot


def test_brd_edits_do_not_touch_hld(stub_ba_agent, stub_sa_agent, sow_file, sample_metadata):
    ba = BusinessAnalystService(project_id="proj", agent=stub_ba_agent)
    ba.generate_initial_brd(sow_file, sample_metadata)
    ba.choose_final_brd(1)

    sa = SolutionArchitectService(project_id="proj", ba_service=ba, agent=stub_sa_agent)
    sa.generate_initial_hld()
    sa.save_manual_edit(sa.get_version(1).content + "\nedit\n")
    hld_snapshot = [v.model_dump() for v in sa.get_all_versions()]

    ba.unlock_final_brd()
    ba.save_manual_edit(ba.get_version(1).content + "\nnew\n")
    ba.choose_final_brd(2)

    assert [v.model_dump() for v in sa.get_all_versions()] == hld_snapshot
