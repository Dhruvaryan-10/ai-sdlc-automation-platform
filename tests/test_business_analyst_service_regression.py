"""
Phase 1 regression coverage (TEST 10).

Exercises the full BRD lifecycle with a stub agent so the shared
`stamp_version_number` refactor and the VersionService `subdir`/`source_ref`
additions cannot have broken existing behaviour.
"""

import pytest

from app.agents.business_analyst.service import BRDLockedError, BusinessAnalystService


@pytest.fixture
def ba(stub_ba_agent):
    return BusinessAnalystService(project_id="brd-regression", agent=stub_ba_agent)


def test_full_brd_lifecycle(ba, sow_file, sample_metadata):
    # generate v1
    v1 = ba.generate_initial_brd(sow_file, sample_metadata)
    assert v1.version == 1
    assert v1.source == "initial"
    assert "**Version:** 1" in v1.content
    assert "**Version:** 0" not in v1.content

    # manual edit -> v2, v1 unchanged
    v1_content = ba.get_version(1).content
    v2 = ba.save_manual_edit(v1_content + "\n\nManual change.\n", note="tweak")
    assert v2.version == 2
    assert "**Version:** 2" in v2.content
    assert ba.get_version(1).content == v1_content

    # ai refine -> v3
    v3 = ba.refine_with_ai("Add MFA as a functional requirement")
    assert v3.version == 3
    assert v3.source == "ai_refine"
    assert "**Version:** 3" in v3.content

    # final selection locks
    ba.choose_final_brd(3)
    assert ba.is_locked() is True
    with pytest.raises(BRDLockedError):
        ba.save_manual_edit("nope")
    with pytest.raises(BRDLockedError):
        ba.refine_with_ai("nope")

    # unlock re-enables editing, history intact
    ba.unlock_final_brd()
    assert ba.is_locked() is False
    v4 = ba.save_manual_edit(ba.get_version(3).content + "\npost-unlock\n")
    assert v4.version == 4
    assert ba.get_final_brd().version == 3
    assert len(ba.get_all_versions()) == 4


def test_unsupported_file_type_rejected(ba, tmp_path, sample_metadata):
    from app.agents.business_analyst.service import UnsupportedFileTypeError

    bad = tmp_path / "sow.rtf"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFileTypeError):
        ba.generate_initial_brd(bad, sample_metadata)
