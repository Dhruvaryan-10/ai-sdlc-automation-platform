"""
Tests for the "Load Existing Project" helpers (app/ui/project_registry.py) and a
deterministic check that opening an existing project is read-only.

No Gemini: every service below is built with a stub agent from conftest.
"""

import json
from pathlib import Path

import pytest

from app.ui.project_registry import list_existing_projects, sanitize_project_id


# --- sanitize_project_id --------------------------------------------------

@pytest.mark.parametrize("value", ["d1801c21", "my-proj_2", "A", "a" * 64, "Proj-1_x"])
def test_sanitize_accepts_valid_ids(value):
    assert sanitize_project_id(value) == value


def test_sanitize_strips_surrounding_whitespace():
    assert sanitize_project_id("  d1801c21  ") == "d1801c21"


@pytest.mark.parametrize("value", [
    "", "   ", "\t\n", "../etc", "a/b", "a\\b", ".", "..", "a b",
    "a" * 65, "bad!", "with.dot", "space bar", "/", "\\", "a/../b",
])
def test_sanitize_rejects_invalid_ids(value):
    assert sanitize_project_id(value) is None


def test_sanitize_rejects_non_str():
    assert sanitize_project_id(None) is None
    assert sanitize_project_id(123) is None


# --- list_existing_projects --------------------------------------------

def _seed_versions_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{
            "version": 1, "content": "# doc", "source": "initial",
            "created_at": "2026-01-01T00:00:00", "note": "", "is_final": False,
            "is_locked": False, "source_ref": None,
        }]),
        encoding="utf-8",
    )


def test_list_existing_projects_missing_dir_returns_empty(tmp_path):
    assert list_existing_projects(tmp_path / "does-not-exist") == []


def test_list_existing_projects_selects_only_dirs_with_root_versions_file(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()

    # included: has a root versions.json
    _seed_versions_file(out / "proj-with-brd" / "versions.json")
    # included: root versions.json AND sub-streams
    _seed_versions_file(out / "proj-full" / "versions.json")
    _seed_versions_file(out / "proj-full" / "hld" / "versions.json")
    # excluded: only a sub-stream, no root versions.json
    _seed_versions_file(out / "proj-hld-only" / "hld" / "versions.json")
    # excluded: empty directory
    (out / "empty-dir").mkdir()
    # excluded: a stray file (not a directory)
    (out / "stray.json").write_text("{}", encoding="utf-8")
    # excluded: a file literally named like a project
    (out / "versions.json").write_text("[]", encoding="utf-8")

    assert list_existing_projects(out) == ["proj-full", "proj-with-brd"]


def test_list_existing_projects_result_is_sorted(tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    for name in ["zeta", "alpha", "mike"]:
        _seed_versions_file(out / name / "versions.json")
    assert list_existing_projects(out) == ["alpha", "mike", "zeta"]


def test_list_existing_projects_accepts_str_path(tmp_path):
    out = tmp_path / "outputs"
    _seed_versions_file(out / "p1" / "versions.json")
    assert list_existing_projects(str(out)) == ["p1"]


# --- opening an existing project is read-only --------------------------

def _seed_full_project(out: Path, pid: str):
    """Seed a project's five streams; return ({stream: bytes} snapshots, {stream: Path})."""
    streams = {
        "brd": out / pid / "versions.json",
        "hld": out / pid / "hld" / "versions.json",
        "lld": out / pid / "lld" / "versions.json",
        "user_stories": out / pid / "user_stories" / "versions.json",
        "test_cases": out / pid / "test_cases" / "versions.json",
    }
    for name, path in streams.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        final = name in ("brd", "hld", "lld")  # BRD/HLD/LLD accepted + locked
        path.write_text(
            json.dumps([{
                "version": 1,
                "content": f"# {pid} {name}\n\n**Version:** 1\n",
                "source": "initial",
                "created_at": "2026-01-01T00:00:00",
                "note": f"seeded {name}",
                "is_final": final,
                "is_locked": final,
                "source_ref": "brd_v1;hld_v1;lld_v1;us_v1" if name == "test_cases" else None,
            }], indent=2),
            encoding="utf-8",
        )
    return {name: path.read_bytes() for name, path in streams.items()}, streams


def test_opening_existing_project_is_read_only(
    isolated_output_dir, stub_ba_agent, stub_sa_agent, stub_us_agent,
    stub_lld_agent, stub_usr_agent, stub_tc_agent,
):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.initial_user_story.service import InitialUserStoryService
    from app.agents.low_level_design.service import LowLevelDesignService
    from app.agents.solution_architect.service import SolutionArchitectService
    from app.agents.test_case.service import TestCaseService
    from app.agents.user_story_refinement.service import UserStoryRefinementService

    out = isolated_output_dir
    pid = "existing1"
    snapshots, streams = _seed_full_project(out, pid)

    # Construct all six services against the existing id (the UI's bootstrap path).
    ba = BusinessAnalystService(project_id=pid, agent=stub_ba_agent)
    sa = SolutionArchitectService(project_id=pid, ba_service=ba, agent=stub_sa_agent)
    us = InitialUserStoryService(project_id=pid, ba_service=ba, agent=stub_us_agent)
    lld = LowLevelDesignService(project_id=pid, sa_service=sa, ba_service=ba, agent=stub_lld_agent)
    usr = UserStoryRefinementService(project_id=pid, agent=stub_usr_agent)
    qa = TestCaseService(project_id=pid, agent=stub_tc_agent)

    # Exercise only read-only accessors (what the sidebar / state block call).
    assert ba.get_all_versions()[0].version == 1
    assert ba.get_final_brd().is_locked is True
    assert sa.get_all_versions()[0].content.startswith("# existing1 hld")
    assert us.get_all_versions()[0].note == "seeded user_stories"
    assert lld.get_final_lld().is_locked is True
    assert usr.recorded_source_versions() is None      # latest US not a refinement
    assert usr.stale_sources() == []
    assert qa.get_all_versions()[0].source_ref == "brd_v1;hld_v1;lld_v1;us_v1"
    assert qa.recorded_source_versions() == {"brd": 1, "hld": 1, "lld": 1, "us": 1}
    assert qa.stale_sources() == []
    assert qa.is_locked() is False

    # Nothing was written: every seeded file is byte-identical.
    for name, path in streams.items():
        assert path.read_bytes() == snapshots[name], f"{name} stream was modified"


def test_opening_nonexistent_project_creates_no_versions_files(isolated_output_dir):
    from app.agents.business_analyst.service import BusinessAnalystService
    from app.agents.test_case.service import TestCaseService

    pid = "never-generated"
    BusinessAnalystService(project_id=pid)
    TestCaseService(project_id=pid)

    # VersionService.__init__ does mkdir(exist_ok=True) but writes no file until
    # an explicit add_version — so a mistyped/never-used id leaves no versions.json.
    created = list((isolated_output_dir / pid).rglob("versions.json"))
    assert created == []
    assert list_existing_projects(isolated_output_dir) == []
