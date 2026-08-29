"""Tests for VersionService, incl. the Phase 2 `subdir` and `source_ref` additions."""

from app.services.version_service import VersionService


def test_subdir_isolates_storage(isolated_output_dir):
    brd = VersionService(project_id="p1")
    hld = VersionService(project_id="p1", subdir="hld")

    brd.add_version(content="brd body", source="initial")
    hld.add_version(content="hld body", source="initial")

    assert (isolated_output_dir / "p1" / "versions.json").exists()
    assert (isolated_output_dir / "p1" / "hld" / "versions.json").exists()

    # Each instance only ever sees its own stream.
    assert len(brd.get_all_versions()) == 1
    assert len(hld.get_all_versions()) == 1
    assert brd.get_all_versions()[0].content == "brd body"
    assert hld.get_all_versions()[0].content == "hld body"


def test_default_path_unchanged_for_brd(isolated_output_dir):
    """Existing BRD callers pass no subdir and must land in the same place as before."""
    VersionService(project_id="p2").add_version(content="x", source="initial")
    assert (isolated_output_dir / "p2" / "versions.json").exists()
    assert not (isolated_output_dir / "p2" / "hld").exists()


def test_append_only_and_deterministic_numbering():
    svc = VersionService(project_id="p3", subdir="hld")
    v1 = svc.add_version(content="one", source="initial")
    v2 = svc.add_version(content="two", source="manual_edit")
    v3 = svc.add_version(content="three", source="ai_refine")

    assert [v1.version, v2.version, v3.version] == [1, 2, 3]
    all_versions = svc.get_all_versions()
    assert [v.content for v in all_versions] == ["one", "two", "three"]
    # earlier versions are never mutated
    assert all_versions[0].content == "one"


def test_mark_final_is_exclusive_and_locks():
    svc = VersionService(project_id="p4", subdir="hld")
    svc.add_version(content="one", source="initial")
    svc.add_version(content="two", source="manual_edit")

    svc.mark_final(1)
    svc.mark_final(2)  # switching final must clear the previous one

    finals = [v for v in svc.get_all_versions() if v.is_final]
    assert len(finals) == 1
    assert finals[0].version == 2
    assert finals[0].is_locked is True
    assert svc.get_version(1).is_final is False
    assert svc.get_version(1).is_locked is False


def test_unlock_final_keeps_is_final():
    svc = VersionService(project_id="p5", subdir="hld")
    svc.add_version(content="one", source="initial")
    svc.mark_final(1)

    svc.unlock_final()

    final = svc.get_final_version()
    assert final.version == 1
    assert final.is_final is True
    assert final.is_locked is False


def test_source_ref_round_trips():
    svc = VersionService(project_id="p6", subdir="hld")
    svc.add_version(content="hld", source="initial", source_ref="brd_v3")

    reloaded = VersionService(project_id="p6", subdir="hld").get_version(1)
    assert reloaded.source_ref == "brd_v3"


def test_source_ref_defaults_to_none():
    svc = VersionService(project_id="p7")
    svc.add_version(content="brd", source="initial")
    assert svc.get_version(1).source_ref is None
